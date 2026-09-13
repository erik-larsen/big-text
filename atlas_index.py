#!/usr/bin/env python3
"""Stage 1: index a source tree into data/<name>_atlas/index.npz + index.json.

Walks the tree (git ls-files inside a repo, a plain walk otherwise), keeps the
text files, normalises every line to printable ASCII (tabs to 4 spaces, one
byte per column, trailing whitespace stripped, 4096 columns at most), runs a
small per-language tokenizer that gives every character a kind, and finds the
items (fn, struct, def, class ...) with their line extents.

Files are ordered so that a directory's own files and all of its descendants'
files are one contiguous range, and so are their lines. See docs/DESIGN.md
for the exact arrays and JSON keys.

  ./atlas_index.py big-picture --out data/big-picture_atlas
  ./atlas_index.py makepad/draw makepad/platform --out data/makepad-draw_atlas
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
from multiprocessing import Pool

import numpy as np

SKIP_NAMES = {".git", "target", "node_modules", "__pycache__", ".venv", "data", "pics"}
MAX_BYTES = 4 << 20
MAX_COLS = 4096
TAB = 4

KINDS = ["space", "ident", "keyword", "type", "string", "comment", "number",
         "punct", "macro", "call"]
SPACE, IDENT, KEYWORD, TYPE, STRING, COMMENT, NUMBER, PUNCT, MACRO, CALL = range(10)
KB = [bytes([k]) for k in range(10)]

# item kinds
K_FN, K_STRUCT, K_ENUM, K_IMPL, K_OTHER = 1, 2, 3, 4, 5

# language family by extension; everything else is "text" (a few text
# variants only differ in which comment syntax they recognise)
EXT_LANG = {".rs": "rust"}
for _e in (".c .h .cpp .hpp .cc .hh .cxx .m .mm .js .ts .jsx .tsx .mjs .java .go "
           ".cs .swift .glsl .frag .vert .metal .wgsl .kt .scala").split():
    EXT_LANG[_e] = "clike"
for _e in (".py", ".pyi"):
    EXT_LANG[_e] = "python"
for _e in (".sh", ".zsh", ".bash"):
    EXT_LANG[_e] = "shell"

TEXT_VARIANT = {}
for _e in (".toml .yaml .yml .ini .cfg .conf .lock .env .mk .cmake .properties "
           ".gitignore .gitmodules .gitattributes .txt").split():
    TEXT_VARIANT[_e] = "text-hash"
for _e in ".html .htm .xml .svg .md .markdown .plist .xib .storyboard .xhtml".split():
    TEXT_VARIANT[_e] = "text-xml"
for _e in ".css .scss .less".split():
    TEXT_VARIANT[_e] = "text-css"
for _e in ".ron .jsonc .proto".split():
    TEXT_VARIANT[_e] = "text-slash"
TEXT_BASENAMES = {"makefile": "text-hash", "dockerfile": "text-hash",
                  "cmakelists.txt": "text-hash"}


# ---------------------------------------------------------------- tokenizer

_IDENT = rb"[A-Za-z_][A-Za-z0-9_]*"
_NUM = (rb"0[xX][0-9A-Fa-f_]+[A-Za-z0-9_]*|0[bB][01_]+[A-Za-z0-9_]*|0[oO][0-7_]+"
        rb"|\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?[A-Za-z0-9_]*")
_SLASH_COMMENT = rb"//[^\n]*|/\*[\s\S]*?\*/|/\*[\s\S]*"

RUST_KW = set(b"""as async await break const continue crate dyn else enum extern
false fn for if impl in let loop match mod move mut pub ref return self Self
static struct super trait true type unsafe use where while union macro_rules
""".split())
CLIKE_KW = set(b"""auto break case char const continue default do double else
enum extern float for goto if inline int long register restrict return short
signed sizeof static struct switch typedef union unsigned void volatile while
bool true false NULL nullptr class namespace template typename this new delete
public private protected virtual override final using try catch throw explicit
operator friend constexpr static_cast dynamic_cast reinterpret_cast const_cast
decltype noexcept mutable function var let of import export from as await async
yield instanceof typeof null undefined interface type implements extends super
readonly declare module abstract package boolean byte synchronized transient
finally native throws assert string object decimal event foreach in internal
is lock out params ref sealed stackalloc uint ulong ushort sbyte checked
unchecked unsafe fixed func go chan select defer map range fallthrough guard
protocol extension init deinit subscript associatedtype some any inout where
willSet didSet get set open fileprivate mutating nonmutating convenience
required optional lazy weak unowned rethrows indirect self nil id
uniform varying attribute layout precision highp mediump lowp discard
vec2 vec3 vec4 ivec2 ivec3 ivec4 uvec2 uvec3 uvec4 bvec2 bvec3 bvec4 mat2 mat3
mat4 mat2x2 mat3x3 mat4x4 sampler2D sampler3D samplerCube sampler2DArray
kernel vertex fragment device constant threadgroup thread half float2 float3
float4 float4x4 float3x3 int2 int3 int4 uint2 uint3 uint4 texture2d sampler
vec2f vec3f vec4f vec2i vec3i vec4i vec2u vec3u vec4u mat4x4f mat3x3f f32 i32
u32 f16 array ptr atomic bitcast fn let var override storage workgroup
texture_2d""".split())
PYTHON_KW = set(b"""False None True and as assert async await break class
continue def del elif else except finally for from global if import in is
lambda nonlocal not or pass raise return try while with yield match case
""".split())
SHELL_KW = set(b"""if then else elif fi for while until do done case esac in
function select time return local export exit set unset source readonly
declare typeset shift break continue eval exec trap true false test echo cd
""".split())

RUST_DEF = {b"fn", b"struct", b"enum", b"trait", b"type", b"mod", b"union", b"macro_rules"}
CLIKE_DEF = {b"class", b"interface", b"namespace", b"function", b"func", b"fn", b"protocol"}
PYTHON_DEF = {b"def", b"class"}
SHELL_DEF = {b"function"}


def _rx(*alts):
    return re.compile(b"|".join(alts), re.M)


# each spec: (regex, keywords, definition keywords, code?)  where code? says
# whether identifiers get the keyword/type/call treatment
SPECS = {
    "rust": (_rx(
        rb"(?P<comment>" + _SLASH_COMMENT + rb")",
        rb"(?P<string>b?r(?P<h>#*)\"[\s\S]*?\"(?P=h)|b?\"(?:[^\"\\]|\\[\s\S])*\"|b?\"[^\n]*)",
        rb"(?P<char>b?'(?:\\(?:u\{[0-9A-Fa-f_]+\}|x[0-9A-Fa-f]{2}|[^\n])|[^'\\\n])')",
        rb"(?P<life>'[A-Za-z_][A-Za-z0-9_]*)",
        rb"(?P<macro>#!?\[[^\]\n]*\]?|[A-Za-z_][A-Za-z0-9_]*!(?!=))",
        rb"(?P<number>" + _NUM + rb")",
        rb"(?P<ident>" + _IDENT + rb")"), RUST_KW, RUST_DEF, True),
    "clike": (_rx(
        rb"(?P<macro>^[ \t]*#[^\n]*(?:\\\n[^\n]*)*|@[A-Za-z_][A-Za-z0-9_]*)",
        rb"(?P<comment>" + _SLASH_COMMENT + rb")",
        rb"(?P<string>\"(?:[^\"\\\n]|\\[\s\S])*\"?|'(?:[^'\\\n]|\\[\s\S])*'?|`(?:[^`\\]|\\[\s\S])*`?)",
        rb"(?P<number>" + _NUM + rb")",
        rb"(?P<ident>" + _IDENT + rb")"), CLIKE_KW, CLIKE_DEF, True),
    "python": (_rx(
        rb"(?P<comment>#[^\n]*)",
        rb"(?P<string>[rRbBuUfF]{0,2}(?:\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"\"\"[\s\S]*|'''[\s\S]*"
        rb"|\"(?:[^\"\\\n]|\\[\s\S])*\"?|'(?:[^'\\\n]|\\[\s\S])*'?))",
        rb"(?P<macro>@[A-Za-z_][A-Za-z0-9_.]*)",
        rb"(?P<number>" + _NUM + rb")",
        rb"(?P<ident>" + _IDENT + rb")"), PYTHON_KW, PYTHON_DEF, True),
    "shell": (_rx(
        rb"(?P<macro>\$\{[^}\n]*\}?|\$[A-Za-z_@#?*0-9][A-Za-z0-9_]*)",
        rb"(?P<comment>#[^\n]*)",
        rb"(?P<string>\"(?:[^\"\\]|\\[\s\S])*\"?|'[^']*'?)",
        rb"(?P<number>\b\d+\b)",
        rb"(?P<ident>[A-Za-z_][A-Za-z0-9_-]*)"), SHELL_KW, SHELL_DEF, True),
}
_TEXT_STRING = rb"(?P<string>\"(?:[^\"\\\n]|\\.)*\")"
_TEXT_TAIL = (rb"(?P<number>\b\d+(?:\.\d+)?\b)", rb"(?P<ident>" + _IDENT + rb")")
SPECS["text"] = (_rx(_TEXT_STRING, *_TEXT_TAIL), set(), set(), False)
SPECS["text-hash"] = (_rx(rb"(?P<comment>#[^\n]*)", _TEXT_STRING, *_TEXT_TAIL), set(), set(), False)
SPECS["text-xml"] = (_rx(rb"(?P<comment><!--[\s\S]*?-->)", _TEXT_STRING, *_TEXT_TAIL), set(), set(), False)
SPECS["text-css"] = (_rx(rb"(?P<comment>/\*[\s\S]*?\*/)", _TEXT_STRING, *_TEXT_TAIL), set(), set(), False)
SPECS["text-slash"] = (_rx(rb"(?P<comment>" + _SLASH_COMMENT + rb")", _TEXT_STRING, *_TEXT_TAIL),
                       set(), set(), False)

GROUP_KIND = {"comment": COMMENT, "string": STRING, "char": STRING, "life": IDENT,
              "macro": MACRO, "number": NUMBER}


def tokenize(text, spec):
    """Kind per byte of text (which may contain newlines), as a bytearray."""
    rx, kw, defkw, code = spec
    kinds = bytearray(b"\x07") * len(text)      # punct unless told otherwise
    prev = None
    for m in rx.finditer(text):
        g = m.lastgroup
        s, e = m.span()
        if g == "ident":
            if code:
                tok = m.group()
                if tok in kw:
                    k = KEYWORD
                elif prev in defkw:
                    k = CALL
                elif text[e:e + 1] == b"(":
                    k = CALL
                elif 65 <= tok[0] <= 90:
                    k = TYPE
                else:
                    k = IDENT
                prev = tok
            else:
                k = IDENT
        else:
            k = GROUP_KIND[g]
            prev = m.group()[:-1] if g == "macro" else None   # macro_rules! name
        kinds[s:e] = KB[k] * (e - s)
    return kinds


# ---------------------------------------------------------------- items

RUST_ITEM = re.compile(
    rb"^[ \t]*(?:pub(?:\([^)\n]*\))?[ \t]+)?(?:(?:unsafe|async|const|default|extern(?:[ \t]+\"[^\"\n]*\")?)[ \t]+)*"
    rb"(fn|struct|enum|trait|impl|mod|macro_rules!|type|const|static|union)\b[ \t]*([^\n]*)", re.M)
RUST_ITEM_KIND = {b"fn": K_FN, b"struct": K_STRUCT, b"union": K_STRUCT, b"enum": K_ENUM,
                  b"impl": K_IMPL, b"trait": K_IMPL, b"mod": K_OTHER, b"macro_rules!": K_OTHER,
                  b"type": K_OTHER, b"const": K_OTHER, b"static": K_OTHER}
PYTHON_ITEM = re.compile(rb"^([ \t]*)(?:async[ \t]+)?(def|class)[ \t]+([A-Za-z_][A-Za-z0-9_]*)", re.M)
CLIKE_TYPE_ITEM = re.compile(
    rb"^[ \t]*(?:(?:public|private|protected|static|final|abstract|export|default|typedef|internal"
    rb"|sealed|partial|open|declare|const)[ \t]+)*(struct|enum|union|class|interface|namespace|protocol|extension)"
    rb"[ \t]+(?:class[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)", re.M)
CLIKE_TYPE_KIND = {b"struct": K_STRUCT, b"union": K_STRUCT, b"class": K_STRUCT, b"enum": K_ENUM,
                   b"interface": K_IMPL, b"protocol": K_IMPL, b"extension": K_IMPL, b"namespace": K_OTHER}
CLIKE_FUNC_ITEM = re.compile(
    rb"^[ \t]*(?:(?:public|private|protected|static|export|default|async|override|final|mutating"
    rb"|internal|fileprivate|open)[ \t]+)*(?:function|func)[ \t]+(?:\([^)\n]*\)[ \t]*)?([A-Za-z_][A-Za-z0-9_]*)", re.M)
CLIKE_STMT_KW = set(b"""if else for while switch return do case default goto break
continue sizeof typedef using namespace new delete throw try catch class struct
enum union extern static_assert""".split())
C_HEAD_NAME = re.compile(rb"^[^(]*?([A-Za-z_][A-Za-z0-9_]*)[ \t]*\(")
IDENT_RX = re.compile(_IDENT)


def brace_index(cf, kf):
    """Matching braces and depth-0 semicolons of a file's joined text.

    cf, kf: uint8 arrays of the joined text (with newlines) and its kinds.
    Only punct-kind characters count, so braces inside strings, comments and
    macros are ignored."""
    punct = kf == PUNCT
    opens = np.nonzero(punct & (cf == 123))[0]
    closes = np.nonzero(punct & (cf == 125))[0]
    pos = np.concatenate([opens, closes])
    is_open = np.concatenate([np.ones(len(opens), bool), np.zeros(len(closes), bool)])
    order = np.argsort(pos, kind="stable")
    match, stack = {}, []
    for p, o in zip(pos[order].tolist(), is_open[order].tolist()):
        if o:
            stack.append(p)
        elif stack:
            match[stack.pop()] = p
    delta = (punct & ((cf == 40) | (cf == 91))).astype(np.int32)
    delta -= (punct & ((cf == 41) | (cf == 93))).astype(np.int32)
    pdepth = np.cumsum(delta)                      # paren/bracket depth after each char
    semis = np.nonzero(punct & (cf == 59))[0]
    return opens, match, semis, pdepth


def brace_extent(a, start, n_lines, line_of, opens, match, semis, pdepth, max_head=None):
    """End line (exclusive) of an item whose header starts at joined offset a.

    A depth-0 ';' before the first '{' means a body-less item ending on that
    line. Otherwise the body is the first '{' and its match. max_head limits
    how many lines after start the '{' may be (None: unlimited); returns None
    when the item should be dropped."""
    i = np.searchsorted(opens, a)
    o = int(opens[i]) if i < len(opens) else None
    base = int(pdepth[a - 1]) if a > 0 else 0
    j = np.searchsorted(semis, a)
    s = None
    while j < len(semis):
        p = int(semis[j])
        if o is not None and p > o:
            break
        if pdepth[p] <= base:
            s = p
            break
        j += 1
    if s is not None:
        return line_of(s) + 1
    if o is None:
        return start + 1
    if max_head is not None and line_of(o) - start > max_head:
        return None
    c = match.get(o)
    return line_of(c) + 1 if c is not None else n_lines


def rust_items(text, cf, kf, line_of, n_lines):
    opens, match, semis, pdepth = brace_index(cf, kf)
    items = []
    for m in RUST_ITEM.finditer(text):
        if kf[m.start(1)] != KEYWORD and kf[m.start(1)] != MACRO:
            continue                              # keyword inside a comment or string
        word, rest = m.group(1), m.group(2)
        start = line_of(m.start())
        end = brace_extent(m.start(1), start, n_lines, line_of, opens, match, semis, pdepth)
        if end is None or end <= start:
            continue
        if word == b"impl":
            name = rest.split(b"{")[0].split(b" where")[0].strip()
            if name.startswith(b"<"):             # drop the generic parameter list
                depth, k = 0, 0
                for k, ch in enumerate(name):
                    depth += (ch == 60) - (ch == 62)
                    if depth == 0:
                        break
                name = name[k + 1:].strip()
        else:
            rest = re.sub(rb'^(?:(?:mut|unsafe|async|const|extern\s+"[^"]*")\s+)+', b"", rest, count=1)
            nm = IDENT_RX.match(rest)
            name = nm.group() if nm else b""
        if not name:                              # `fn(` pointer types, `mod.x = {` script lines
            continue
        items.append((start, end, RUST_ITEM_KIND[word], name[:80].decode("ascii", "replace")))
    return items


def python_items(text, kf, line_of, line_indent, line_len, n_lines):
    defs = []
    for m in PYTHON_ITEM.finditer(text):
        if kf[m.start(2)] != KEYWORD:
            continue
        defs.append((line_of(m.start()), K_FN if m.group(2) == b"def" else K_STRUCT,
                     m.group(3).decode("ascii")))
    items, stack, last, di = [], [], -1, 0
    for i in range(n_lines):
        if line_len[i] == 0:
            continue
        ind = line_indent[i]
        while stack and ind <= stack[-1][3]:
            s, kind, name, _ = stack.pop()
            items.append((s, last + 1, kind, name))
        last = i
        if di < len(defs) and defs[di][0] == i:
            s, kind, name = defs[di]
            stack.append((s, kind, name, ind))
            di += 1
    while stack:
        s, kind, name, _ = stack.pop()
        items.append((s, last + 1, kind, name))
    items.sort()
    return items


def clike_items(text, cf, kf, lines, line_off, line_of, line_indent, n_lines):
    opens, match, semis, pdepth = brace_index(cf, kf)
    items = []
    for m in CLIKE_TYPE_ITEM.finditer(text):
        if kf[m.start(1)] != KEYWORD:
            continue
        start = line_of(m.start())
        end = brace_extent(m.start(1), start, n_lines, line_of, opens, match, semis, pdepth, 4)
        if end is None or end <= start:
            continue
        if end == start + 1 and text[m.end():line_off[start + 1] - 1 if start + 1 < n_lines else len(text)].find(b"{") < 0:
            continue                              # a plain declaration: `struct Foo bar;`
        items.append((start, end, CLIKE_TYPE_KIND[m.group(1)], m.group(2).decode("ascii")))
    for m in CLIKE_FUNC_ITEM.finditer(text):
        start = line_of(m.start())
        if kf[m.start(1)] not in (CALL, IDENT, TYPE):
            continue
        end = brace_extent(m.start(1), start, n_lines, line_of, opens, match, semis, pdepth, 4)
        if end is None or end <= start:
            continue
        items.append((start, end, K_FN, m.group(1).decode("ascii")))
    # the C function heuristic: a line at column 0 ending with { or ) that
    # contains ( and does not start with a statement keyword
    for i in range(n_lines):
        ln = lines[i]
        if not ln or line_indent[i] or ln[-1] not in b"{)" or b"(" not in ln:
            continue
        c0 = ln[0]
        if not (65 <= c0 <= 90 or 97 <= c0 <= 122 or c0 == 95):
            continue
        first = IDENT_RX.match(ln).group()
        if first in CLIKE_STMT_KW or first in (b"function", b"func"):
            continue
        nm = C_HEAD_NAME.match(ln)
        if nm is None or nm.group(1) in CLIKE_STMT_KW:
            continue
        a = int(line_off[i])
        if kf[a + nm.start(1)] not in (CALL, IDENT, TYPE):
            continue
        end = brace_extent(a, i, n_lines, line_of, opens, match, semis, pdepth, 4)
        if end is None or end <= i + (ln[-1] == 41):   # `foo(x)` alone needs a body below
            continue
        items.append((i, end, K_FN, nm.group(1).decode("ascii")))
    items.sort()
    return items


# ---------------------------------------------------------------- per file

_CTRL = bytes.maketrans(bytes(c for c in range(32) if c != 10) + b"\x7f", b"?" * 32)


def file_lang(relpath, head):
    base = os.path.basename(relpath).lower()
    ext = os.path.splitext(base)[1]
    if ext in EXT_LANG:
        return EXT_LANG[ext], EXT_LANG[ext]
    if head.startswith(b"#!"):
        first = head.split(b"\n", 1)[0]
        if b"python" in first:
            return "python", "python"
        if b"sh" in first:
            return "shell", "shell"
    return "text", TEXT_BASENAMES.get(base, TEXT_VARIANT.get(ext, "text"))


def index_file(task):
    """Worker: read, normalise, tokenize and find items of one file."""
    root, relpath = task
    path = os.path.join(root, relpath)
    size = os.path.getsize(path)
    if size > MAX_BYTES:
        return ("large", relpath, size)
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        return ("binary", relpath, len(data))
    text = data.decode("utf-8", "replace")
    if text.count("�") > 0.01 * max(len(text), 1):
        return ("binary", relpath, len(data))
    lang, variant = file_lang(relpath, data[:256])

    # normalise: tabs, printable ASCII only, strip trailing whitespace, cap
    text = text.replace("\r\n", "\n").replace("\r", "\n")   # CRLF and CR files
    raw = text.expandtabs(TAB).encode("ascii", "replace").translate(_CTRL)
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    lines = [ln.rstrip()[:MAX_COLS] for ln in lines]
    joined = b"\n".join(lines)
    n_lines = len(lines)
    line_len = np.fromiter((len(ln) for ln in lines), np.int64, n_lines)
    line_indent = np.fromiter((len(ln) - len(ln.lstrip(b" ")) for ln in lines), np.int64, n_lines)
    line_off = np.zeros(n_lines + 1, np.int64)   # offsets into joined (newlines included)
    np.cumsum(line_len + 1, out=line_off[1:])

    kinds = tokenize(joined, SPECS[variant])
    cf = np.frombuffer(joined, np.uint8)
    kf = np.frombuffer(bytes(kinds), np.uint8).copy()
    kf[(cf == 32) & (kf == PUNCT)] = SPACE

    def line_of(pos):
        return int(np.searchsorted(line_off, pos, side="right")) - 1

    if lang == "rust":
        items = rust_items(joined, cf, kf, line_of, n_lines)
    elif lang == "python":
        items = python_items(joined, kf, line_of, line_indent, line_len, n_lines)
    elif lang == "clike":
        items = clike_items(joined, cf, kf, lines, line_off, line_of, line_indent, n_lines)
    else:
        items = []

    keep = cf != 10
    chars = cf[keep]
    kinds = kf[keep]
    return ("ok", relpath, len(data), lang, chars.tobytes(), kinds.tobytes(),
            line_len.astype(np.uint16), line_indent.astype(np.uint16),
            int((chars != 32).sum()), items)


# ---------------------------------------------------------------- discovery

def git_toplevel(path):
    try:
        out = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True).stdout.strip()
        return out or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def git_files(path):
    """Tracked and untracked-but-not-ignored files under path (which lies in
    a git repo), relative to path. Submodules show up as directories."""
    out = subprocess.run(["git", "-C", path, "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard"], capture_output=True, check=True).stdout
    return [p.decode("utf-8", "replace") for p in out.split(b"\0") if p]


def walk_files(path):
    files = []
    for dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_NAMES
                             and not os.path.exists(os.path.join(dirpath, d, ".git")))
        for fn in filenames:
            files.append(os.path.relpath(os.path.join(dirpath, fn), path))
    return files


def discover(root, subdir, args, skipped):
    """Files to index under root/subdir, as paths relative to root."""
    full = os.path.join(root, subdir) if subdir else root
    toplevel = None if args.no_git else git_toplevel(full)
    rels = git_files(full) if toplevel else walk_files(full)
    result = []
    for rel in rels:
        fp = os.path.join(full, rel)
        parts = rel.split("/")
        if any(p in SKIP_NAMES for p in parts):
            continue
        if os.path.islink(fp):
            continue
        if os.path.isdir(fp):                     # a submodule or nested repo
            if args.submodules:
                result += discover(root, os.path.join(subdir, rel) if subdir else rel, args, skipped)
            else:
                skipped["submodules"] += 1
            continue
        if not os.path.isfile(fp):
            continue
        result.append(os.path.join(subdir, rel) if subdir else rel)
    return result


def excluded(rel, patterns):
    base = os.path.basename(rel)
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(base, p) for p in patterns)


def build_tree(relpaths):
    """Directory tree in pre-order with files first, then child directories,
    both sorted by name. Returns (dirs, ordered relpaths, file_dir)."""
    node = {"": {"files": [], "children": {}}}
    for rel in relpaths:
        parts = rel.split("/")
        d = ""
        for p in parts[:-1]:
            nd = p if d == "" else d + "/" + p
            if nd not in node:
                node[nd] = {"files": [], "children": {}}
                node[d]["children"][p] = nd
            d = nd
        node[d]["files"].append(rel)
    dirs, files, file_dir = [], [], []

    def visit(path, parent, depth):
        idx = len(dirs)
        dirs.append({"path": path, "parent": parent, "children": [], "files": [], "top": 0,
                     "depth": depth})
        for rel in sorted(node[path]["files"]):
            dirs[idx]["files"].append(len(files))
            files.append(rel)
            file_dir.append(idx)
        for name in sorted(node[path]["children"]):
            child = visit(node[path]["children"][name], idx, depth + 1)
            dirs[idx]["children"].append(child)
        return idx

    visit("", -1, 0)
    for d in dirs:
        if d["depth"] == 1:
            d["top"] = dirs.index(d)
        elif d["depth"] > 1:
            d["top"] = dirs[d["parent"]]["top"]
    for d in dirs:
        del d["depth"]
    return dirs, files, file_dir


# ---------------------------------------------------------------- main

def default_workers():
    return max(1, (os.cpu_count() or 2) // 2)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", nargs="+", metavar="SRC_DIR",
                    help="source tree(s); several are indexed as subtrees of their common parent")
    ap.add_argument("--out", default=None, help="output dir (default: data/<name>_atlas)")
    ap.add_argument("--name", default=None,
                    help="corpus name (default: from --out, else the source dir name)")
    ap.add_argument("--workers", type=int, default=default_workers(),
                    help="tokenizer processes (default: half the cores)")
    ap.add_argument("--exclude", nargs="+", action="extend", default=[], metavar="PATTERN",
                    help="fnmatch patterns on the relative path or file name to skip")
    ap.add_argument("--submodules", action="store_true",
                    help="descend into git submodules and nested repos (default: skip them)")
    ap.add_argument("--no-git", action="store_true",
                    help="walk the tree instead of asking git which files it tracks (for a tree "
                         "extracted under an ignored directory, such as a revision's cache)")
    args = ap.parse_args()

    srcs = [os.path.abspath(s) for s in args.src]
    for s in srcs:
        if not os.path.isdir(s):
            sys.exit(f"not a directory: {s}")
    if len(srcs) == 1:
        root, subdirs = srcs[0], [""]
    else:
        root = os.path.commonpath(srcs)
        subdirs = [os.path.relpath(s, root) for s in srcs]
        if any(sd == "." for sd in subdirs):
            sys.exit("with several source dirs none may contain another")
    if args.name is None:
        if args.out:
            name = os.path.basename(os.path.normpath(args.out))
            args.name = name[:-6] if name.endswith("_atlas") else name
        elif len(srcs) == 1:
            args.name = os.path.basename(root)
        else:
            args.name = os.path.basename(root) + "-" + "-".join(os.path.basename(s) for s in srcs)
    if args.out is None:
        args.out = os.path.join("data", f"{args.name}_atlas")

    t0 = time.time()
    skipped = {"binary": 0, "large": 0, "submodules": 0}
    relpaths = []
    for sd in subdirs:
        relpaths += discover(root, sd, args, skipped)
    relpaths = [r for r in dict.fromkeys(relpaths) if not excluded(r, args.exclude)]
    dirs, files, file_dir = build_tree(relpaths)
    print(f"{root}: {len(files)} files in {len(dirs)} dirs to index, {args.workers} workers")

    tasks = [(root, rel) for rel in files]
    results = [None] * len(tasks)
    done, nbytes = 0, 0

    def progress(res):
        nonlocal done, nbytes
        done += 1
        nbytes += res[2]
        if done % 20 == 0 or done == len(tasks):
            print(f"  {done}/{len(tasks)} files, {nbytes / 1e6:.1f} MB, {time.time() - t0:.1f}s",
                  end="\r", flush=True)

    if args.workers > 1 and len(tasks) > 1:
        with Pool(args.workers) as pool:
            for i, res in enumerate(pool.imap(index_file, tasks, chunksize=4)):
                results[i] = res
                progress(res)
    else:
        for i, task in enumerate(tasks):
            results[i] = index_file(task)
            progress(results[i])
    print()

    # drop skipped files from the tree (rebuild it from the survivors)
    kept = []
    for rel, res in zip(files, results):
        if res[0] == "ok":
            kept.append(rel)
        else:
            skipped[res[0]] += 1
    if len(kept) != len(files):
        by_rel = dict(zip(files, results))
        dirs, files, file_dir = build_tree(kept)
        results = [by_rel[rel] for rel in files]

    n_files = len(files)
    chars = b"".join(r[4] for r in results)
    kinds = b"".join(r[5] for r in results)
    line_len = np.concatenate([r[6] for r in results]) if n_files else np.zeros(0, np.uint16)
    line_indent = np.concatenate([r[7] for r in results]) if n_files else np.zeros(0, np.uint16)
    n_lines = len(line_len)
    lines_per_file = np.array([len(r[6]) for r in results], np.int64)
    file_line0 = np.zeros(n_files + 1, np.uint32)
    file_line0[1:] = np.cumsum(lines_per_file)
    line_off = np.zeros(n_lines + 1, np.uint64)
    np.cumsum(line_len.astype(np.uint64), out=line_off[1:])
    line_file = np.repeat(np.arange(n_files, dtype=np.uint32), lines_per_file)
    file_chars = np.array([r[8] for r in results], np.uint32)

    items = []
    for fi, r in enumerate(results):
        for (s, e, k, nm) in r[9]:
            items.append({"file": fi, "start": int(s), "end": int(e), "kind": int(k), "name": nm})
    item_file = np.array([it["file"] for it in items], np.uint32)
    item_start = np.array([it["start"] for it in items], np.uint32)
    item_end = np.array([it["end"] for it in items], np.uint32)
    item_kind = np.array([it["kind"] for it in items], np.uint8)

    os.makedirs(args.out, exist_ok=True)
    np.savez(os.path.join(args.out, "index.npz"),
             chars=np.frombuffer(chars, np.uint8), kinds=np.frombuffer(kinds, np.uint8),
             line_off=line_off, line_file=line_file, line_indent=line_indent, line_len=line_len,
             file_line0=file_line0, file_chars=file_chars,
             file_dir=np.array(file_dir, np.uint32),
             item_file=item_file, item_start=item_start, item_end=item_end, item_kind=item_kind)

    seconds = round(time.time() - t0, 2)
    meta = {
        "root": root, "name": args.name,
        "files": [{"path": rel, "lang": r[3], "lines": int(len(r[6])), "bytes": int(r[2])}
                  for rel, r in zip(files, results)],
        "dirs": dirs,
        "items": items,
        "kinds": KINDS,
        "stats": {"files": n_files, "dirs": len(dirs), "lines": int(n_lines), "chars": len(chars),
                  "items": len(items), "skipped_binary": skipped["binary"],
                  "skipped_large": skipped["large"], "skipped_submodules": skipped["submodules"],
                  "seconds": seconds},
    }
    with open(os.path.join(args.out, "index.json"), "w") as f:
        json.dump(meta, f)
    langs = {}
    for r in results:
        langs[r[3]] = langs.get(r[3], 0) + 1
    print(f"wrote {args.out}/index.npz + index.json: {n_files} files ({langs}), {len(dirs)} dirs, "
          f"{n_lines} lines, {len(chars) / 1e6:.2f} M chars, {len(items)} items, "
          f"skipped {skipped}, {seconds}s")


if __name__ == "__main__":
    main()
