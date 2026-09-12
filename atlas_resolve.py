#!/usr/bin/env python3
"""Stage 6: resolve names. Parses every file of an atlas with tree-sitter,
collects the entities it defines and the identifiers it uses, and resolves
each use by scope, file, imports and uniqueness, with a status that says how
far that got. Rust is covered fully; Python and C get functions, types,
parameters and locals. No type inference: this is a lexical resolver, as in
the code atlas's "lexical references recognised".

    ./atlas_resolve.py data/<name>_atlas [--workers N]

writes resolve.npz and resolve.json beside the index (see docs/DESIGN.md,
Stage 6). The viewer uses them for the filter, the hover label, the
Inspector, and atlas_layout.py --metric references.
"""
import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

KINDS = ["fn", "method", "struct", "field", "enum", "variant", "union", "trait", "type",
         "const", "static", "mod", "macro", "generic", "param", "local", "class"]
K = {k: i for i, k in enumerate(KINDS)}
STATUSES = ["local", "file", "import", "crate", "global", "generic", "self", "method",
            "ambiguous", "external", "import missing", "macro missing", "self unresolved",
            "not found"]
S = {s: i for i, s in enumerate(STATUSES)}

TYPE_KINDS = {K["struct"], K["enum"], K["union"], K["trait"], K["type"], K["generic"], K["class"]}
VALUE_KINDS = {K["fn"], K["method"], K["const"], K["static"], K["variant"], K["mod"],
               K["param"], K["local"], K["struct"], K["class"]}   # tuple structs are called
FIELD_KINDS = {K["field"], K["method"], K["variant"], K["fn"]}
CTX_KINDS = {"value": VALUE_KINDS, "type": TYPE_KINDS, "field": FIELD_KINDS,
             "macro": {K["macro"]}, "import": None, "self": TYPE_KINDS}

STD_CRATES = {"std", "core", "alloc", "proc_macro", "test"}
STD_MACROS = {"println", "print", "eprintln", "eprint", "format", "vec", "assert",
              "assert_eq", "assert_ne", "debug_assert", "debug_assert_eq", "panic", "todo",
              "unimplemented", "unreachable", "write", "writeln", "matches", "include_str",
              "include_bytes", "include", "concat", "stringify", "env", "option_env", "cfg",
              "dbg", "thread_local", "format_args", "line", "file", "column", "module_path",
              "compile_error", "macro_rules", "vec_deque", "log", "error", "warn", "info",
              "debug", "trace", "lazy_static", "bitflags", "json", "quote", "parse_quote"}
PY_BUILTINS = set("""abs all any ascii bin bool bytearray bytes callable chr classmethod
compile complex delattr dict dir divmod enumerate eval exec filter float format frozenset
getattr globals hasattr hash help hex id input int isinstance issubclass iter len list
locals map max memoryview min next object oct open ord pow print property range repr
reversed round set setattr slice sorted staticmethod str sum super tuple type vars zip
self cls None True False Exception ValueError TypeError KeyError IndexError RuntimeError
StopIteration OSError IOError NotImplementedError AttributeError ImportError""".split())

LANG_OF = {"rust": "rust", "python": "python", "clike": "c"}


# ----------------------------------------------------------------- parsing

_parsers = {}


def parser_for(lang):
    if lang not in _parsers:
        from tree_sitter import Language, Parser
        if lang == "rust":
            import tree_sitter_rust as m
        elif lang == "python":
            import tree_sitter_python as m
        else:
            import tree_sitter_c as m
        _parsers[lang] = Parser(Language(m.language()))
    return _parsers[lang]


def text(src, node):
    return src[node.start_byte:node.end_byte].decode("utf-8", "replace")


class FileWalk:
    """One pass over a file's tree. Entities and references are collected
    with file-relative line, index column and length; locals, generics,
    Self and same-file names are resolved here, the rest in the parent."""

    def __init__(self, src, lang):
        self.src = src
        self.lang = lang
        self.lines = src.split(b"\n")
        self.ents = []          # [line, col, len, kind, name, scope]
        self.refs = []          # [line, col, len, name, ctx, root, parent, scope_fn, ent, status]
        self.imports = {}       # name -> (root, path list)
        self.globs = []         # roots of `use x::*`
        self.fn_stack = []      # (entity index, {local name: entity index})
        self.gen_stack = []     # {generic name: entity index}
        self.impl_stack = []    # type name of the enclosing impl (or "")
        self.scope_stack = []   # entity index of the enclosing struct/enum/trait/mod/fn
        self.by_name = defaultdict(list)

    # positions: tree-sitter columns are bytes in the original line; the
    # index expanded tabs to 4 and made every character one column
    def col_of(self, node):
        row, bc = node.start_point
        prefix = self.lines[row][:bc] if row < len(self.lines) else b""
        return len(prefix.decode("utf-8", "replace").expandtabs(4))

    def add_ent(self, node, kind, name=None):
        name = name if name is not None else text(self.src, node)
        i = len(self.ents)
        scope = self.scope_stack[-1] if self.scope_stack else -1
        self.ents.append([node.start_point[0], self.col_of(node),
                          node.end_point[1] - node.start_point[1], K[kind], name, scope])
        self.by_name[name].append(i)
        return i

    def add_ref(self, node, ctx, root="", parent="", name=None):
        name = name if name is not None else text(self.src, node)
        fn = self.fn_stack[-1][0] if self.fn_stack else -1
        self.refs.append([node.start_point[0], self.col_of(node),
                          node.end_point[1] - node.start_point[1], name, ctx, root, parent,
                          fn, -1, -1])
        return self.refs[-1]

    # ---- rust ---------------------------------------------------------
    def rust(self, n):
        t = n.type
        if t in ("line_comment", "block_comment", "string_literal", "raw_string_literal",
                 "char_literal", "primitive_type", "self", "super", "crate", "lifetime",
                 "integer_literal", "float_literal", "boolean_literal", "attribute_item",
                 "inner_attribute_item"):
            return
        if t in ("function_item", "function_signature_item"):
            name = n.child_by_field_name("name")
            kind = "method" if self.impl_stack or (self.scope_stack and
                                                    self.ents[self.scope_stack[-1]][3] == K["trait"]) else "fn"
            i = self.add_ent(name, kind) if name else -1
            if i >= 0 and self.impl_stack:
                self.ents[i].append(self.impl_stack[-1])     # impl_of, index 6
            self.fn_stack.append((i, {}))
            self.scope_stack.append(i)
            self.gen_stack.append({})
            for c in n.children:
                if c is not name:
                    self.rust(c)
            self.gen_stack.pop(); self.scope_stack.pop(); self.fn_stack.pop()
            return
        if t in ("struct_item", "enum_item", "union_item", "trait_item", "type_item"):
            name = n.child_by_field_name("name")
            i = self.add_ent(name, t.split("_")[0]) if name else -1
            self.scope_stack.append(i)
            self.gen_stack.append({})
            for c in n.children:
                if c is not name:
                    self.rust(c)
            self.gen_stack.pop(); self.scope_stack.pop()
            return
        if t == "impl_item":
            ty = n.child_by_field_name("type")
            base = ty
            while base is not None and base.type in ("generic_type", "reference_type", "scoped_type_identifier"):
                base = base.child_by_field_name("type") if base.type != "scoped_type_identifier" \
                    else base.child_by_field_name("name")
            self.impl_stack.append(text(self.src, base) if base is not None else "")
            self.gen_stack.append({})
            for c in n.children:
                self.rust(c)
            self.gen_stack.pop(); self.impl_stack.pop()
            return
        if t == "mod_item":
            name = n.child_by_field_name("name")
            i = self.add_ent(name, "mod") if name else -1
            self.scope_stack.append(i)
            for c in n.children:
                if c is not name:
                    self.rust(c)
            self.scope_stack.pop()
            return
        if t in ("const_item", "static_item"):
            name = n.child_by_field_name("name")
            if name:
                self.add_ent(name, t.split("_")[0])
            for c in n.children:
                if c is not name:
                    self.rust(c)
            return
        if t == "macro_definition":
            name = n.child_by_field_name("name")
            if name:
                self.add_ent(name, "macro")
            return
        if t == "field_declaration":
            name = n.child_by_field_name("name")
            if name:
                self.add_ent(name, "field")
            for c in n.children:
                if c is not name:
                    self.rust(c)
            return
        if t == "enum_variant":
            name = n.child_by_field_name("name")
            if name:
                self.add_ent(name, "variant")
            for c in n.children:
                if c is not name:
                    self.rust(c)
            return
        if t == "type_parameter":
            name = n.child_by_field_name("name") or (n.children[0] if n.children else None)
            if name is not None and name.type == "type_identifier":
                i = self.add_ent(name, "generic")
                if self.gen_stack:
                    self.gen_stack[-1][text(self.src, name)] = i
            for c in n.children:
                if c is not name:
                    self.rust(c)
            return
        if t in ("parameter", "closure_parameters"):
            pat = n.child_by_field_name("pattern") if t == "parameter" else None
            targets = [pat] if pat is not None else [c for c in n.children if c.is_named]
            for p in targets:
                self.bind(p, "param")
            ty = n.child_by_field_name("type")
            if ty is not None:
                self.rust(ty)
            return
        if t == "let_declaration":
            pat = n.child_by_field_name("pattern")
            if pat is not None:
                self.bind(pat, "local")
            for c in n.children:
                if c is not pat:
                    self.rust(c)
            return
        if t in ("for_expression", "if_let_expression", "while_let_expression", "let_condition"):
            pat = n.child_by_field_name("pattern")
            if pat is not None:
                self.bind(pat, "local")
            for c in n.children:
                if c is not pat:
                    self.rust(c)
            return
        if t == "match_arm":
            pat = n.child_by_field_name("pattern")
            if pat is not None:
                self.bind(pat, "local")
            for c in n.children:
                if c is not pat:
                    self.rust(c)
            return
        if t == "use_declaration":
            arg = n.child_by_field_name("argument")
            if arg is not None:
                self.use_tree(arg, [])
            return
        if t == "macro_invocation":
            name = n.child_by_field_name("macro")
            if name is not None:
                if name.type == "scoped_identifier":
                    last = name.child_by_field_name("name")
                    self.add_ref(last, "macro", root=self.path_root(name))
                else:
                    self.add_ref(name, "macro")
            for c in n.children:
                if c is not name:
                    self.rust(c)
            return
        if t == "field_expression":
            val = n.child_by_field_name("value")
            fld = n.child_by_field_name("field")
            if val is not None:
                self.rust(val)
            if fld is not None and fld.type == "field_identifier":
                self.add_ref(fld, "field")
            return
        if t == "field_initializer":
            fld = n.child_by_field_name("field")
            if fld is not None:
                self.add_ref(fld, "field")
            for c in n.children:
                if c is not fld:
                    self.rust(c)
            return
        if t == "shorthand_field_initializer":
            for c in n.children:
                if c.type == "identifier":
                    self.add_ref(c, "field")
            return
        if t in ("scoped_identifier", "scoped_type_identifier"):
            last = n.child_by_field_name("name")
            path = n.child_by_field_name("path")
            root = self.path_root(n)
            parent = text(self.src, path.child_by_field_name("name")) if path is not None and \
                path.type in ("scoped_identifier", "scoped_type_identifier") else \
                (text(self.src, path) if path is not None and path.type in ("identifier", "type_identifier") else "")
            if last is not None:
                ctx = "type" if t == "scoped_type_identifier" else "value"
                self.ident_ref(last, ctx, root=root, parent=parent)
            # generic arguments inside the path
            for c in n.children:
                if c.type == "generic_type" or c.type == "type_arguments":
                    self.rust(c)
            return
        if t == "identifier":
            self.ident_ref(n, "value")
            return
        if t == "type_identifier":
            self.ident_ref(n, "type")
            return
        for c in n.children:
            self.rust(c)

    def path_root(self, n):
        while n is not None and n.type in ("scoped_identifier", "scoped_type_identifier"):
            n = n.child_by_field_name("path")
        return text(self.src, n) if n is not None else ""

    def bind(self, pat, kind):
        """Lowercase identifiers inside a pattern bind locals; the rest
        (enum variants, consts, paths) are references."""
        if pat.type in ("identifier",):
            name = text(self.src, pat)
            if name[:1].islower() or name[:1] == "_":
                i = self.add_ent(pat, kind)
                if self.fn_stack:
                    self.fn_stack[-1][1][name] = i
            else:
                self.ident_ref(pat, "value")
            return
        if pat.type in ("scoped_identifier", "scoped_type_identifier"):
            self.rust(pat)
            return
        for c in pat.children:
            if c.is_named:
                self.bind(c, kind)

    def use_tree(self, n, path):
        t = n.type
        if t == "scoped_identifier":
            segs = self.segments(n)
            self.import_name(segs[-1], segs, n.child_by_field_name("name"))
        elif t == "identifier" or t in ("crate", "super", "self"):
            segs = path + [text(self.src, n)]
            self.import_name(segs[-1], segs, n)
        elif t == "use_as_clause":
            p = n.child_by_field_name("path")
            alias = n.child_by_field_name("alias")
            segs = self.segments(p) if p is not None else path
            if alias is not None:
                self.import_name(text(self.src, alias), segs, p.child_by_field_name("name")
                                 if p is not None and p.type == "scoped_identifier" else p)
        elif t == "scoped_use_list":
            p = n.child_by_field_name("path")
            lst = n.child_by_field_name("list")
            base = self.segments(p) if p is not None else []
            if lst is not None:
                for c in lst.children:
                    if c.is_named:
                        self.use_tree(c, base)
        elif t == "use_list":
            for c in n.children:
                if c.is_named:
                    self.use_tree(c, path)
        elif t == "use_wildcard":
            p = n.children[0] if n.children else None
            segs = self.segments(p) if p is not None else path
            self.globs.append(segs[0] if segs else "")

    def segments(self, n):
        if n is None:
            return []
        if n.type in ("scoped_identifier", "scoped_type_identifier"):
            return self.segments(n.child_by_field_name("path")) + [text(self.src, n.child_by_field_name("name"))]
        return [text(self.src, n)]

    def import_name(self, name, segs, node):
        if name in ("self", "super", "crate", "*"):
            return
        self.imports[name] = (segs[0] if segs else "", segs)
        if node is not None and node.type in ("identifier", "type_identifier"):
            self.add_ref(node, "import", root=segs[0] if segs else "", parent=segs[-2] if len(segs) > 1 else "")

    def ident_ref(self, node, ctx, root="", parent=""):
        name = text(self.src, node)
        if name == "Self":
            r = self.add_ref(node, "self", root=root, parent=parent)
            if self.impl_stack and self.impl_stack[-1]:
                r[3] = self.impl_stack[-1]                 # resolve as the impl's type
            else:
                r[9] = S["self unresolved"]
            return
        r = self.add_ref(node, ctx, root=root, parent=parent)
        if not root and ctx == "value" and self.fn_stack:
            loc = self.fn_stack[-1][1].get(name)
            if loc is not None:
                r[8], r[9] = loc, S["local"]
                return
        if not root and ctx in ("type", "value"):
            for g in reversed(self.gen_stack):
                if name in g:
                    r[8], r[9] = g[name], S["generic"]
                    return

    # ---- python -------------------------------------------------------
    def python(self, n):
        t = n.type
        if t in ("comment", "string", "integer", "float", "true", "false", "none"):
            return
        if t in ("function_definition", "class_definition"):
            name = n.child_by_field_name("name")
            kind = "class" if t == "class_definition" else ("method" if self.scope_stack and
                                                             self.ents[self.scope_stack[-1]][3] == K["class"] else "fn")
            i = self.add_ent(name, kind) if name else -1
            if t == "function_definition":
                self.fn_stack.append((i, {}))
            self.scope_stack.append(i)
            for c in n.children:
                if c is not name:
                    self.python(c)
            self.scope_stack.pop()
            if t == "function_definition":
                self.fn_stack.pop()
            return
        if t == "parameters":
            for c in n.children:
                tgt = c if c.type == "identifier" else c.child_by_field_name("name") if c.type in ("default_parameter", "typed_default_parameter") else \
                    (c.children[0] if c.type in ("typed_parameter", "list_splat_pattern", "dictionary_splat_pattern") and c.children else None)
                if tgt is not None and tgt.type == "identifier":
                    i = self.add_ent(tgt, "param")
                    if self.fn_stack:
                        self.fn_stack[-1][1][text(self.src, tgt)] = i
                ty = c.child_by_field_name("type") if c.is_named else None
                if ty is not None:
                    self.python(ty)
            return
        if t == "assignment" and self.fn_stack:
            left = n.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                name = text(self.src, left)
                if name not in self.fn_stack[-1][1]:
                    self.fn_stack[-1][1][name] = self.add_ent(left, "local")
                else:
                    self.ident_ref_py(left, "value")
                for c in n.children:
                    if c is not left:
                        self.python(c)
                return
        if t in ("import_statement", "import_from_statement"):
            mod = n.child_by_field_name("module_name")
            root = text(self.src, mod).split(".")[0] if mod is not None else ""
            for c in n.children:
                if c.type == "dotted_name":
                    ident = c.children[-1] if c.children else None
                    if ident is not None and ident.type == "identifier":
                        nm = text(self.src, ident)
                        segs = text(self.src, c).split(".")
                        self.imports[nm] = (root or segs[0], (segs if not root else [root] + segs))
                        self.add_ref(ident, "import", root=root or segs[0])
                elif c.type == "aliased_import":
                    nm = c.child_by_field_name("name")
                    alias = c.child_by_field_name("alias")
                    if nm is not None and alias is not None:
                        segs = text(self.src, nm).split(".")
                        self.imports[text(self.src, alias)] = (root or segs[0], (segs if not root else [root] + segs))
                        self.add_ref(alias, "import", root=root or segs[0])
            return
        if t == "attribute":
            obj = n.child_by_field_name("object")
            attr = n.child_by_field_name("attribute")
            if obj is not None:
                self.python(obj)
            if attr is not None:
                self.add_ref(attr, "field")
            return
        if t == "keyword_argument":
            val = n.child_by_field_name("value")
            if val is not None:
                self.python(val)
            return
        if t == "identifier":
            self.ident_ref_py(n, "value")
            return
        for c in n.children:
            self.python(c)

    def ident_ref_py(self, node, ctx):
        name = text(self.src, node)
        r = self.add_ref(node, ctx)
        if self.fn_stack:
            loc = self.fn_stack[-1][1].get(name)
            if loc is not None:
                r[8], r[9] = loc, S["local"]
                return
        if name in PY_BUILTINS:
            r[9] = S["external"]

    # ---- c ------------------------------------------------------------
    def c(self, n):
        t = n.type
        if t in ("comment", "string_literal", "char_literal", "number_literal", "primitive_type",
                 "system_lib_string", "preproc_include", "preproc_arg"):
            return
        if t == "function_definition":
            decl = n.child_by_field_name("declarator")
            ident = self.c_ident(decl)
            i = self.add_ent(ident, "fn") if ident is not None else -1
            self.fn_stack.append((i, {}))
            self.scope_stack.append(i)
            for c in n.children:
                self.c_skip_ident(c, ident)
            self.scope_stack.pop(); self.fn_stack.pop()
            return
        if t in ("struct_specifier", "union_specifier", "enum_specifier"):
            name = n.child_by_field_name("name")
            body = n.child_by_field_name("body")
            if name is not None and body is not None:
                i = self.add_ent(name, t.split("_")[0])
                self.scope_stack.append(i)
                self.c(body)
                self.scope_stack.pop()
            elif name is not None:
                self.add_ref(name, "type")
            return
        if t == "type_definition":
            decl = n.child_by_field_name("declarator")
            ident = self.c_ident(decl)
            if ident is not None:
                self.add_ent(ident, "type")
            for c in n.children:
                self.c_skip_ident(c, ident)
            return
        if t in ("preproc_def", "preproc_function_def"):
            name = n.child_by_field_name("name")
            if name is not None:
                self.add_ent(name, "macro")
            val = n.child_by_field_name("value")
            if val is not None and val.type != "preproc_arg":
                self.c(val)
            return
        if t == "enumerator":
            name = n.child_by_field_name("name")
            if name is not None:
                self.add_ent(name, "variant")
            return
        if t == "field_declaration":
            decl = n.child_by_field_name("declarator")
            ident = self.c_ident(decl)
            if ident is not None:
                self.add_ent(ident, "field")
            for c in n.children:
                self.c_skip_ident(c, ident)
            return
        if t == "parameter_declaration":
            decl = n.child_by_field_name("declarator")
            ident = self.c_ident(decl)
            if ident is not None and ident.type == "identifier":
                i = self.add_ent(ident, "param")
                if self.fn_stack:
                    self.fn_stack[-1][1][text(self.src, ident)] = i
            for c in n.children:
                self.c_skip_ident(c, ident)
            return
        if t == "declaration" and self.fn_stack:
            for c in n.children:
                if c.type in ("init_declarator", "identifier", "pointer_declarator", "array_declarator"):
                    ident = self.c_ident(c)
                    if ident is not None and ident.type == "identifier":
                        i = self.add_ent(ident, "local")
                        self.fn_stack[-1][1][text(self.src, ident)] = i
                    for cc in c.children:
                        self.c_skip_ident(cc, ident)
                else:
                    self.c(c)
            return
        if t == "field_expression":
            arg = n.child_by_field_name("argument")
            fld = n.child_by_field_name("field")
            if arg is not None:
                self.c(arg)
            if fld is not None:
                self.add_ref(fld, "field")
            return
        if t == "identifier":
            name = text(self.src, n)
            r = self.add_ref(n, "value")
            if self.fn_stack:
                loc = self.fn_stack[-1][1].get(name)
                if loc is not None:
                    r[8], r[9] = loc, S["local"]
            return
        if t == "type_identifier":
            self.add_ref(n, "type")
            return
        if t == "field_identifier":
            self.add_ref(n, "field")
            return
        for c in n.children:
            self.c(c)

    def c_ident(self, n):
        while n is not None and n.type not in ("identifier", "type_identifier", "field_identifier"):
            nxt = n.child_by_field_name("declarator")
            if nxt is None:
                nxt = next((c for c in n.children if c.is_named and c.type != "parameter_list"), None)
            n = nxt
        return n

    def c_skip_ident(self, node, ident):
        if node is ident:
            return
        if ident is not None and node.start_byte <= ident.start_byte < node.end_byte and node.type not in ("compound_statement",):
            for c in node.children:
                self.c_skip_ident(c, ident)
            return
        self.c(node)


def parse_file(job):
    """worker: (file index, path, lang) -> (file index, ents, refs, imports, globs)"""
    fi, path, lang = job
    try:
        src = Path(path).read_bytes()
    except OSError:
        return fi, [], [], {}, []
    sys.setrecursionlimit(100000)      # generated files nest expressions thousands deep
    w = FileWalk(src, lang)
    tree = parser_for(LANG_OF[lang]).parse(src)
    try:
        getattr(w, LANG_OF[lang])(tree.root_node)
    except RecursionError:
        print(f"  skipped {path}: nested too deeply to walk", file=sys.stderr)
        return fi, [], [], {}, []
    # same-file resolution happens here, where the file's names are known
    for r in w.refs:
        if r[9] >= 0 or r[5]:
            continue
        fit = CTX_KINDS.get(r[4])
        cands = [i for i in w.by_name.get(r[3], ()) if fit is None or w.ents[i][3] in fit]
        if r[4] == "field" and r[6] == "":
            pass
        if len(cands) == 1:
            r[8], r[9] = cands[0], S["file"]
        elif len(cands) > 1:
            top = [i for i in cands if w.ents[i][5] == -1]
            if len(top) == 1:
                r[8], r[9] = top[0], S["file"]
    return fi, w.ents, w.refs, w.imports, w.globs


# ---------------------------------------------------------------- crates

def find_crates(root, files):
    """crate id per file from the nearest ancestor Cargo.toml (Rust) or the
    nearest ancestor with an __init__.py chain's top (Python) - keep it to
    Cargo.toml: Python and C files are attached to the same crate as Rust
    files beside them, else unattached."""
    cargo = {}
    crates = []
    cache = {}
    for fi, f in enumerate(files):
        d = Path(f["path"]).parent
        parts = d.parts
        found = -1
        for k in range(len(parts), -1, -1):
            sub = Path(*parts[:k]) if k else Path(".")
            if sub in cache:
                found = cache[sub]
                break
            toml = Path(root) / sub / "Cargo.toml"
            if toml.exists():
                try:
                    txt = toml.read_text(errors="replace")
                except OSError:
                    txt = ""
                m = re.search(r'^\s*name\s*=\s*"([^"]+)"', txt, re.M)
                if m and "[package]" in txt:
                    name = m.group(1).replace("-", "_")
                    if name not in cargo:
                        cargo[name] = len(crates)
                        crates.append({"name": name, "dir": str(sub)})
                    found = cargo[name]
                    break
        cache[d] = found
        yield found
    find_crates.crates = crates


def u16(values):
    """columns past 65535 (generated one-line files) are clamped; the index
    caps lines at 4096 columns anyway"""
    return np.minimum(np.array(values, np.int64), 65535).astype(np.uint16)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("atlas")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    args = ap.parse_args()
    d = Path(args.atlas)
    ix = json.loads((d / "index.json").read_text())
    root, files = ix["root"], ix["files"]
    t0 = time.perf_counter()

    file_crate = np.array(list(find_crates(root, files)), np.int32)
    crates = find_crates.crates
    crate_by_name = {c["name"]: i for i, c in enumerate(crates)}

    jobs = [(fi, str(Path(root) / f["path"]), f["lang"]) for fi, f in enumerate(files)
            if f["lang"] in LANG_OF]
    results = {}
    done = 0
    with ProcessPoolExecutor(args.workers) as ex:
        for fi, ents, refs, imports, globs in ex.map(parse_file, jobs, chunksize=8):
            results[fi] = (ents, refs, imports, globs)
            done += 1
            if done % 500 == 0:
                print(f"  parsed {done}/{len(jobs)} files", file=sys.stderr)
    print(f"parsed {len(jobs)} files in {time.perf_counter() - t0:.1f}s; resolving", file=sys.stderr)

    # global entity tables
    ent_file, ent_line, ent_col, ent_len, ent_kind, ent_scope, ent_name, ent_impl = [], [], [], [], [], [], [], []
    base = {}
    names = {}

    def name_id(s):
        i = names.get(s)
        if i is None:
            i = names[s] = len(names)
        return i

    for fi in sorted(results):
        ents = results[fi][0]
        base[fi] = len(ent_file)
        for e in ents:
            ent_file.append(fi); ent_line.append(e[0]); ent_col.append(e[1]); ent_len.append(e[2])
            ent_kind.append(e[3]); ent_name.append(name_id(e[4]))
            ent_scope.append(base[fi] + e[5] if e[5] >= 0 else -1)
            ent_impl.append(e[6] if len(e) > 6 else "")
    ent_kind_a = np.array(ent_kind, np.uint8)
    ent_file_a = np.array(ent_file, np.int64)
    n_ent = len(ent_file)
    by_name = defaultdict(list)          # name -> global entity ids
    for i, nm in enumerate(ent_name):
        by_name[nm].append(i)
    by_crate_name = defaultdict(list)    # (crate, name) -> ids
    for i in range(n_ent):
        by_crate_name[(int(file_crate[ent_file_a[i]]), ent_name[i])].append(i)

    def pick(cands, fit, parent=""):
        cands = [i for i in cands if fit is None or ent_kind_a[i] in fit]
        if parent:
            byp = [i for i in cands if ent_impl[i] == parent or
                   (ent_scope[i] >= 0 and ent_name[ent_scope[i]] == name_id(parent))]
            if byp:
                cands = byp
        if len(cands) > 1:
            top = [i for i in cands if ent_scope[i] == -1]
            if len(top) == 1:
                cands = top
        return cands

    ref_file, ref_line, ref_col, ref_len, ref_name, ref_ent, ref_status = [], [], [], [], [], [], []
    for fi in sorted(results):
        ents, refs, imports, globs = results[fi]
        crate = int(file_crate[fi])
        for r in refs:
            line, col, ln, name, ctx, root, parent, fn, ent, status = r
            nm = name_id(name)
            fit = CTX_KINDS.get(ctx)
            if status >= 0:
                ent = base[fi] + ent if ent >= 0 else -1
                if ctx == "self" and ent >= 0:
                    status = S["self"]
            else:
                ent = -1
                # a path root or a same-named import decides the crate to look in
                target_crate, via_import = None, False
                if root in ("crate", "self", "super"):
                    target_crate = crate
                elif root in STD_CRATES:
                    status = S["external"]
                elif root:
                    target_crate = crate_by_name.get(root.replace("-", "_"))
                    if target_crate is None and name_id(root) not in by_name:
                        status = S["external"]          # a crate that is not in the corpus
                    # else the root is a type or module of the corpus (Enum::Variant,
                    # Type::method, module::item): resolved below with `parent`
                elif name in imports:
                    iroot = imports[name][0]
                    via_import = True
                    if iroot in ("crate", "self", "super"):
                        target_crate = crate
                    elif iroot in STD_CRATES:
                        status = S["external"]
                    else:
                        target_crate = crate_by_name.get(iroot.replace("-", "_"))
                        if target_crate is None:
                            status = S["external"]
                if status < 0 and ctx == "macro":
                    if name in STD_MACROS:
                        status = S["external"]
                    else:
                        cands = pick(by_name.get(nm, ()), fit)
                        if len(cands) == 1:
                            ent, status = cands[0], S["global"]
                        elif cands:
                            status = S["ambiguous"]
                        else:
                            status = S["macro missing"]
                if status < 0 and target_crate is not None:
                    cands = pick(by_crate_name.get((target_crate, nm), ()), fit, parent)
                    if len(cands) == 1:
                        ent, status = cands[0], S["import"] if (via_import or root) else S["crate"]
                    elif cands:
                        status = S["ambiguous"]
                    elif via_import or root:
                        status = S["import missing"]
                if status < 0 and crate >= 0:
                    cands = pick(by_crate_name.get((crate, nm), ()), fit, parent)
                    if len(cands) == 1:
                        ent, status = cands[0], S["crate"]
                    elif cands:
                        status = S["ambiguous"]
                if status < 0 and globs:
                    for g in globs:
                        gc = crate if g in ("crate", "self", "super") else crate_by_name.get(g.replace("-", "_"))
                        if gc is None:
                            continue
                        cands = pick(by_crate_name.get((gc, nm), ()), fit, parent)
                        if len(cands) == 1:
                            ent, status = cands[0], S["import"]
                            break
                if status < 0:
                    cands = pick(by_name.get(nm, ()), fit, parent)
                    if len(cands) == 1:
                        ent, status = cands[0], S["global"]
                    elif cands:
                        status = S["method"] if ctx == "field" else S["ambiguous"]
                    else:
                        status = S["not found"]
                if ctx == "self" and status not in (S["self unresolved"],):
                    status = S["self"] if ent >= 0 else S["self unresolved"]
            ref_file.append(fi); ref_line.append(line); ref_col.append(col); ref_len.append(ln)
            ref_name.append(nm); ref_ent.append(ent); ref_status.append(status)

    ref_file_a = np.array(ref_file, np.uint32)
    ref_ent_a = np.array(ref_ent, np.int32)
    ref_status_a = np.array(ref_status, np.uint8)
    n_files = len(files)
    refs_in = np.zeros(n_files, np.uint32)
    refs_out = np.zeros(n_files, np.uint32)
    ok = ref_ent_a >= 0
    tgt_file = ent_file_a[ref_ent_a[ok]]
    cross = tgt_file != ref_file_a[ok]
    np.add.at(refs_in, tgt_file[cross], 1)
    np.add.at(refs_out, ref_file_a[ok][cross], 1)

    counts = {s: int((ref_status_a == i).sum()) for i, s in enumerate(STATUSES)}
    unattached = int((file_crate < 0).sum())
    secs = time.perf_counter() - t0
    stats = {"files": len(jobs), "unattached": unattached, "crates": len(crates),
             "entities": n_ent, "references": len(ref_file),
             "resolved": int(ok.sum()), "status": counts, "seconds": round(secs, 1)}
    np.savez(d / "resolve.npz",
             ent_file=np.array(ent_file, np.uint32), ent_line=np.array(ent_line, np.uint32),
             ent_col=u16(ent_col), ent_len=u16(ent_len),
             ent_kind=ent_kind_a, ent_scope=np.array(ent_scope, np.int32),
             ent_name=np.array(ent_name, np.uint32),
             ref_file=ref_file_a, ref_line=np.array(ref_line, np.uint32),
             ref_col=u16(ref_col), ref_len=u16(ref_len),
             ref_ent=ref_ent_a, ref_status=ref_status_a, ref_name=np.array(ref_name, np.uint32),
             file_refs_in=refs_in, file_refs_out=refs_out, file_crate=file_crate)
    name_list = [None] * len(names)
    for s, i in names.items():
        name_list[i] = s
    (d / "resolve.json").write_text(json.dumps(
        {"names": name_list, "kinds": KINDS, "statuses": STATUSES, "crates": crates, "stats": stats}))
    print(f"wrote {d}/resolve.npz + resolve.json: {n_ent} entities, {len(ref_file)} references "
          f"({stats['resolved']} resolved), {len(crates)} crates, {unattached} unattached files, {secs:.1f}s")
    print("  " + " · ".join(f"{s} {c}" for s, c in counts.items() if c))


if __name__ == "__main__":
    main()
