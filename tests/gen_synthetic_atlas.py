#!/usr/bin/env python3
"""Synthetic corpus for testing the viewer without atlas_index/atlas_layout.

Writes data/synthetic_atlas/{index.npz, index.json, layout.npz, layout.json}
in exactly the formats of docs/DESIGN.md: about 40 fake files in 6
directories at two depths, generated code-like lines with per-character
kinds and real words (so the text rung is readable), a few items per file,
and a binary-partition treemap with the spec's file column layout. The
treemap is not squarified; it only has to satisfy the formats and the
invariants checked by tests/check_layout.py.

    ./tests/gen_synthetic_atlas.py [--out data/synthetic_atlas] [--seed 1]
"""
import argparse
import json
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    from atlas_font import font_box, DEFAULT_FONT
except Exception:                                   # noqa: BLE001
    font_box = None

KINDS = ["space", "ident", "keyword", "type", "string", "comment", "number",
         "punct", "macro", "call"]
SPACE, IDENT, KEYWORD, TYPE, STRING, COMMENT, NUMBER, PUNCT, MACRO, CALL = range(10)

WORDS = ("window buffer render solve matrix vertex index camera frame layer "
         "shader atlas pixel glyph offset scale width height count total "
         "value result item node child parent path name data size line "
         "column row cell zoom level tile token text color alpha depth "
         "world view rect point delta time step mode state flag handle").split()
TYPES = ("Window Buffer Renderer Matrix Vertex Camera Frame Layer Shader "
         "Atlas Pixel Glyph Rect Point Vec2 Vec3 Node Item State Handle "
         "Texture Program Layout Index Result Option String Error").split()
RUST_KW = "let mut pub fn struct impl for in if else return match while use".split()
PY_KW = "def class return if else for in while import from with as pass self".split()
SH_KW = "if then else fi for do done in echo export local".split()

# directory tree: (path, parent index) in depth-first order; root first
DIRS = [("", -1), ("core", 0), ("core/math", 1), ("render", 0),
        ("render/gl", 3), ("tools", 0), ("tools/scripts", 5)]
FILES = {                       # dir -> file names (sorted by the generator)
    "": ["README.md", "build.py"],
    "core": ["camera.rs", "index.rs", "item.rs", "layout.rs", "state.rs", "world.rs"],
    "core/math": ["matrix.rs", "point.rs", "rect.rs", "solve.rs", "vec2.rs", "vec3.rs"],
    "render": ["atlas.rs", "frame.rs", "glyph.rs", "layer.rs", "pipeline.rs",
               "shader.rs", "texture.rs"],
    "render/gl": ["blit.c", "lines.glsl", "quad.glsl", "text.glsl", "upload.c"],
    "tools": ["bench.py", "check.py", "export.py", "fetch.py", "profile.py", "stats.py"],
    "tools/scripts": ["build.sh", "clean.sh", "deploy.sh", "gen.py", "lint.sh",
                      "pack.py", "run.sh", "test.py"],
}


def lang_of(name):
    ext = name.rsplit(".", 1)[-1]
    return {"rs": "rust", "py": "python", "sh": "shell", "c": "c",
            "glsl": "c", "md": "text"}.get(ext, "text")


# ---------------------------------------------------------------- lines

def seg(rng, lang, indent):
    """One code-like line as a list of (text, kind) segments."""
    kw = {"rust": RUST_KW, "python": PY_KW, "shell": SH_KW}.get(lang, RUST_KW)
    s = [(" " * indent, SPACE)] if indent else []
    r = rng.random()
    if r < 0.12:
        mark = "#" if lang in ("python", "shell") else "//"
        words = " ".join(rng.choice(WORDS) for _ in range(rng.integers(2, 7)))
        s.append((f"{mark} {words}", COMMENT))
        return s
    if r < 0.22 and lang == "rust":
        s += [("let", KEYWORD), (" ", SPACE), (rng.choice(WORDS), IDENT),
              (" = ", PUNCT), (rng.choice(TYPES), TYPE), ("::", PUNCT),
              (rng.choice(["new", "from", "with"]), CALL), ("(", PUNCT),
              (str(rng.integers(0, 4096)), NUMBER), (");", PUNCT)]
        return s
    if r < 0.32:
        s += [(rng.choice(WORDS), IDENT), (".", PUNCT), (rng.choice(WORDS), CALL),
              ("(", PUNCT), ('"' + " ".join(rng.choice(WORDS) for _ in range(2)) + '"', STRING),
              (", ", PUNCT), (rng.choice(WORDS), IDENT), (")", PUNCT)]
        s.append((";" if lang == "rust" else "", PUNCT))
        return s
    if r < 0.42:
        s += [(rng.choice(kw[:4]), KEYWORD), (" ", SPACE), (rng.choice(WORDS), IDENT),
              (" ", SPACE), (rng.choice(["+=", "-=", "*=", "="]), PUNCT), (" ", SPACE),
              (rng.choice(WORDS), IDENT), (" * ", PUNCT),
              (f"{rng.random() * 10:.2f}", NUMBER)]
        s.append((";" if lang == "rust" else "", PUNCT))
        return s
    if r < 0.5 and lang == "rust":
        s += [("#[", MACRO), (rng.choice(["inline", "derive(Clone)", "allow(unused)"]), MACRO),
              ("]", MACRO)]
        return s
    if r < 0.6:
        s += [(rng.choice(["if", "for", "while"]), KEYWORD), (" ", SPACE),
              (rng.choice(WORDS), IDENT), (" ", SPACE), (rng.choice(["<", ">", "==", "!="]), PUNCT),
              (" ", SPACE), (str(rng.integers(0, 100)), NUMBER),
              (" {" if lang != "python" else ":", PUNCT)]
        return s
    n = rng.integers(2, 6)
    for i in range(n):
        s.append((rng.choice(WORDS + TYPES[:6]), IDENT if rng.random() < 0.7 else TYPE))
        s.append((rng.choice([", ", " + ", " ", "::", "."]), PUNCT))
    s.append((rng.choice(WORDS), IDENT))
    if lang == "rust":
        s.append((rng.choice([";", ".unwrap();", ")?;"]), PUNCT))
    return s


def item_header(rng, lang, name, kind):
    if lang == "python":
        if kind == 2:
            return [("class", KEYWORD), (" ", SPACE), (name, CALL), (":", PUNCT)]
        return [("def", KEYWORD), (" ", SPACE), (name, CALL), ("(", PUNCT),
                ("self", KEYWORD), (", ", PUNCT), (rng.choice(WORDS), IDENT), ("):", PUNCT)]
    if lang == "shell":
        return [(name, CALL), ("() {", PUNCT)]
    if kind == 2:
        return [("pub", KEYWORD), (" ", SPACE), ("struct", KEYWORD), (" ", SPACE),
                (name, CALL), (" {", PUNCT)]
    if kind == 3:
        return [("pub", KEYWORD), (" ", SPACE), ("enum", KEYWORD), (" ", SPACE),
                (name, CALL), (" {", PUNCT)]
    if kind == 4:
        return [("impl", KEYWORD), (" ", SPACE), (name, TYPE), (" {", PUNCT)]
    if lang == "c":
        return [(rng.choice(["void", "int", "float"]), KEYWORD), (" ", SPACE),
                (name, CALL), ("(", PUNCT), (rng.choice(TYPES), TYPE), (" *", PUNCT),
                (rng.choice(WORDS), IDENT), (") {", PUNCT)]
    return [("pub", KEYWORD), (" ", SPACE), ("fn", KEYWORD), (" ", SPACE), (name, CALL),
            ("(&", PUNCT), ("self", KEYWORD), (", ", PUNCT), (rng.choice(WORDS), IDENT),
            (": ", PUNCT), (rng.choice(TYPES), TYPE), (") -> ", PUNCT),
            (rng.choice(TYPES), TYPE), (" {", PUNCT)]


def gen_file(rng, lang, n_lines):
    """-> lines (list of segment lists), items [(start, end, kind, name)]."""
    lines, items = [], []
    if lang == "text":
        while len(lines) < n_lines:
            r = rng.random()
            if r < 0.15:
                lines.append([("# " + " ".join(rng.choice(WORDS) for _ in range(3)).title(), COMMENT)])
            elif r < 0.3:
                lines.append([])
            else:
                lines.append([(" ".join(rng.choice(WORDS) for _ in range(rng.integers(4, 14))), IDENT)])
        return lines[:n_lines], items
    while len(lines) < n_lines:
        r = rng.random()
        if r < 0.1:
            lines.append([])
            continue
        if r < 0.18:
            lines.append(seg(rng, lang, 0))
            continue
        kind = int(rng.choice([1, 1, 1, 2, 3, 4])) if lang == "rust" else \
            (int(rng.choice([1, 1, 2])) if lang == "python" else 1)
        name = rng.choice(TYPES) + rng.choice(["", "s", "Set"]) if kind in (2, 3, 4) \
            else rng.choice(WORDS) + "_" + rng.choice(WORDS)
        start = len(lines)
        lines.append(item_header(rng, lang, name, kind))
        body = int(rng.integers(3, 30))
        depth = 1
        for _ in range(body):
            rr = rng.random()
            if rr < 0.12 and depth < 3:
                lines.append(seg(rng, lang, 4 * depth)[:1] + [
                    ("if", KEYWORD), (" ", SPACE), (rng.choice(WORDS), IDENT),
                    (" {" if lang != "python" else ":", PUNCT)])
                depth += 1
            elif rr < 0.24 and depth > 1:
                depth -= 1
                if lang != "python":
                    lines.append([(" " * (4 * depth), SPACE), ("}", PUNCT)])
            else:
                lines.append(seg(rng, lang, 4 * depth))
        while depth > 1:
            depth -= 1
            if lang != "python":
                lines.append([(" " * (4 * depth), SPACE), ("}", PUNCT)])
        if lang != "python":
            lines.append([("}", PUNCT)])
        items.append((start, len(lines), kind, name))
        lines.append([])
    lines = lines[:n_lines]
    items = [(s, min(e, n_lines), k, n) for s, e, k, n in items if s < n_lines]
    return lines, items


def flatten(segs):
    text = "".join(t for t, _ in segs).rstrip()
    kinds = []
    for t, k in segs:
        kinds += [k] * len(t)
    kinds = kinds[:len(text)]
    for i, ch in enumerate(text):        # spaces inside punct runs count as space
        if ch == " ":
            kinds[i] = SPACE
    return text, kinds


# ---------------------------------------------------------------- layout

def pad_of(w, h):
    return min(max(0.015 * min(w, h), 0.05), 4.0)


def partition(rect, entries):
    """Binary weighted partition of rect among entries [(weight, key)];
    returns {key: rect}. Splits along the longer side."""
    x0, y0, x1, y1 = rect
    if len(entries) == 1:
        return {entries[0][1]: rect}
    total = sum(w for w, _ in entries)
    acc, i = 0.0, 0
    while i < len(entries) - 1 and acc + entries[i][0] < total / 2:
        acc += entries[i][0]
        i += 1
    a, b = entries[:max(i, 1)], entries[max(i, 1):]
    fa = sum(w for w, _ in a) / total
    if (x1 - x0) >= (y1 - y0):
        xm = x0 + (x1 - x0) * fa
        ra, rb = (x0, y0, xm, y1), (xm, y0, x1, y1)
    else:
        ym = y0 + (y1 - y0) * fa
        ra, rb = (x0, y0, x1, ym), (x0, ym, x1, y1)
    out = partition(ra, a)
    out.update(partition(rb, b))
    return out


def file_columns(w, h, n_lines, lens, A):
    """Column choice: -> (k, rows, pitch, cap, colw). cap falls as k grows
    (cap ~ w*n / (h*k*k)), so the wrapped-columns intent of the spec is the
    largest k whose cap still reaches the target; if none does, k = 1."""
    if n_lines == 0:
        n_lines = 1
    target = float(np.clip(np.percentile(lens, 90) if len(lens) else 24, 24, 100))
    best = None
    for k in range(1, 65):
        rows = math.ceil(n_lines / k)
        p = h / (rows + 2)
        c = w / (k + 0.06 * (k - 1))           # text width of one column
        cap = int(c / (p * A)) if p > 0 else 0
        colw = c * 1.06
        cand = (k, rows, p, cap, colw)
        if cap >= target or k == 1:
            best = cand
        else:
            break
    return best


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/synthetic_atlas")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--aspect", default="16:9")
    ap.add_argument("--repeat", type=int, default=1,
                    help="repeat every file name N times (a bigger corpus for timing)")
    args = ap.parse_args()
    t0 = time.time()
    rng = np.random.default_rng(args.seed)
    A = 0.6
    if font_box is not None:
        try:
            asc, desc, adv, _, _ = font_box(DEFAULT_FONT, 0)
            A = adv / (asc + desc)
        except Exception:                        # noqa: BLE001
            pass

    # ---- corpus
    dir_index = {p: i for i, (p, _) in enumerate(DIRS)}
    files, file_dir = [], []
    for dpath, _ in DIRS:
        names = sorted(FILES[dpath])
        if args.repeat > 1:
            names = sorted(f"{n.rsplit('.', 1)[0]}_{k}.{n.rsplit('.', 1)[1]}"
                           for n in names for k in range(args.repeat))
        for name in names:
            files.append(f"{dpath}/{name}" if dpath else name)
            file_dir.append(dir_index[dpath])
    chars_all, kinds_all, line_file, line_indent, line_len = [], [], [], [], []
    file_line0, file_chars = [0], []
    items_json, item_file, item_start, item_end, item_kind = [], [], [], [], []
    file_json = []
    for fi, path in enumerate(files):
        lang = lang_of(path)
        n = int(rng.integers(20, 400)) if rng.random() < 0.8 else int(rng.integers(400, 1200))
        lines, items = gen_file(rng, lang, n)
        nonspace = 0
        nbytes = 0
        for segs in lines:
            text, kinds = flatten(segs)
            text = text[:4096]
            kinds = kinds[:4096]
            chars_all.append(np.frombuffer(text.encode("ascii"), np.uint8))
            kinds_all.append(np.array(kinds, np.uint8))
            line_file.append(fi)
            line_indent.append(len(text) - len(text.lstrip(" ")))
            line_len.append(len(text))
            nonspace += sum(1 for ch in text if ch != " ")
            nbytes += len(text) + 1
        file_line0.append(file_line0[-1] + len(lines))
        file_chars.append(nonspace)
        file_json.append({"path": path, "lang": lang, "lines": len(lines), "bytes": nbytes})
        for s, e, k, name in items:
            items_json.append({"file": fi, "start": s, "end": e, "kind": k, "name": name})
            item_file.append(fi)
            item_start.append(s)
            item_end.append(e)
            item_kind.append(k)
    chars = np.concatenate(chars_all) if chars_all else np.zeros(0, np.uint8)
    kinds = np.concatenate(kinds_all) if kinds_all else np.zeros(0, np.uint8)
    line_len = np.array(line_len, np.uint16)
    line_off = np.zeros(len(line_len) + 1, np.uint64)
    line_off[1:] = np.cumsum(line_len.astype(np.uint64))
    n_files, n_lines = len(files), len(line_len)

    def top_of(d):
        while DIRS[d][1] > 0:
            d = DIRS[d][1]
        return d if DIRS[d][1] == 0 else 0

    dirs_json = []
    for di, (p, parent) in enumerate(DIRS):
        dirs_json.append({"path": p, "parent": parent,
                          "children": [j for j, (_, pp) in enumerate(DIRS) if pp == di],
                          "files": [f for f in range(n_files) if file_dir[f] == di],
                          "top": top_of(di)})

    # ---- layout
    a_w, a_h = (float(v) for v in args.aspect.split(":"))
    W, H = 1600.0, 1600.0 * a_h / a_w
    n_dirs = len(DIRS)
    dir_rect = np.zeros((n_dirs, 4)); dir_pad = np.zeros(n_dirs)
    dir_depth = np.zeros(n_dirs, np.uint16); dir_hue = np.zeros(n_dirs, np.uint8)
    file_rect = np.zeros((n_files, 4))
    weight = np.array(file_chars, np.float64) + 1.0

    def dir_weight(d):
        return sum(weight[f] for f in dirs_json[d]["files"]) + \
            sum(dir_weight(c) for c in dirs_json[d]["children"])

    def lay(d, rect, depth):
        dir_rect[d] = rect
        dir_depth[d] = depth
        dir_hue[d] = dirs_json[d]["top"] % 12
        x0, y0, x1, y1 = rect
        pad = pad_of(x1 - x0, y1 - y0)
        dir_pad[d] = pad
        inner = (x0 + pad, y0 + pad, x1 - pad, y1 - pad)
        entries = [(weight[f], ("f", f)) for f in dirs_json[d]["files"]]
        entries += [(dir_weight(c), ("d", c)) for c in dirs_json[d]["children"]]
        entries.sort(key=lambda e: -e[0])
        for key, r in partition(inner, entries).items():
            if key[0] == "f":
                file_rect[key[1]] = r
            else:
                lay(key[1], r, depth + 1)

    lay(0, (0.0, 0.0, W, H), 0)
    file_pitch = np.zeros(n_files); file_cols = np.zeros(n_files, np.uint16)
    file_rows = np.zeros(n_files, np.uint32); file_cap = np.zeros(n_files, np.uint16)
    file_colw = np.zeros(n_files); file_hue = np.zeros(n_files, np.uint8)
    line_pos = np.zeros((n_lines, 2), np.float32)
    item_rect, item_rect_item = [], []
    for f in range(n_files):
        x0, y0, x1, y1 = file_rect[f]
        l0, l1 = file_line0[f], file_line0[f + 1]
        k, rows, p, cap, colw = file_columns(x1 - x0, y1 - y0, l1 - l0, line_len[l0:l1], A)
        file_pitch[f], file_cols[f], file_rows[f] = p, k, rows
        file_cap[f], file_colw[f] = cap, colw
        file_hue[f] = dir_hue[file_dir[f]]
        j = np.arange(l1 - l0)
        c, r = j // rows, j % rows
        line_pos[l0:l1, 0] = x0 + c * colw
        line_pos[l0:l1, 1] = y0 + p + r * p
    for ii in range(len(item_file)):
        f = item_file[ii]
        x0, y0, x1, y1 = file_rect[f]
        rows, p, colw = int(file_rows[f]), file_pitch[f], file_colw[f]
        c_text = colw / 1.06
        s, e = item_start[ii], item_end[ii]
        for c in range(s // rows, (e - 1) // rows + 1):
            r0 = max(s, c * rows) - c * rows
            r1 = min(e, (c + 1) * rows) - c * rows
            item_rect.append((x0 + c * colw, y0 + p + r0 * p,
                              x0 + c * colw + c_text, y0 + p + r1 * p))
            item_rect_item.append(ii)
    item_rect = np.array(item_rect, np.float32).reshape(-1, 4)

    os.makedirs(args.out, exist_ok=True)
    np.savez(os.path.join(args.out, "index.npz"),
             chars=chars, kinds=kinds, line_off=line_off,
             line_file=np.array(line_file, np.uint32),
             line_indent=np.array(line_indent, np.uint16), line_len=line_len,
             file_line0=np.array(file_line0, np.uint32),
             file_chars=np.array(file_chars, np.uint32),
             file_dir=np.array(file_dir, np.uint32),
             item_file=np.array(item_file, np.uint32),
             item_start=np.array(item_start, np.uint32),
             item_end=np.array(item_end, np.uint32),
             item_kind=np.array(item_kind, np.uint8))
    index = {"root": os.path.abspath(args.out), "name": "synthetic",
             "files": file_json, "dirs": dirs_json, "items": items_json, "kinds": KINDS,
             "stats": {"files": n_files, "dirs": n_dirs, "lines": n_lines,
                       "chars": int(chars.size), "items": len(item_file),
                       "skipped_binary": 0, "skipped_large": 0,
                       "seconds": round(time.time() - t0, 3)}}
    with open(os.path.join(args.out, "index.json"), "w") as fh:
        json.dump(index, fh)
    np.savez(os.path.join(args.out, "layout.npz"),
             world=np.array([W, H]), dir_rect=dir_rect, dir_pad=dir_pad,
             dir_depth=dir_depth, dir_hue=dir_hue, file_rect=file_rect,
             file_pitch=file_pitch, file_cols=file_cols, file_rows=file_rows,
             file_cap=file_cap, file_colw=file_colw, file_hue=file_hue,
             line_pos=line_pos, item_rect=item_rect,
             item_rect_item=np.array(item_rect_item, np.uint32))
    layout = {"world": [W, H], "aspect": args.aspect, "metric": "chars", "char_aspect": A,
              "dirs": [{"label": (p.rsplit("/", 1)[-1] + "/") if p else "synthetic",
                        "rect": list(map(float, dir_rect[i])), "depth": int(dir_depth[i]),
                        "hue": int(dir_hue[i])} for i, (p, _) in enumerate(DIRS)]}
    with open(os.path.join(args.out, "layout.json"), "w") as fh:
        json.dump(layout, fh)
    print(f"{args.out}: {n_files} files, {n_dirs} dirs, {n_lines} lines, "
          f"{chars.size} chars, {len(item_file)} items, {len(item_rect)} item rects, "
          f"char_aspect {A:.3f}, pitch {file_pitch.min():.3f}..{file_pitch.max():.3f}")



if __name__ == "__main__":
    main()
    # the real layout stage on the synthetic index, so the viewer's row
    # format (wrapped rows, one layout per metric) is what the tests see
    import subprocess
    out = "data/synthetic_atlas"
    for i, a in enumerate(sys.argv[1:]):
        if a == "--out":
            out = sys.argv[i + 2]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    subprocess.run([sys.executable, os.path.join(root, "atlas_layout.py"), out], check=True)
