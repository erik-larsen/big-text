#!/usr/bin/env python3
"""Stage 4: the code atlas viewer.

Draws a source tree laid out by atlas_layout.py as a zoomable treemap with
a ladder of representations chosen per file from its size on screen:
sampled one pixel line bars over a dark tile (under 1 device pixel per
line), grey line bars (1 to 3), coloured token segments (3 to 6) and glyphs
from the raster atlas (6 and up). All
lines, tokens and characters of the corpus are resident on the GPU as
integer and float textures; every frame is a handful of instanced draws
whose vertex shaders cull by rung and view.

Controls: wheel = zoom about the cursor (with a glide) | drag = pan
          hover = file / line / item label | R = refit | Q = quit
          / = focus the filter box, type to search, Backspace edits
          Enter, Down, ] = next result | Up, [ = previous result
          click a result row = fly there | Escape = clear filter, then selection
"""
import argparse
import ctypes
import json
import math
import queue
import re
import sys
import threading
import time
from pathlib import Path

import glfw
import numpy as np
from OpenGL.GL import *  # noqa: F403
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import atlas_font  # noqa: E402

HERE = Path(__file__).resolve().parent
CHARS_W = 16384          # width of tex_chars / tex_kinds, shared with line.glsl
TEX_W = 4096             # width of the per-line and per-file textures
ZOOM_TAU = 0.12          # glide time constant, seconds
ZOOM_TICK = math.log(1.2) / ZOOM_TAU    # one wheel unit ends up as x1.2
ZOOM_VMAX = 12.0         # log-zoom per second at most
PANEL_PT = 180           # results panel width in window points
MARGIN_PT = 10           # UI margin in window points
FLY_RHO = 1.4
FLY_V = 10.0             # van Wijk path units per second
VT_MIN_PPL = 40.0        # --vector-text draws glyphs with the vector tier from here


def rgb(h):
    return np.array([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], np.float32)


BG = rgb("1c1c1e")
KIND_COLORS = np.array([rgb("000000"), rgb("cfd2d8"), rgb("7aa2f7"), rgb("e0c080"),
                        rgb("d7a0a8"), rgb("7f9f7f"), rgb("f0a060"), rgb("8c909a"),
                        rgb("7fc8c8"), rgb("c8d08c")], np.float32)
HUES = np.array([rgb(h) for h in ("6a8fd8", "4fb3a6", "7fbf6a", "e08a4a", "d8c050",
                                  "a57fd0", "a8865a", "d878a8", "60b8e0", "b0c860",
                                  "d86868", "9090d8")], np.float32)
ITEM_COLORS = {1: rgb("7aa2f7"), 2: rgb("e0c080"), 3: rgb("d7a0a8"),
               4: rgb("5fb7b7"), 5: rgb("8c909a")}
ITEM_KW_RX = re.compile(r"\b(fn|struct|enum|trait|impl|mod|macro_rules|type|const|static|union|def|class|function|interface|namespace)\b")
ITEM_NAMES = {"rust": ["fn", "struct", "enum", "impl", "mod"],
              "python": ["def", "class", "enum", "impl", "mod"],
              None: ["function", "struct", "enum", "interface", "module"]}
DEF_KW = {"rust": b"fn|struct|enum|trait|impl|type|mod|const|static",
          "python": b"def|class",
          None: b"fn|struct|enum|trait|impl|type|mod|def|class|function|interface|const|static"}
YELLOW = rgb("e8d44d")
UI_TEXT = rgb("e6e6e6")
UI_BOX = rgb("2a2a2e")
UI_DIM = rgb("8c909a")


# ---------------------------------------------------------------- GL helpers

def load_program(name, frag_prefix=""):
    """shaders/<name>.glsl holds the vertex and fragment stages separated
    by a '// ---- fragment ----' line; comments above #version are dropped.
    frag_prefix is pasted into the fragment stage right after its #version
    line (the vector text tier's #define and code)."""
    src = (HERE / "shaders" / f"{name}.glsl").read_text()
    parts = src.split("// ---- fragment ----")
    if len(parts) != 2:
        raise RuntimeError(f"{name}.glsl: expected one fragment marker")

    def stage(text, prefix=""):
        lines = text.splitlines()
        while lines and not lines[0].startswith("#version"):
            lines.pop(0)
        if prefix:
            lines.insert(1, prefix)
        return "\n".join(lines) + "\n"

    prog = glCreateProgram()
    for kind, text, prefix in ((GL_VERTEX_SHADER, parts[0], ""),
                               (GL_FRAGMENT_SHADER, parts[1], frag_prefix)):
        sh = glCreateShader(kind)
        glShaderSource(sh, stage(text, prefix))
        glCompileShader(sh)
        if not glGetShaderiv(sh, GL_COMPILE_STATUS):
            raise RuntimeError(f"{name}.glsl: " + glGetShaderInfoLog(sh).decode())
        glAttachShader(prog, sh)
    glLinkProgram(prog)
    if not glGetProgramiv(prog, GL_LINK_STATUS):
        raise RuntimeError(f"{name}.glsl: " + glGetProgramInfoLog(prog).decode())
    return prog


def padded_rows(data, width):
    """(n, ch) array -> (rows, width, ch) with zero padding, for a 2D texture."""
    data = np.ascontiguousarray(data)
    n = data.shape[0]
    ch = data.shape[1] if data.ndim > 1 else 1
    h = max(1, -(-n // width))
    buf = np.zeros((h * width, ch), data.dtype)
    buf[:n] = data.reshape(n, ch)
    return buf.reshape(h, width, ch)


def texture_2d(internal, fmt, typ, buf, filt=GL_NEAREST, mipmap=False):
    """Upload a (h, w, ch) array as a 2D texture; returns the texture id."""
    t = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, t)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER,
                    GL_LINEAR_MIPMAP_LINEAR if mipmap else filt)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, filt)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    h, w = buf.shape[:2]
    glTexImage2D(GL_TEXTURE_2D, 0, internal, w, h, 0, fmt, typ, buf)
    if mipmap:
        glGenerateMipmap(GL_TEXTURE_2D)
    return t


def instanced_vao(quad_vbo, attribs):
    """VAO with the unit quad at location 0 and per-instance float attributes
    [(location, size), ...] interleaved in one dynamic VBO. Returns (vao, vbo)."""
    vao = glGenVertexArrays(1)
    glBindVertexArray(vao)
    glBindBuffer(GL_ARRAY_BUFFER, quad_vbo)
    glEnableVertexAttribArray(0)
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 8, ctypes.c_void_p(0))
    vbo = glGenBuffers(1)
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    stride = 4 * sum(n for _, n in attribs)
    off = 0
    for loc, n in attribs:
        glEnableVertexAttribArray(loc)
        glVertexAttribPointer(loc, n, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(off))
        glVertexAttribDivisor(loc, 1)
        off += 4 * n
    glBindVertexArray(0)
    return vao, vbo


# ---------------------------------------------------------------- data

def check_atlas(d):
    d = Path(d)
    missing = [n for n in ("index.npz", "index.json", "layout.npz", "layout.json")
               if not (d / n).exists()]
    if missing:
        raise SystemExit(f"error: '{d}' is missing {', '.join(missing)}\n\n"
                         "Build an atlas first:\n\n"
                         "    ./atlas_index.py <src_dir>            # -> data/<name>_atlas\n"
                         "    ./atlas_layout.py data/<name>_atlas\n"
                         "    ./tests/gen_synthetic_atlas.py         # or a synthetic one")


def load_atlas(d):
    d = Path(d)
    ix = np.load(d / "index.npz")
    ly = np.load(d / "layout.npz")
    a = {k: ix[k] for k in ix.files}
    a.update({k: ly[k] for k in ly.files})
    a["index"] = json.loads((d / "index.json").read_text())
    a["layout"] = json.loads((d / "layout.json").read_text())
    return a


def def_regex(lang, word):
    kw = DEF_KW.get(lang, DEF_KW[None])
    return re.compile(rb"\b(" + kw + rb")\s+(?:<[^>]*>\s*)?" + re.escape(word) + rb"\b")


def joined_lines(atlas):
    """The corpus with a newline after every line, built once on first use:
    chars has no separators, so a word that ends one line and a word that
    starts the next fuse and defeat the regex's word boundaries."""
    nl = atlas.get("_nl")
    if nl is None:
        chars = atlas["chars"]
        line_off = atlas["line_off"].astype(np.int64)
        n = len(line_off) - 1
        out = np.full(len(chars) + n, 10, np.uint8)
        line = np.repeat(np.arange(n, dtype=np.int64), np.diff(line_off))
        out[np.arange(len(chars), dtype=np.int64) + line] = chars
        nl = atlas["_nl"] = (out.tobytes(), line_off + np.arange(n + 1))
    return nl


def run_search(atlas, word):
    """Whole-word substring search over the corpus bytes. Returns a dict of
    arrays (file, line, col, len, is_def) plus per-hit kind strings, or
    None when the word is too short."""
    if len(word) < 2:
        return None
    w = word.encode("ascii", "replace")
    pat = re.compile(rb"\b" + re.escape(w) + rb"\b")
    buf, line_off_nl = joined_lines(atlas)
    # scan in chunks so the GIL changes hands between them and the frame
    # loop keeps running while a big corpus is searched in the thread;
    # pos/endpos (not slicing) keep the left context for the first \b
    chunk, overlap = 1 << 20, len(w) + 2
    starts = []
    for c0 in range(0, len(buf), chunk):
        c1 = min(c0 + chunk + overlap, len(buf))
        starts.extend(m.start() for m in pat.finditer(buf, c0, c1)
                      if m.start() < c0 + chunk)
        time.sleep(0)
    off = np.array(starts, np.int64)
    line = np.searchsorted(line_off_nl, off, side="right") - 1
    col = off - line_off_nl[line]
    chars = atlas["chars"]
    line_off = atlas["line_off"].astype(np.int64)
    # a mention inside a comment or a string is not a reference
    code = ~np.isin(atlas["kinds"][line_off[line] + col], (4, 5))
    line, col = line[code], col[code]
    file = atlas["line_file"][line].astype(np.int64)
    is_def = np.zeros(len(line), bool)
    kinds = [""] * len(line)
    files = atlas["index"]["files"]
    regs = {}
    for i in range(len(line)):
        lang = files[file[i]]["lang"]
        r = regs.get(lang)
        if r is None:
            r = regs[lang] = def_regex(lang, w)
        text = chars[line_off[line[i]]:line_off[line[i] + 1]].tobytes()
        m = r.search(text)
        if m:
            is_def[i] = True
            kinds[i] = m.group(1).decode()
    return {"file": file, "line": line, "col": col, "len": len(w),
            "is_def": is_def, "kind": kinds, "word": word}


def load_vector_tier(font, index):
    """(atlas, glyph_table, metrics) of the Dobbie vector glyph tier for the
    font, built in the same line box as the raster atlas so both tiers draw
    a glyph at the same size and baseline; cached under data/ because the
    build takes about 1.6 s."""
    import vt_glyphs
    asc, desc, _, _, _ = atlas_font.font_box(font, index)
    cache = HERE / "data" / f"vt_{Path(font).stem.lower()}_{index}_{asc}_{desc}.npz"
    if cache.exists():
        return vt_glyphs.load(cache)
    t0 = time.perf_counter()
    atlas, table, metrics = vt_glyphs.build_atlas(font, index, 12, box=(asc, desc))
    cache.parent.mkdir(parents=True, exist_ok=True)
    vt_glyphs.save(cache, atlas, table, metrics)
    print(f"vector glyph atlas {atlas.shape[1]}x{atlas.shape[0]} built in "
          f"{time.perf_counter() - t0:.1f} s -> {cache}")
    return atlas, table, metrics


# ---------------------------------------------------------------- viewer

class Viewer:
    def __init__(self, args):
        self.args = args
        self.scripted = args.frames is not None or args.shots or args.stats
        t0 = time.perf_counter()
        self.a = load_atlas(args.atlas)
        a = self.a
        self.n_files = len(a["file_rect"])
        self.n_lines = len(a["line_pos"])
        self.n_dirs = len(a["dir_rect"])
        self.n_chars = len(a["chars"])
        self.W, self.H = (float(v) for v in a["world"])
        self.A = float(a["layout"].get("char_aspect", 0.6))
        self.name = a["index"].get("name", Path(args.atlas).name)
        self.paths = [f["path"] for f in a["index"]["files"]]
        self.langs = [f.get("lang") for f in a["index"]["files"]]
        self.dir_labels = [d["label"] for d in a["layout"]["dirs"]]
        self.dir_parent = np.array([d["parent"] for d in a["index"]["dirs"]], np.int64)
        self.item_names = [it["name"] for it in a["index"]["items"]]
        self.build_dir_tags()
        self.item_rect_file = a["item_file"][a["item_rect_item"]]
        order = np.argsort(a["item_file"], kind="stable")
        bounds = np.searchsorted(a["item_file"][order], np.arange(self.n_files + 1))
        self.items_by_file = [order[bounds[f]:bounds[f + 1]] for f in range(self.n_files)]
        self.build_hover_grid()
        # the typical pitch: that of the file holding the median line
        order = np.argsort(a["file_pitch"], kind="stable")
        per_file = np.diff(a["file_line0"])[order]
        mid = min(int(np.searchsorted(np.cumsum(per_file), per_file.sum() / 2)), len(order) - 1)
        self.median_pitch = float(a["file_pitch"][order[mid]])
        if self.n_chars > CHARS_W * CHARS_W:
            raise SystemExit(f"error: {self.n_chars} characters exceed the "
                             f"{CHARS_W}x{CHARS_W} texture; the resident design stops here")
        self.glyphs, self.gm = atlas_font.build_atlas(args.font, args.font_index, 64)
        self.ui_aspect = self.gm["cell_w"] / self.gm["cell_h"]
        self.vt = load_vector_tier(args.font, args.font_index) if args.vector_text else None
        self.load_seconds = time.perf_counter() - t0

        self.open_window()
        t1 = time.perf_counter()
        self.setup_gl()
        self.gpu_seconds = time.perf_counter() - t1

        # camera (float64) and motion
        self.cx, self.cy, self.zoom = self.W / 2, self.H / 2, 1.0
        self.zoom_vel, self.zoom_anchor = 0.0, None
        self.fly = None
        self.drag = None
        self.press = None
        self.cursor = None            # device pixels, None when outside
        self.cursor_pt = None         # the same in window points
        self.cursor_override = None   # scripted hover position

        # hover, crumbs, filter
        self.hover = None             # (file, line_j, col)
        self.crumb = ""
        self.filter_text = ""
        self.filter_focus = False
        self.swallow_char = False
        self.results = None           # dict from run_search + ordering
        self.hits_by_file = np.zeros(self.n_files, bool)
        self.hit_count = np.zeros(self.n_files, np.int64)
        self.dimmed = np.zeros(self.n_files, bool)
        self.current_file = -1
        self.result_i = -1
        self.panel_rows = []
        self.panel_scroll = 0
        self.panel_rect = None
        self.filter_rect = None
        self.search_gen = 0
        self.search_q = queue.Queue()
        self.frame_times = []
        self.rung = np.zeros(self.n_files, np.uint8)
        self.visible = np.zeros(self.n_files, bool)
        self.fit()

    # ---- window ---------------------------------------------------------
    def open_window(self):
        if not glfw.init():
            raise RuntimeError("glfw.init failed")
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
        self.win = glfw.create_window(1600, 1000, f"code atlas: {self.name}", None, None)
        if not self.win:
            raise RuntimeError("window creation failed")
        glfw.make_context_current(self.win)
        glfw.swap_interval(0 if self.scripted else 1)
        self.update_sizes()
        glfw.set_mouse_button_callback(self.win, self.on_mouse_button)
        glfw.set_cursor_pos_callback(self.win, self.on_cursor)
        glfw.set_cursor_enter_callback(self.win, self.on_enter)
        glfw.set_scroll_callback(self.win, self.on_scroll)
        glfw.set_key_callback(self.win, self.on_key)
        glfw.set_char_callback(self.win, self.on_char)

    def update_sizes(self):
        self.fb_w, self.fb_h = glfw.get_framebuffer_size(self.win)
        ww, wh = glfw.get_window_size(self.win)
        self.px = self.fb_w / ww if ww else 1.0      # device pixels per point

    def setup_gl(self):
        a = self.a
        vt_src = ""
        if self.vt is not None:
            vt_src = "#define VT_TEXT 1\n" + (HERE / "shaders" / "vt_glyph.glsl").read_text()
        self.prog = {n: load_program(n, vt_src if n == "line" else "")
                     for n in ("dir", "file", "line", "rect", "text")}
        self.loc = {n: {} for n in self.prog}
        quad = np.array([[0, 0], [1, 0], [1, 1], [0, 0], [1, 1], [0, 1]], np.float32)
        self.quad_vbo = glGenBuffers(1)
        glBindBuffer(GL_ARRAY_BUFFER, self.quad_vbo)
        glBufferData(GL_ARRAY_BUFFER, quad.nbytes, quad, GL_STATIC_DRAW)
        self.quad_vao = glGenVertexArrays(1)
        glBindVertexArray(self.quad_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.quad_vbo)
        glEnableVertexAttribArray(0)
        glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 8, ctypes.c_void_p(0))
        self.rect_vao, self.rect_vbo = instanced_vao(self.quad_vbo, [(1, 4), (2, 4), (3, 2)])
        self.text_vao, self.text_vbo = instanced_vao(self.quad_vbo, [(1, 2), (2, 2), (3, 1), (4, 4)])
        glBindVertexArray(0)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)

        gpu = 0
        chars = padded_rows(a["chars"], CHARS_W)
        self.tex_chars = texture_2d(GL_R8UI, GL_RED_INTEGER, GL_UNSIGNED_BYTE, chars)
        self.tex_kinds = texture_2d(GL_R8UI, GL_RED_INTEGER, GL_UNSIGNED_BYTE,
                                    padded_rows(a["kinds"], CHARS_W))
        gpu += 2 * chars.size
        lf = np.zeros((self.n_lines, 4), np.float32)
        lf[:, :2] = a["line_pos"]
        # z: the line's index within its file, for the rung-0 sampling
        lf[:, 2] = np.arange(self.n_lines) - a["file_line0"][a["line_file"]].astype(np.int64)
        buf = padded_rows(lf, TEX_W)
        self.tex_line_f = texture_2d(GL_RGBA32F, GL_RGBA, GL_FLOAT, buf)
        gpu += buf.nbytes
        off = a["line_off"][:-1].astype(np.uint64)
        lu = np.zeros((self.n_lines, 4), np.uint32)
        lu[:, 0] = (off & np.uint64(0xFFFFFFFF)).astype(np.uint32)
        lu[:, 1] = a["line_file"]
        lu[:, 2] = a["line_indent"].astype(np.uint32) | (a["line_len"].astype(np.uint32) << 16)
        lu[:, 3] = (off >> np.uint64(32)).astype(np.uint32)
        buf = padded_rows(lu, TEX_W)
        self.tex_line_u = texture_2d(GL_RGBA32UI, GL_RGBA_INTEGER, GL_UNSIGNED_INT, buf)
        gpu += buf.nbytes
        ff = np.zeros((2 * self.n_files, 4), np.float32)
        ff[0::2] = a["file_rect"]
        ff[1::2, 0] = a["file_pitch"]
        ff[1::2, 1] = a["file_colw"]
        ff[1::2, 2] = a["file_cap"]
        ff[1::2, 3] = a["file_hue"]
        buf = padded_rows(ff, TEX_W)
        self.tex_file_f = texture_2d(GL_RGBA32F, GL_RGBA, GL_FLOAT, buf)
        gpu += buf.nbytes
        self.file_u = padded_rows(np.zeros((self.n_files, 4), np.uint8), TEX_W)
        self.tex_file_u = texture_2d(GL_RGBA8UI, GL_RGBA_INTEGER, GL_UNSIGNED_BYTE, self.file_u)
        gpu += self.file_u.nbytes
        df = np.zeros((2 * self.n_dirs, 4), np.float32)
        df[0::2] = a["dir_rect"]
        df[1::2, 0] = a["dir_pad"]
        df[1::2, 1] = a["dir_depth"]
        df[1::2, 2] = a["dir_hue"]
        buf = padded_rows(df, TEX_W)
        self.tex_dir_f = texture_2d(GL_RGBA32F, GL_RGBA, GL_FLOAT, buf)
        gpu += buf.nbytes
        # directories draw in depth order so parents go under children
        self.dir_order = np.argsort(a["dir_depth"], kind="stable")
        self.tex_glyphs = texture_2d(GL_R8, GL_RED, GL_UNSIGNED_BYTE,
                                     self.glyphs[:, :, None], GL_LINEAR, mipmap=True)
        gpu += int(self.glyphs.nbytes * 4 / 3)
        textures = [self.tex_chars, self.tex_kinds, self.tex_glyphs, self.tex_line_f,
                    self.tex_line_u, self.tex_file_f, self.tex_file_u, self.tex_dir_f]
        if self.vt is not None:
            import vt_glyphs
            textures.append(vt_glyphs.make_texture(self.vt[0]))
            gpu += self.vt[0].nbytes
        self.gpu_bytes = gpu

        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDisable(GL_DEPTH_TEST)
        for n, p in self.prog.items():
            glUseProgram(p)
            for i, tex in enumerate(("uChars", "uKinds", "uGlyphs", "uLineF", "uLineU",
                                     "uFileF", "uFileU", "uDirF", "vt_atlas")):
                loc = glGetUniformLocation(p, tex)
                if loc >= 0:
                    glUniform1i(loc, i)
            loc = glGetUniformLocation(p, "uHue")
            if loc >= 0:
                glUniform3fv(loc, 12, HUES)
            loc = glGetUniformLocation(p, "uKindColor")
            if loc >= 0:
                glUniform3fv(loc, 10, KIND_COLORS)
            loc = glGetUniformLocation(p, "uGlyph")
            if loc >= 0:
                glUniform4f(loc, self.gm["cell_w"], self.gm["cell_h"],
                            self.glyphs.shape[1], self.glyphs.shape[0])
            loc = glGetUniformLocation(p, "uCharAspect")
            if loc >= 0:
                glUniform1f(loc, self.A)
            loc = glGetUniformLocation(p, "uVtMin")
            if loc >= 0:
                glUniform1f(loc, VT_MIN_PPL)
        for i, tex in enumerate(textures):
            glActiveTexture(GL_TEXTURE0 + i)
            glBindTexture(GL_TEXTURE_2D, tex)

    def uniform(self, prog, name):
        d = self.loc[prog]
        if name not in d:
            d[name] = glGetUniformLocation(self.prog[prog], name)
        return d[name]

    def set_camera_uniforms(self, prog, world=True):
        """world: the map's viewport (the window minus the panel column);
        else the whole window in device pixels for the UI"""
        p = self.prog[prog]
        glUseProgram(p)
        if world:
            x0, y0, _, _ = self.view()
            glUniform2f(self.uniform(prog, "uOffset"), x0, y0)
            glUniform1f(self.uniform(prog, "uScale"), self.zoom)
            glUniform2f(self.uniform(prog, "uViewport"), self.map_w, self.fb_h)
        else:
            glUniform2f(self.uniform(prog, "uOffset"), 0.0, 0.0)
            glUniform1f(self.uniform(prog, "uScale"), 1.0)
            glUniform2f(self.uniform(prog, "uViewport"), self.fb_w, self.fb_h)

    # ---- camera ---------------------------------------------------------
    @property
    def map_w(self):
        """Width of the map's viewport in device pixels: the window, minus
        the right column (panel plus margins) while the results panel is open."""
        column = (PANEL_PT + 2 * MARGIN_PT) * self.px if self.panel_rows else 0.0
        return self.fb_w - column

    def view(self):
        w, h = self.map_w / self.zoom, self.fb_h / self.zoom
        return (self.cx - w / 2, self.cy - h / 2, self.cx + w / 2, self.cy + h / 2)

    def fit_zoom(self):
        return min(self.map_w * 0.94 / self.W, self.fb_h * 0.94 / self.H)

    def is_fitted(self):
        return (abs(self.zoom - self.fit_zoom()) < 1e-9 * self.zoom
                and self.cx == self.W / 2 and self.cy == self.H / 2)

    def ref_pitch(self, pt=None):
        """Line pitch of the file under pt (default: the view centre), else
        the typical pitch: the reference for --zoom, the shots and the zoom
        limit, so that 'N pixels per line' is about the file being looked at."""
        f = self.file_at(*(pt if pt is not None else (self.cx, self.cy)))
        return float(self.a["file_pitch"][f]) if f is not None else self.median_pitch

    def zoom_limits(self, pitch=None):
        """half the fit, up to 400 device pixels per line for the file at the centre"""
        return self.fit_zoom() * 0.5, 400.0 / (pitch or self.ref_pitch())

    def clamp_zoom(self, z, pitch=None):
        lo, hi = self.zoom_limits(pitch)
        return min(max(z, lo), hi)

    def fit(self):
        self.fly = None
        self.zoom_vel = 0.0
        self.cx, self.cy = self.W / 2, self.H / 2
        self.zoom = self.fit_zoom()

    def screen_to_world(self, sx, sy):
        return (self.cx + (sx - self.map_w / 2) / self.zoom,
                self.cy + (sy - self.fb_h / 2) / self.zoom)

    def world_to_screen(self, wx, wy):
        return ((wx - self.cx) * self.zoom + self.map_w / 2,
                (wy - self.cy) * self.zoom + self.fb_h / 2)

    def zoom_about(self, sx, sy, factor):
        wx, wy = self.screen_to_world(sx, sy)
        # the limit follows the file under the anchor, and a clamp never
        # moves the zoom against the gesture when the map slides under it
        lo, hi = self.zoom_limits(self.ref_pitch((wx, wy)))
        if factor > 1.0:
            hi = max(hi, self.zoom)
        else:
            lo = min(lo, self.zoom)
        z = min(max(self.zoom * factor, lo), hi)
        if z != self.zoom * factor:
            self.zoom_vel = 0.0
        self.zoom = z
        self.cx = wx - (sx - self.map_w / 2) / z
        self.cy = wy - (sy - self.fb_h / 2) / z

    def set_zoom_ppl(self, ppl, centre=None):
        """Zoom so the file under the view centre has ppl device pixels per line."""
        if centre is not None:
            self.cx, self.cy = centre
        self.fly = None
        self.zoom_vel = 0.0
        self.zoom = self.clamp_zoom(ppl / self.ref_pitch())

    def update_glide(self, dt):
        if self.zoom_vel == 0.0 or self.zoom_anchor is None:
            return
        k = math.exp(-dt / ZOOM_TAU)
        dlog = self.zoom_vel * ZOOM_TAU * (1.0 - k)
        self.zoom_vel *= k
        if abs(self.zoom_vel) < 0.02:
            self.zoom_vel = 0.0
        self.zoom_about(self.zoom_anchor[0], self.zoom_anchor[1], math.exp(dlog))

    def fly_to(self, rect, margin=0.06, complete=False):
        """van Wijk & Nuij smooth zoom-and-pan to a view containing rect."""
        x0, y0, x1, y1 = rect
        rw, rh = (x1 - x0) * (1 + 2 * margin), (y1 - y0) * (1 + 2 * margin)
        w1 = max(rw, rh * self.map_w / self.fb_h, 1e-6)
        c1 = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
        pitch = self.ref_pitch((c1[0], c1[1]))
        z1 = self.clamp_zoom(self.map_w / w1, pitch)
        w1 = self.map_w / z1
        self.zoom_vel = 0.0
        if complete:
            self.fly = None
            self.cx, self.cy, self.zoom = c1[0], c1[1], z1
            return
        c0 = np.array([self.cx, self.cy])
        w0 = self.map_w / self.zoom
        u1 = float(np.linalg.norm(c1 - c0))
        rho = FLY_RHO
        f = {"c0": c0, "c1": c1, "w0": w0, "w1": w1, "u1": u1, "z1": z1, "pitch": pitch}
        if u1 < 1e-6 * max(w0, w1):
            f["mode"] = "zoom"
            f["k"] = 1.0 if w1 > w0 else -1.0
            S = abs(math.log(w1 / w0)) / rho
        else:
            b0 = (w1 * w1 - w0 * w0 + rho ** 4 * u1 * u1) / (2 * w0 * rho * rho * u1)
            b1 = (w1 * w1 - w0 * w0 - rho ** 4 * u1 * u1) / (2 * w1 * rho * rho * u1)
            r0 = math.log(-b0 + math.sqrt(b0 * b0 + 1))
            r1 = math.log(-b1 + math.sqrt(b1 * b1 + 1))
            S = (r1 - r0) / rho
            f.update(mode="pan", r0=r0, r1=r1)
        f["S"] = S
        f["T"] = min(max(S / FLY_V, 0.3), 1.2)
        f["t0"] = time.perf_counter()
        self.fly = f

    def update_fly(self):
        f = self.fly
        if f is None:
            return
        t = (time.perf_counter() - f["t0"]) / f["T"]
        if t >= 1.0:
            self.cx, self.cy, self.zoom = f["c1"][0], f["c1"][1], f["z1"]
            self.fly = None
            return
        s = f["S"] * t
        rho = FLY_RHO
        if f["mode"] == "zoom":
            w = f["w0"] * math.exp(f["k"] * rho * s)
            u = 0.0
        else:
            r0, w0 = f["r0"], f["w0"]
            u = (w0 / rho ** 2) * math.cosh(r0) * math.tanh(rho * s + r0) \
                - (w0 / rho ** 2) * math.sinh(r0)
            w = w0 * math.cosh(r0) / math.cosh(rho * s + r0)
        frac = u / f["u1"] if f["u1"] > 0 else 1.0
        c = f["c0"] + (f["c1"] - f["c0"]) * frac
        self.cx, self.cy = float(c[0]), float(c[1])
        self.zoom = self.clamp_zoom(self.map_w / w, f["pitch"])

    # ---- input ----------------------------------------------------------
    def on_mouse_button(self, win, button, action, mods):
        if self.scripted:             # real input must not disturb a scripted run
            return
        x, y = self.cursor_pt if self.cursor_pt else glfw.get_cursor_pos(win)
        sx, sy = x * self.px, y * self.px
        if action == glfw.PRESS:
            self.fly = None
            self.zoom_vel = 0.0
            self.drag = (x, y)
            self.press = (x, y)
        else:
            if (self.press is not None and abs(x - self.press[0]) < 3
                    and abs(y - self.press[1]) < 3 and button == glfw.MOUSE_BUTTON_LEFT):
                self.click(sx, sy)
            self.drag = None
            self.press = None

    def click(self, sx, sy):
        if self.filter_rect and in_rect(sx, sy, self.filter_rect):
            self.filter_focus = True
            return
        if self.panel_rect and in_rect(sx, sy, self.panel_rect):
            row = self.panel_row_at(sy)
            if row is not None and self.panel_rows[row][2] >= 0:
                self.goto_result(self.panel_rows[row][2])
            return
        self.filter_focus = False

    def on_cursor(self, win, x, y):
        if self.scripted:             # real input must not disturb a scripted run
            return
        self.cursor_pt = (x, y)
        self.cursor = (x * self.px, y * self.px)
        if self.drag:
            dx, dy = (x - self.drag[0]) * self.px, (y - self.drag[1]) * self.px
            self.drag = (x, y)
            self.cx -= dx / self.zoom
            self.cy -= dy / self.zoom

    def on_enter(self, win, entered):
        if not entered:
            self.cursor = None

    def on_scroll(self, win, dx, dy):
        if self.scripted:             # real input must not disturb a scripted run
            return
        if self.cursor and self.panel_rect and in_rect(*self.cursor, self.panel_rect):
            self.panel_scroll = max(0.0, self.panel_scroll - dy * 3)   # rows, fractional
            return
        if self.cursor is None:
            return
        self.fly = None
        self.zoom_vel = min(max(self.zoom_vel + dy * ZOOM_TICK, -ZOOM_VMAX), ZOOM_VMAX)
        self.zoom_anchor = self.cursor

    def on_key(self, win, key, sc, action, mods):
        if self.scripted:             # real input must not disturb a scripted run
            return
        if action not in (glfw.PRESS, glfw.REPEAT):
            return
        if key == glfw.KEY_ESCAPE:
            if self.filter_text or self.filter_focus:
                self.set_filter("")
                self.filter_focus = False
            elif self.result_i >= 0:
                self.result_i = -1
                self.update_file_flags()
            return
        if key in (glfw.KEY_ENTER, glfw.KEY_KP_ENTER, glfw.KEY_DOWN):
            self.step(1)
        elif key == glfw.KEY_UP:
            self.step(-1)
        elif key == glfw.KEY_BACKSPACE and self.filter_focus:
            self.set_filter(self.filter_text[:-1])
        elif self.filter_focus:
            return                       # typed characters arrive in on_char
        elif key == glfw.KEY_RIGHT_BRACKET:
            self.step(1)
        elif key == glfw.KEY_LEFT_BRACKET:
            self.step(-1)
        elif key == glfw.KEY_R:
            self.fit()
        elif key == glfw.KEY_SLASH:
            self.filter_focus = True
            self.swallow_char = True
        elif key == glfw.KEY_Q:
            glfw.set_window_should_close(win, True)

    def on_char(self, win, code):
        if self.scripted:             # real input must not disturb a scripted run
            return
        if self.swallow_char:
            self.swallow_char = False
            return
        if self.filter_focus and 32 <= code < 127:
            self.set_filter(self.filter_text + chr(code))

    # ---- filter and results ---------------------------------------------
    def set_filter(self, text, sync=False):
        self.filter_text = text
        self.search_gen += 1
        gen = self.search_gen
        if len(text) < 2:
            self.set_results(None)
            return
        if sync:
            self.set_results(run_search(self.a, text))
            return
        threading.Thread(target=lambda: self.search_q.put((gen, run_search(self.a, text))),
                         daemon=True).start()

    def poll_search(self):
        while True:
            try:
                gen, res = self.search_q.get_nowait()
            except queue.Empty:
                return
            if gen == self.search_gen:
                self.set_results(res)

    def set_results(self, res):
        fitted = self.is_fitted()     # before the panel changes the map's width
        self.result_i = -1
        self.panel_scroll = 0
        self.hits_by_file[:] = False
        self.hit_count[:] = 0
        self.panel_rows = []
        if res is None or len(res["file"]) == 0:
            self.results = None if res is None else dict(res, order=np.zeros(0, np.int64))
            if self.results is not None:
                self.panel_rows = [("no hits", UI_DIM, -1)]
        else:
            # definitions first, then references, each by file then line
            key = np.lexsort((res["line"], res["file"], ~res["is_def"]))
            res = dict(res, order=key)
            self.results = res
            np.add.at(self.hit_count, res["file"], 1)
            self.hits_by_file = self.hit_count > 0
            self.build_panel_rows()
        self.update_file_flags()
        if fitted:                    # a fitted map stays fitted to the map viewport
            self.fit()

    def result(self, i):
        """(file, global line, col, len, is_def, kind) of result i in panel order."""
        r = self.results
        k = r["order"][i]
        return (int(r["file"][k]), int(r["line"][k]), int(r["col"][k]), r["len"],
                bool(r["is_def"][k]), r["kind"][k])

    def line_text(self, line):
        lo = self.a["line_off"]
        return self.a["chars"][lo[line]:lo[line + 1]].tobytes().decode("ascii", "replace")

    def panel_chars(self):
        """characters per results row: 180 points wide, 11 point text"""
        return int((180 - 10) / (11 * self.ui_aspect))

    def build_panel_rows(self):
        r = self.results
        width = self.panel_chars()
        n = len(r["order"])
        n_def = int(r["is_def"].sum())
        rows = []
        for title, lo, hi in (("Definitions", 0, n_def), ("References", n_def, n)):
            if hi <= lo:
                continue
            rows.append((f"{title} ({hi - lo})", YELLOW, -1))
            last_file = -1
            for i in range(lo, hi):
                f, line, col, ln, is_def, kind = self.result(i)
                if f != last_file:
                    path = self.paths[f]
                    if len(path) > width:            # keep the file name, lose the head
                        path = "…" + path[len(path) - width + 1:]
                    rows.append((path, UI_TEXT, -1))
                    last_file = f
                j = line - int(self.a["file_line0"][f]) + 1
                raw = self.line_text(line)
                body = raw.lstrip()
                c = col - (len(raw) - len(body))     # hit column in the stripped text
                room = width - 6 - (len(kind) + 1 if is_def else 0)
                if c + ln > room:                    # window the text so the hit shows
                    start = max(0, c - 8)
                    body = "…" + body[start:]
                text = f"{j:>5} {body}"
                if is_def:                   # kind tag at the right edge
                    room = width - len(kind) - 1
                    text = text[:room - 1] + "…" if len(text) > room else text.ljust(room)
                    text += " " + kind
                rows.append((text, UI_TEXT if is_def else UI_DIM, i))
        self.panel_rows = rows

    def update_file_flags(self):
        self.dimmed = np.zeros(self.n_files, bool)
        self.current_file = -1
        if self.results is not None and len(self.filter_text) >= 2:
            self.dimmed = ~self.hits_by_file
            if self.result_i >= 0:
                self.current_file = self.result(self.result_i)[0]

    def step(self, d):
        if self.results is None or len(self.results["order"]) == 0:
            return
        n = len(self.results["order"])
        self.goto_result((self.result_i + d) % n if self.result_i >= 0 else (0 if d > 0 else n - 1))

    def goto_result(self, i, complete=False):
        self.result_i = i
        self.update_file_flags()
        f, line, col, ln, _, _ = self.result(i)
        self.fly_to(self.line_rect(f, line, col + ln / 2), complete=complete)
        # keep the current row in view
        for k, row in enumerate(self.panel_rows):
            if row[2] == i:
                vis = self.panel_visible_rows()
                if k < self.panel_scroll or k >= self.panel_scroll + vis:
                    self.panel_scroll = max(0, k - vis // 2)
                break

    def line_rect(self, f, line, col=0.0, lines=40, cols=100):
        """World rect around a line (about `lines` by `cols` cells), so the
        destination of a fly-to is at the text rung."""
        p = float(self.a["file_pitch"][f])
        x, y = (float(v) for v in self.a["line_pos"][line])
        xc, yc = x + col * p * self.A, y + p / 2
        hw, hh = cols / 2 * p * self.A, lines / 2 * p
        return (xc - hw, yc - hh, xc + hw, yc + hh)

    def goto(self, spec, complete=True):
        """--goto path[:line]"""
        path, _, line = spec.partition(":")
        matches = [i for i, p in enumerate(self.paths)
                   if p == path or p.endswith("/" + path)]
        if not matches:
            raise SystemExit(f"error: --goto: no file matches '{path}'")
        f = min(matches, key=lambda i: len(self.paths[i]))
        if line and not line.isdigit():
            raise SystemExit("error: --goto: line must be an integer")
        l0, l1 = (int(v) for v in self.a["file_line0"][f:f + 2])
        if line:
            j = min(max(int(line) - 1, 0), max(l1 - l0 - 1, 0))
            if l1 > l0:
                self.fly_to(self.line_rect(f, l0 + j), complete=complete)
                return
        self.fly_to(tuple(float(v) for v in self.a["file_rect"][f]), complete=complete)

    # ---- hover ----------------------------------------------------------
    def build_hover_grid(self):
        a = self.a
        self.grid_n = (64, max(1, int(round(64 * self.H / self.W))))
        gx, gy = self.grid_n
        r = a["file_rect"]
        cx0 = np.clip((r[:, 0] / self.W * gx).astype(int), 0, gx - 1)
        cx1 = np.clip((r[:, 2] / self.W * gx).astype(int), 0, gx - 1)
        cy0 = np.clip((r[:, 1] / self.H * gy).astype(int), 0, gy - 1)
        cy1 = np.clip((r[:, 3] / self.H * gy).astype(int), 0, gy - 1)
        self.grid = [[[] for _ in range(gx)] for _ in range(gy)]
        for f in range(self.n_files):
            for y in range(cy0[f], cy1[f] + 1):
                for x in range(cx0[f], cx1[f] + 1):
                    self.grid[y][x].append(f)

    def file_at(self, wx, wy):
        gx, gy = self.grid_n
        ix, iy = int(wx / self.W * gx), int(wy / self.H * gy)
        if not (0 <= ix < gx and 0 <= iy < gy):
            return None
        r = self.a["file_rect"]
        for f in self.grid[iy][ix]:
            if r[f, 0] <= wx < r[f, 2] and r[f, 1] <= wy < r[f, 3]:
                return f
        return None

    def hover_at(self, sx, sy):
        wx, wy = self.screen_to_world(sx, sy)
        f = self.file_at(wx, wy)
        if f is None:
            return None
        a = self.a
        x0, y0 = a["file_rect"][f, :2]
        p, k, rows, colw = (float(a["file_pitch"][f]), int(a["file_cols"][f]),
                            int(a["file_rows"][f]), float(a["file_colw"][f]))
        n = int(a["file_line0"][f + 1] - a["file_line0"][f])
        c = min(max(int((wx - x0) // colw), 0), max(k - 1, 0))
        r = int(math.floor((wy - y0 - p) / p))
        j = c * rows + r if 0 <= r < rows else -1
        if j >= n:
            j = -1
        col = int((wx - x0 - c * colw) // (p * self.A))
        return f, j, col

    def enclosing_item(self, f, j):
        idx = self.items_by_file[f]
        if len(idx) == 0:
            return None
        s, e = self.a["item_start"][idx], self.a["item_end"][idx]
        m = (s <= j) & (e > j)
        if not m.any():
            return None
        cand = idx[m]
        return int(cand[np.argmin((e - s)[m])])

    def item_keyword(self, it, f):
        """The item's own keyword from its first line ('const', 'trait',
        'type'), falling back to the kind table when the line has none."""
        first = int(self.a["file_line0"][f] + self.a["item_start"][it])
        m = ITEM_KW_RX.search(self.line_text(first))
        if m:
            return m.group(1)
        names = ITEM_NAMES.get(self.langs[f], ITEM_NAMES[None])
        kind = int(self.a["item_kind"][it])
        return names[min(max(kind, 1), 5) - 1]

    def hover_label(self):
        if self.hover is None:
            return None
        f, j, col = self.hover
        text = self.paths[f]
        if self.rung[f] >= 2 and j >= 0:
            text += f":{j + 1}"
            it = self.enclosing_item(f, j)
            if it is not None:
                text += f" {self.item_keyword(it, f)} {self.item_names[it]}"
        if self.rung[f] == 3 and self.results is not None:
            text += f" · {int(self.hit_count[f])} hits"
        return text

    def build_dir_tags(self):
        """The tag each directory draws, or None. A directory whose inner
        rectangle is filled by exactly one child directory (at least 85
        percent of it) draws no tag; the innermost directory of such a
        chain draws one tag joining the chain's names ('platform · src ·
        os'), as the video collapses 'git · tests'. The crumb trail still
        uses the plain labels."""
        a = self.a
        r, pad = a["dir_rect"], a["dir_pad"]
        area = (r[:, 2] - r[:, 0]) * (r[:, 3] - r[:, 1])
        inner = (np.maximum(r[:, 2] - r[:, 0] - 2 * pad, 0)
                 * np.maximum(r[:, 3] - r[:, 1] - 2 * pad, 0))
        dirs = a["index"]["dirs"]
        self.dir_tags = [None] * self.n_dirs
        chain = [""] * self.n_dirs         # the names a hidden ancestor passes down
        for d in np.argsort(a["dir_depth"], kind="stable"):
            if d == 0:
                continue
            name = self.dir_labels[d].rstrip("/")
            prefix = chain[self.dir_parent[d]]
            kids = dirs[d]["children"]
            hidden = len(kids) == 1 and area[kids[0]] >= 0.85 * inner[d]
            if hidden:
                chain[d] = f"{prefix} · {name}" if prefix else name
            else:
                self.dir_tags[d] = f"{prefix} · {name}" if prefix else self.dir_labels[d]

    def update_crumb(self):
        a = self.a
        r = a["dir_rect"]
        side = np.minimum(r[:, 2] - r[:, 0], r[:, 3] - r[:, 1]) * self.zoom
        inside = ((r[:, 0] <= self.cx) & (self.cx < r[:, 2]) & (r[:, 1] <= self.cy)
                  & (self.cy < r[:, 3]) & (side >= 64))
        if not inside.any():
            self.crumb = f"← {self.name}"
            return
        d = int(np.flatnonzero(inside)[np.argmax(a["dir_depth"][inside])])
        parts = []
        while d > 0:
            parts.append(self.dir_labels[d].rstrip("/"))
            d = int(self.dir_parent[d])
        self.crumb = "← " + " › ".join([self.name] + parts[::-1])

    # ---- frame ----------------------------------------------------------
    def update_per_file(self):
        a = self.a
        x0, y0, x1, y1 = self.view()
        ppl = a["file_pitch"] * self.zoom
        self.rung = ((ppl >= 1).astype(np.uint8) + (ppl >= 3) + (ppl >= 6)).astype(np.uint8)
        r = a["file_rect"]
        self.visible = (r[:, 2] > x0) & (r[:, 0] < x1) & (r[:, 3] > y0) & (r[:, 1] < y1)
        flags = np.zeros(self.n_files, np.uint8)
        cur = self.cursor_override if self.scripted else self.cursor
        over_ui = cur is not None and any(rc and in_rect(cur[0], cur[1], rc)
                                          for rc in (self.panel_rect, self.filter_rect))
        self.hover = (self.hover_at(*cur) if cur is not None and self.drag is None
                      and not over_ui else None)
        if self.hover is not None:
            flags[self.hover[0]] |= 1
        if self.results is not None and len(self.filter_text) >= 2:
            flags[self.hits_by_file] |= 2
            if self.current_file >= 0:
                flags[self.current_file] |= 4
            flags[self.dimmed] |= 8
        # rung 0 draws every step-th line so the density stays about one
        # bar per pixel row; the step goes to the shader as two bytes
        self.file_step = np.ones(self.n_files, np.int64)
        sub = ppl < 1
        self.file_step[sub] = np.clip(np.ceil(1.0 / np.maximum(ppl[sub], 1e-9)), 1, 65535)
        fu = self.file_u.reshape(-1, 4)
        fu[:self.n_files, 0] = self.rung
        fu[:self.n_files, 1] = flags
        fu[:self.n_files, 2] = self.file_step & 255
        fu[:self.n_files, 3] = self.file_step >> 8
        glActiveTexture(GL_TEXTURE6)
        glBindTexture(GL_TEXTURE_2D, self.tex_file_u)
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, TEX_W, self.file_u.shape[0],
                        GL_RGBA_INTEGER, GL_UNSIGNED_BYTE, self.file_u)
        self.update_crumb()

    def draw_visible_lines(self):
        """One instanced draw per run of consecutive visible files: files
        are laid out depth-first so a visible region is a few contiguous
        line ranges, and the vertex shader no longer visits every line of
        the corpus each frame."""
        vis = self.visible
        if not vis.any():
            return
        edges = np.flatnonzero(np.diff(np.concatenate(([False], vis, [False]))))
        l0 = self.a["file_line0"]
        glUseProgram(self.prog["line"])
        loc = self.uniform("line", "uBase")
        glBindVertexArray(self.quad_vao)
        for f0, f1 in zip(edges[::2], edges[1::2]):
            base, end = int(l0[f0]), int(l0[f1])
            if end > base:
                glUniform1i(loc, base)
                glDrawArraysInstanced(GL_TRIANGLES, 0, 6, end - base)
        self.line_draws = len(edges) // 2

    def draw_instanced(self, prog, n):
        glUseProgram(self.prog[prog])
        glBindVertexArray(self.quad_vao)
        glDrawArraysInstanced(GL_TRIANGLES, 0, 6, n)

    def draw_rects(self, inst, world=True):
        """inst: float32 (n, 10): x0 y0 x1 y1 r g b a border_px fill_alpha."""
        if len(inst) == 0:
            return
        inst = np.ascontiguousarray(inst, np.float32)
        self.set_camera_uniforms("rect", world)
        glBindVertexArray(self.rect_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.rect_vbo)
        glBufferData(GL_ARRAY_BUFFER, inst.nbytes, inst, GL_STREAM_DRAW)
        glDrawArraysInstanced(GL_TRIANGLES, 0, 6, len(inst))

    def draw_text(self, inst):
        """inst: float32 (n, 9): x y w h cell r g b a in device pixels."""
        if len(inst) == 0:
            return
        inst = np.ascontiguousarray(inst, np.float32)
        glUseProgram(self.prog["text"])
        glUniform2f(self.uniform("text", "uViewport"), self.fb_w, self.fb_h)
        glBindVertexArray(self.text_vao)
        glBindBuffer(GL_ARRAY_BUFFER, self.text_vbo)
        glBufferData(GL_ARRAY_BUFFER, inst.nbytes, inst, GL_STREAM_DRAW)
        glDrawArraysInstanced(GL_TRIANGLES, 0, 6, len(inst))

    def text_run(self, x, y, size, text, color, alpha=1.0, max_chars=None):
        """Per-character instances for one string; returns (array, width)."""
        if max_chars is not None and len(text) > max_chars:
            text = text[:max(max_chars - 1, 0)] + "…" if max_chars > 0 else ""
        cw = size * self.ui_aspect
        n = len(text)
        inst = np.zeros((n, 9), np.float32)
        inst[:, 0] = x + np.arange(n) * cw
        inst[:, 1] = y
        inst[:, 2], inst[:, 3] = cw, size
        inst[:, 4] = [atlas_font.glyph_cell(c, self.gm) for c in text]
        inst[:, 5:8] = color
        inst[:, 8] = alpha
        return inst, n * cw

    def band_instances(self):
        """The kind bands: below 3 px per line the item rectangles of visible
        files are filled bands in the item kind colour, under the sampled
        bars and the line bars alike so nothing flips at the 1 px cut; from
        3 px the outlines take over (fills far, outlines near)."""
        a = self.a
        m = (self.visible & (self.rung <= 1))[self.item_rect_file]
        if not m.any():
            return np.zeros((0, 10), np.float32)
        rects = a["item_rect"][m]
        kinds = a["item_kind"][a["item_rect_item"][m]]
        inst = np.zeros((len(rects), 10), np.float32)
        inst[:, :4] = rects
        for k, c in ITEM_COLORS.items():
            inst[kinds == k, 4:7] = c
        inst[(kinds < 1) | (kinds > 5), 4:7] = ITEM_COLORS[5]
        inst[:, 7] = 1.0
        inst[:, 8] = 0.0
        inst[:, 9] = 0.18
        return inst

    def item_instances(self):
        a = self.a
        m = (self.visible & (self.rung >= 2))[self.item_rect_file]
        if not m.any():
            return np.zeros((0, 10), np.float32)
        rects = a["item_rect"][m]
        kinds = a["item_kind"][a["item_rect_item"][m]]
        inst = np.zeros((len(rects), 10), np.float32)
        inst[:, :4] = rects
        for k, c in ITEM_COLORS.items():
            inst[kinds == k, 4:7] = c
        inst[(kinds < 1) | (kinds > 5), 4:7] = ITEM_COLORS[5]
        inst[:, 7] = 0.6
        inst[:, 8] = 1.5
        return inst

    def hit_instances(self):
        r = self.results
        if r is None or len(r["order"]) == 0:
            return np.zeros((0, 10), np.float32)
        a = self.a
        f = r["file"]
        m = self.visible[f] & (self.rung[f] >= 2)
        if not m.any():
            return np.zeros((0, 10), np.float32)
        idx = np.flatnonzero(m)
        ff = f[idx]
        p = a["file_pitch"][ff]
        cap = a["file_cap"][ff].astype(np.float64)
        pos = a["line_pos"][r["line"][idx]].astype(np.float64)
        c0 = np.minimum(r["col"][idx], cap) * p * self.A
        c1 = np.minimum(r["col"][idx] + r["len"], cap) * p * self.A
        inst = np.zeros((len(idx), 10), np.float32)
        inst[:, 0] = pos[:, 0] + c0
        inst[:, 1] = pos[:, 1]
        inst[:, 2] = pos[:, 0] + c1
        inst[:, 3] = pos[:, 1] + p
        inst[:, 4:7] = YELLOW
        inst[:, 7] = 1.0
        inst[:, 8] = 2.0
        if self.result_i >= 0:
            cur = np.flatnonzero(idx == r["order"][self.result_i])
            inst[cur, 9] = 0.25
        return inst

    def panel_visible_rows(self):
        if self.panel_rect is None:
            return 1
        return max(1, int((self.panel_rect[3] - self.panel_rect[1] - 8 * self.px) // (13 * self.px)))

    def panel_row_at(self, sy):
        if self.panel_rect is None:
            return None
        k = int((sy - self.panel_rect[1] - 4 * self.px) // (13 * self.px)) + int(self.panel_scroll)
        return k if 0 <= k < len(self.panel_rows) else None

    def draw_ui(self):
        s = self.px
        boxes, texts = [], []
        size, small = 13 * s, 11 * s
        cw, cws = size * self.ui_aspect, small * self.ui_aspect
        m, pad = MARGIN_PT * s, 5 * s
        a = self.a

        def box(x0, y0, x1, y1, color, alpha, border=0.0):
            """filled box at alpha, or (border > 0) an outline of that width"""
            boxes.append([x0, y0, x1, y1, *color, 1.0, border, 0.0 if border > 0 else alpha])

        def label(x, y, text, sz=size, color=UI_TEXT, alpha=1.0, max_chars=None):
            inst, w = self.text_run(x, y, sz, text, color, alpha, max_chars)
            texts.append(inst)
            return w

        def flush():
            if boxes:
                self.draw_rects(np.array(boxes, np.float32).reshape(-1, 10), world=False)
            if texts:
                self.draw_text(np.concatenate(texts))
            boxes.clear()
            texts.clear()

        # directory labels: tags at the padded top-left of visible directories
        r = a["dir_rect"]
        side = np.minimum(r[:, 2] - r[:, 0], r[:, 3] - r[:, 1]) * self.zoom
        x0, y0, x1, y1 = self.view()
        vis = ((r[:, 2] > x0) & (r[:, 0] < x1) & (r[:, 3] > y0) & (r[:, 1] < y1)
               & (a["dir_depth"] >= 1) & (side >= 48))
        placed = []
        bh = size + 2 * pad * 0.6
        for d in sorted(np.flatnonzero(vis), key=lambda d: a["dir_depth"][d]):
            text = self.dir_tags[d]
            if text is None:               # hidden by a label chain
                continue
            bw = len(text) * cw + 2 * pad
            if bw > 0.4 * (r[d, 2] - r[d, 0]) * self.zoom:
                continue
            sx, sy = self.world_to_screen(r[d, 0] + a["dir_pad"][d], r[d, 1] + a["dir_pad"][d])
            # a child's corner is inset by its padding only, so its tag would
            # sit on the parent's: slide it right along the top edge, past
            # the tag in the way (the tags then read as a trail, each on its
            # own directory's edge); down only if the right edge runs out
            right = self.world_to_screen(r[d, 2] - a["dir_pad"][d], 0)[0]
            ox = sx
            for _ in range(8):
                hit = next((b for b in placed
                            if sx < b[2] and sx + bw > b[0] and sy < b[3] and sy + bh > b[1]), None)
                if hit is None:
                    break
                sx = hit[2] + 2 * s
                if sx + bw > right:
                    sx, sy = ox, sy + bh + 2 * s
            placed.append((sx, sy, sx + bw, sy + bh))
            box(sx, sy, sx + bw, sy + bh, UI_BOX, 0.85)
            label(sx + pad, sy + pad * 0.6, text)
        glEnable(GL_SCISSOR_TEST)          # labels stay inside the map viewport
        glScissor(0, 0, int(self.map_w), self.fb_h)
        flush()                            # and go under the panels
        glDisable(GL_SCISSOR_TEST)

        # crumb trail, bottom left
        bw = len(self.crumb) * cw + 2 * pad
        box(m, self.fb_h - m - size - 2 * pad * 0.6, m + bw, self.fb_h - m, UI_BOX, 0.85)
        label(m + pad, self.fb_h - m - size - pad * 0.6, self.crumb)

        # the right column: status line, filter box, results panel. With the
        # panel open the column is its own strip beside the map and the
        # status wraps to the panel's width; otherwise the status line and
        # the filter box sit in the top right corner over the map.
        pw = PANEL_PT * s
        cx0, cx1 = self.fb_w - m - pw, self.fb_w - m
        status = f"{self.n_files} files · {self.n_lines} lines · {self.n_chars} chars"
        if self.results is not None and len(self.filter_text) >= 2:
            n = len(self.results["order"])
            n_def = int(self.results["is_def"].sum())
            status = (f"{n_def} definitions · {n - n_def} references · "
                      f"{int(self.hits_by_file.sum())} files · {self.result_i + 1}/{n}")
        if self.panel_rows:
            parts = wrap_parts(status, self.panel_chars())
            row_h = small + 2 * s
            sy1 = m + len(parts) * row_h + 2 * pad * 0.6
            box(cx0, m, cx1, sy1, UI_BOX, 0.85)
            for k, part in enumerate(parts):
                label(cx0 + pad, m + pad * 0.6 + k * row_h, part, small)
            fy0 = sy1 + m
        else:
            bw = len(status) * cw + 2 * pad
            box(self.fb_w - m - bw, m, self.fb_w - m, m + size + 2 * pad * 0.6, UI_BOX, 0.85)
            label(self.fb_w - m - bw + pad, m + pad * 0.6, status)
            fy0 = m + size + 2 * pad * 0.6 + m
        fy1 = fy0 + size + 2 * pad * 0.6
        self.filter_rect = (cx0, fy0, cx1, fy1)
        box(*self.filter_rect, UI_BOX, 0.85)
        box(*self.filter_rect, YELLOW if self.filter_focus else UI_DIM, 1.0, border=1.0 * s)
        if self.filter_text or self.filter_focus:
            label(self.filter_rect[0] + pad, fy0 + pad * 0.6,
                  self.filter_text + ("_" if self.filter_focus else ""),
                  max_chars=int((pw - 2 * pad) / cw))
        else:
            label(self.filter_rect[0] + pad, fy0 + pad * 0.6, "/ filter", color=UI_DIM)

        # results panel down the rest of the column
        if self.panel_rows:
            py0, py1 = fy1 + m, self.fb_h - m
            self.panel_rect = (cx0, py0, cx1, py1)
            box(*self.panel_rect, UI_BOX, 0.85)
            row_h = 13 * s
            n_vis = self.panel_visible_rows()
            self.panel_scroll = min(self.panel_scroll, max(0, len(self.panel_rows) - n_vis))
            top = int(self.panel_scroll)
            max_chars = self.panel_chars()
            for k in range(top, min(len(self.panel_rows), top + n_vis)):
                text, color, ri = self.panel_rows[k]
                y = py0 + 4 * s + (k - top) * row_h
                if ri >= 0 and ri == self.result_i:
                    box(self.panel_rect[0] + 2 * s, y, self.panel_rect[2] - 2 * s, y + row_h,
                        YELLOW, 0.25)
                label(self.panel_rect[0] + pad, y + (row_h - small) / 2, text, small, color,
                      max_chars=max_chars)
        else:
            self.panel_rect = None

        # hover label next to the cursor
        text = self.hover_label()
        cur = self.cursor_override if self.scripted else self.cursor
        if text and cur is not None:
            bw = len(text) * cw + 2 * pad
            bh = size + 2 * pad * 0.6
            hx, hy = cur[0] + 16 * s, cur[1] + 16 * s
            if hx + bw > self.map_w - m:
                hx = max(m, cur[0] - 16 * s - bw)
            if hy + bh > self.fb_h - m:
                hy = cur[1] - 16 * s - bh
            box(hx, hy, hx + bw, hy + bh, UI_BOX, 0.85)
            label(hx + pad, hy + pad * 0.6, text)

        flush()

    def render(self):
        self.update_sizes()
        glViewport(0, 0, self.fb_w, self.fb_h)
        glClearColor(BG[0], BG[1], BG[2], 1.0)
        glClear(GL_COLOR_BUFFER_BIT)
        self.update_per_file()
        # the map in its own viewport, the window minus the panel column
        glViewport(0, 0, int(self.map_w), self.fb_h)
        for prog in ("dir", "file", "line"):
            self.set_camera_uniforms(prog)
        self.draw_instanced("dir", self.n_dirs)
        glUseProgram(self.prog["file"])
        glUniform1i(self.uniform("file", "uRingOnly"), 0)
        self.draw_instanced("file", self.n_files)
        self.draw_rects(self.band_instances())      # kind bands under the bars
        self.draw_visible_lines()
        self.draw_rects(self.item_instances())
        self.draw_rects(self.hit_instances())
        glUseProgram(self.prog["file"])          # borders over the text
        glUniform1i(self.uniform("file", "uRingOnly"), 1)
        self.draw_instanced("file", self.n_files)
        glViewport(0, 0, self.fb_w, self.fb_h)
        self.draw_ui()

    def screenshot(self, path):
        """Render one more frame and save it before the swap (after a swap
        the back buffer holds a stale frame on macOS)."""
        self.render()
        glFinish()
        buf = glReadPixels(0, 0, self.fb_w, self.fb_h, GL_RGB, GL_UNSIGNED_BYTE)
        img = np.frombuffer(buf, np.uint8).reshape(self.fb_h, self.fb_w, 3)[::-1]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(img).save(path)
        glfw.swap_buffers(self.win)
        print(f"screenshot -> {path}")

    def frame(self, dt):
        t = time.perf_counter()
        self.poll_search()
        self.update_glide(dt)
        self.update_fly()
        self.render()
        glfw.swap_buffers(self.win)
        glfw.poll_events()
        self.frame_times.append(time.perf_counter() - t)

    def print_stats(self):
        ft = np.array(self.frame_times[5:] or self.frame_times) * 1000.0
        print(f"load {self.load_seconds * 1000:.0f} ms, GPU setup {self.gpu_seconds * 1000:.0f} ms, "
              f"GPU memory {self.gpu_bytes / 1e6:.1f} MB")
        if len(ft):
            print(f"frames {len(ft)}: mean {ft.mean():.2f} ms, p99 {np.percentile(ft, 99):.2f} ms, "
                  f"max {ft.max():.2f} ms")

    # ---- runs -----------------------------------------------------------
    def apply_script_flags(self):
        args = self.args
        held = False
        if args.goto:
            self.goto(args.goto, complete=True)
            held = True
        if args.zoom is not None:
            self.set_zoom_ppl(args.zoom)
            held = True
        if args.filter:
            self.set_filter(args.filter, sync=True)
            held = True
        if args.step is not None:
            if self.results is None or len(self.results["order"]) == 0:
                raise SystemExit("error: --step needs results; give --filter WORD with hits")
            self.goto_result((args.step - 1) % len(self.results["order"]), complete=True)
            held = True
        return held

    def run_scripted(self):
        """--frames N: N frames of the opening zoom (fit to 16 px per line
        for the file at the corpus centre, through all four rungs) unless a
        flag pins the view, then --screenshot / --stats."""
        n = self.args.frames if self.args.frames is not None else 240
        held = self.apply_script_flags()
        centre = (self.W / 2, self.H / 2)
        z0, z1 = self.fit_zoom(), 16.0 / self.ref_pitch(centre)
        last = time.perf_counter()
        for k in range(n):
            if not held:
                u = k / max(n - 1, 1)
                self.fly, self.zoom_vel = None, 0.0
                self.cx, self.cy = centre
                self.zoom = z0 * (z1 / z0) ** u
                self.cursor_override = (self.map_w / 2, self.fb_h / 2)
            now = time.perf_counter()
            self.frame(now - last)
            last = now
            if glfw.window_should_close(self.win):
                break
        if self.args.screenshot:
            self.screenshot(self.args.screenshot)
        if self.args.stats:
            self.print_stats()
        print(f"frames: {len(self.frame_times)}")

    def run_shots(self):
        out = Path(self.args.shots)
        out.mkdir(parents=True, exist_ok=True)
        word = self.args.filter
        if not word:
            names = [n for n in self.item_names if len(n) >= 2]
            word = names[0] if names else "the"

        def snap(name):
            last = time.perf_counter()
            for _ in range(3):
                now = time.perf_counter()
                self.frame(now - last)
                last = now
            self.screenshot(out / name)

        centre = (self.W / 2, self.H / 2)
        self.fit()
        snap("overview.png")
        for name, ppl in (("bars.png", 2.0), ("tokens.png", 4.5), ("text.png", 16.0)):
            self.set_zoom_ppl(ppl, centre)
            snap(name)
        self.cursor_override = (self.map_w / 2, self.fb_h / 2)
        snap("hover.png")
        self.cursor_override = None
        self.fit()
        self.set_filter(word, sync=True)
        snap("filter.png")
        if self.results is not None and len(self.results["order"]):
            self.goto_result((self.args.step or 1) - 1, complete=True)
        snap("result.png")
        if self.args.stats:
            self.print_stats()

    def run_interactive(self):
        print(__doc__.split("Controls:")[1].strip())
        self.apply_script_flags()
        last = time.perf_counter()
        fps_t, fps_n = last, 0
        while not glfw.window_should_close(self.win):
            now = time.perf_counter()
            self.frame(now - last)
            last = now
            fps_n += 1
            if now - fps_t > 0.5:
                fps = fps_n / (now - fps_t)
                ppl = self.ref_pitch() * self.zoom
                glfw.set_window_title(
                    self.win, f"code atlas: {self.name} | {fps:5.1f} fps | "
                              f"{ppl:.1f} px/line at centre")
                fps_t, fps_n = now, 0
            if len(self.frame_times) > 10000:
                del self.frame_times[:5000]

    def run(self):
        print(f"{self.name}: {self.n_files} files, {self.n_lines} lines, {self.n_chars} chars, "
              f"{self.n_dirs} dirs; loaded in {self.load_seconds * 1000:.0f} ms, "
              f"GPU {self.gpu_bytes / 1e6:.1f} MB in {self.gpu_seconds * 1000:.0f} ms; "
              f"framebuffer {self.fb_w}x{self.fb_h} ({self.px:g}x)"
              + (f"; vector text from {VT_MIN_PPL:g} px per line" if self.vt is not None else ""))
        try:
            if self.args.shots:
                self.run_shots()
            elif self.scripted:
                self.run_scripted()
            else:
                self.run_interactive()
        finally:
            glfw.terminate()


def in_rect(x, y, r):
    return r[0] <= x < r[2] and r[1] <= y < r[3]


def wrap_parts(text, n):
    """Split a ' · ' separated status into lines of at most n characters."""
    lines, cur = [], ""
    for part in text.split(" · "):
        joined = part if not cur else cur + " · " + part
        if cur and len(joined) > n:
            lines.append(cur)
            cur = part
        else:
            cur = joined
    return lines + [cur]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("atlas", help="data/<name>_atlas directory")
    ap.add_argument("--font", default=atlas_font.DEFAULT_FONT,
                    help="monospace TTF/TTC/OTF for the glyph atlas")
    ap.add_argument("--font-index", type=int, default=0, help="face index in a TTC")
    ap.add_argument("--vector-text", action="store_true",
                    help=f"draw glyphs with the vector tier (vt_glyphs.py) from {VT_MIN_PPL:g} device px per line")
    ap.add_argument("--frames", type=int, default=None,
                    help="run N scripted frames and exit (self-test)")
    ap.add_argument("--screenshot", default=None, help="save the final frame to this PNG")
    ap.add_argument("--goto", default=None, metavar="PATH[:LINE]",
                    help="start with the fly-to this file (and line) complete")
    ap.add_argument("--zoom", type=float, default=None, metavar="PX_PER_LINE",
                    help="zoom so the file under the view centre has this many device px per line")
    ap.add_argument("--filter", default=None, metavar="WORD", help="apply a filter")
    ap.add_argument("--step", type=int, default=None, metavar="K", help="step to result K (1-based)")
    ap.add_argument("--shots", default=None, metavar="DIR",
                    help="write the standard screenshot set to DIR and exit")
    ap.add_argument("--stats", action="store_true",
                    help="print load time, GPU memory and frame times after the scripted frames")
    args = ap.parse_args()
    check_atlas(args.atlas)
    Viewer(args).run()


if __name__ == "__main__":
    main()
