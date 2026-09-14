#!/usr/bin/env python3
"""Stage 3: the viewer.

Draws a corpus laid out by atlas_layout.py as one zoomable surface with a
level of detail chosen per file from its size on screen: sampled
one pixel line bars over a dark tile (under 1 device pixel per line), grey
line bars (1 to 3), coloured token segments (3 to 6) and glyphs (6 and up),
from the raster atlas and, from VT_MIN_PPL, the vector tier. All lines,
tokens and characters of the corpus are resident on the GPU as integer and
float textures; every frame is a handful of instanced draws whose vertex
shaders cull by LOD and view.

The viewer does not know which corpus it shows. The index gives it a
hierarchy, the leaves' lines with a kind per character, the items, and an
optional tint per leaf with a one-word label; the layout gives it the
rectangles, a weight per leaf and whether the sheet is flat.

Controls: wheel = zoom about the cursor (with a glide) | drag = pan
          double-click = fly to the file or directory under the cursor,
          again at its extents = fly back | hover = file / line / item label
          R = reset the view | Q = quit
          / = focus the filter box, type to search, Backspace edits
          Enter, Down, ] = next hit | Up, [ = previous hit
          click a hit in the panel = fly there | Escape = clear the filter
          3 = the tilted projection on and off | Alt-drag = tilt and turn
          H = the heights on and off in 3D (files rise by their weight)
          C = the tint on and off, when the corpus has one
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
UI_UNIT = 11             # the texture unit of the UI's own glyph atlas
ZOOM_TAU = 0.12          # glide time constant, seconds
ZOOM_TICK = math.log(1.2) / ZOOM_TAU    # one wheel unit ends up as x1.2
ZOOM_VMAX = 12.0         # log-zoom per second at most
PANEL_PT = 180           # results panel width in window points
BAND_PT = 3              # the directory band's greatest width in window points
BAR_HEIGHT = 0.7         # bars and token blocks stand this much of a row tall (the shaders' barf)
INK_FAR = 0.55           # the bars' alpha at the far LODs when the scheme names no ink: the die shot
DBL_CLICK_S = 0.35       # two presses this close in time and place are a double click
MARGIN_PT = 10           # UI margin in window points
FLY_RHO = 1.4
FOVY = 45.0              # 3D camera field of view, degrees
H_MAX = 90.0             # tallest file in world units (the world is 1600 wide)
DIR_STEP = 5.0           # terrace height per directory level
TILT_MAX = 70.0
FLY_V = 10.0             # van Wijk path units per second
VT_MIN_PPL = 12.0        # the vector tier draws glyphs from here: the smallest size measured
                         # by tests/bench_vt.py at which its error is below the raster tier's


def rgb(h):
    return np.array([int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)], np.float32)


# the default scheme, the code atlas's: an index.json "scheme" overrides any key
SCHEME = {"ground": "1c1c1e", "page": "111114", "bar": "a4a8b0",
          "kinds": ["000000", "cfd2d8", "7aa2f7", "e0c080", "d7a0a8", "7f9f7f", "f0a060", "8c909a", "7fc8c8", "c8d08c"],
          "items": ["7aa2f7", "e0c080", "d7a0a8", "5fb7b7", "8c909a"]}
HUES = np.array([rgb(h) for h in ("6a8fd8", "4fb3a6", "7fbf6a", "e08a4a", "d8c050",
                                  "a57fd0", "a8865a", "d878a8", "60b8e0", "b0c860",
                                  "d86868", "9090d8")], np.float32)
ITEM_KW_RX = re.compile(r"\b(fn|struct|enum|trait|impl|mod|macro_rules|type|const|static|union|def|class|function|interface|namespace)\b")
ITEM_NAMES = {"rust": ["fn", "struct", "enum", "impl", "mod"],
              "python": ["def", "class", "enum", "impl", "mod"],
              None: ["function", "struct", "enum", "interface", "module"]}
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
    """The index and the layout as one dict of arrays, plus 'index' and
    'layout' for the two JSON files."""
    d = Path(d)
    ix = np.load(d / "index.npz")
    a = {k: ix[k] for k in ix.files}
    a["index"] = json.loads((d / "index.json").read_text())
    ly = np.load(d / "layout.npz")
    a.update({k: ly[k] for k in ly.files})
    a["layout"] = json.loads((d / "layout.json").read_text())
    if "row_pos" not in a:
        raise SystemExit(f"error: {d}/layout.npz predates wrapped rows; rerun ./atlas_layout.py {d}")
    return a


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
    """Whole-word search over the corpus bytes. Returns a dict of arrays
    (file, line, col) plus the hit length and the word, or None when the
    word is too short."""
    if len(word) < 2:
        return None
    w = word.encode(atlas["index"].get("encoding", "ascii"), "replace")
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
    line_off = atlas["line_off"].astype(np.int64)
    # a mention inside a comment or a string is not a hit
    code = ~np.isin(atlas["kinds"][line_off[line] + col], (4, 5))
    line, col = line[code], col[code]
    file = atlas["line_file"][line].astype(np.int64)
    return {"file": file, "line": line, "col": col, "len": len(w), "word": word}


def load_vector_tier(font, index, leading=1.0):
    """(curves, bands, metrics) of the vector glyph tier for the font, built
    in the same line box as the raster atlas so both tiers draw a glyph at
    the same size and baseline, with its bands widened for VT_MIN_PPL;
    cached under data/."""
    import vt_glyphs
    asc, desc, _, _, _ = atlas_font.font_box(font, index, leading)
    cache = HERE / "data" / f"vt_{Path(font).stem.lower()}_{index}_{asc}_{desc}_{VT_MIN_PPL:g}.npz"
    if cache.exists():
        return vt_glyphs.load(cache)
    t0 = time.perf_counter()
    curves, bands, metrics = vt_glyphs.build_atlas(font, index, min_ppl=VT_MIN_PPL, box=(asc, desc))
    cache.parent.mkdir(parents=True, exist_ok=True)
    vt_glyphs.save(cache, curves, bands, metrics)
    print(f"vector glyph data: {metrics['n_curves']} curves, {metrics['n_bands']} bands (fullest "
          f"{metrics['max_band']}), textures {curves.shape[1]}x{curves.shape[0]} and "
          f"{bands.shape[1]}x{bands.shape[0]}, built in {time.perf_counter() - t0:.1f} s -> {cache}")
    return curves, bands, metrics


# ---------------------------------------------------------------- viewer

class Viewer:
    def __init__(self, args):
        self.args = args
        self.scripted = args.frames is not None or args.shots or args.stats
        t0 = time.perf_counter()
        self.a = a = load_atlas(args.atlas)
        # the scheme: the adapter's colours and face, over the code atlas's defaults
        sc = dict(SCHEME, **a["index"].get("scheme", {}))
        self.bg = rgb(sc["ground"].lstrip("#"))
        self.page = rgb(sc["page"].lstrip("#"))
        self.bar = rgb(sc["bar"].lstrip("#"))
        self.band = rgb(sc["band"].lstrip("#")) if sc.get("band") else None   # one colour for every band, else the hues
        self.ink = np.array(sc["ink"], np.float32) if sc.get("ink") else None   # bars' alpha far and near, the blocks'; measured below when unset
        self.leading = float(sc.get("leading", 1.0))   # the line box over the ink extents: a book's air between lines
        self.kind_colors = np.array([rgb(h.lstrip("#")) for h in sc["kinds"]], np.float32)
        self.item_colors = {k + 1: rgb(h.lstrip("#")) for k, h in enumerate(sc["items"])}
        if sc.get("font") and args.font == atlas_font.DEFAULT_FONT:
            args.font = str(HERE / sc["font"])
        # the tint: a number in 0..1 per file and a label, when the index has them
        self.tint = a.get("file_tint")
        self.tint_label = a["index"].get("tint") if self.tint is not None else None
        self.tint_on = self.tint is not None and bool(getattr(args, "tint", False))
        self.hang = int(a["layout"].get("hang", 2))
        self.prop = bool(a["layout"].get("proportional", False))   # glyphs placed by their own advances
        # projection: 2d is orthographic; 3d is a perspective camera that
        # orbits the focus point (cx, cy) at tilt and yaw; with the heights
        # on, files and directories are extruded by their weight
        self.proj = getattr(args, "proj", None) or "2d"
        self.heights = bool(getattr(args, "heights", False))
        self.tilt = float(getattr(args, "tilt", None) if getattr(args, "tilt", None) is not None
                          else (55.0 if self.proj == "3d" else 0.0))
        self.yaw = float(getattr(args, "yaw", None) or 0.0)
        self.tilting = False
        self.M = np.eye(4)
        self.M_inv = np.eye(4)
        self.focus_w = 1.0
        self.z_focus = 0.0            # height the 3D camera orbits: the file top under the centre
        self.W, self.H = (float(v) for v in a["world"])
        self.A = float(a["layout"].get("char_aspect", 0.6))
        self.name = a["index"].get("name", Path(args.atlas).name)
        self.attach_atlas(a)
        # the corpus face's raster atlas, and the UI's own: the bundled monospace
        self.glyphs, self.gm = atlas_font.build_atlas(args.font, args.font_index, 64, self.leading)
        if args.font == atlas_font.DEFAULT_FONT and args.font_index == 0 and self.leading == 1.0:
            self.ui_glyphs, self.ui_gm = self.glyphs, self.gm
        else:
            self.ui_glyphs, self.ui_gm = atlas_font.build_atlas(atlas_font.DEFAULT_FONT, 0, 64)
        self.ui_aspect = self.ui_gm["cell_w"] / self.ui_gm["cell_h"]
        self.adv = np.zeros(224, np.float32)                       # advances in line heights, bytes 32..255
        self.adv[:len(self.gm["advances"])] = self.gm["advances"]
        if self.ink is None:
            # the scheme names no ink: measure it, the mean coverage of the
            # face's glyphs over this corpus's characters, so the blocks carry
            # the ink of the text they stand in for; the bars keep the bright
            # far view and ease down to the blocks by 3 px per line
            freq = np.bincount(a["chars"], minlength=256)[32:256]
            ink, _ = atlas_font.mean_ink(args.font, args.font_index, self.leading, freq)
            block = ink / BAR_HEIGHT
            self.ink = np.array([INK_FAR, block * 1.25, block], np.float32)
        self.vt = load_vector_tier(args.font, args.font_index, self.leading) if args.vector_text else None
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
        self.last_press = None        # (time, x, y) of the last left press, for double clicks
        self.zoomed = None            # {"rect", "back": (cx, cy, zoom)} after a double click
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
        self.current_file = -1
        self.result_i = -1
        self.panel_rows = []
        self.panel_scroll = 0
        self.panel_rect = None
        self.filter_rect = None
        self.search_gen = 0
        self.search_q = queue.Queue()
        self.frame_times = []
        self.fit()

    # ---- the corpus -----------------------------------------------------
    def attach_atlas(self, a):
        """Everything derived from the atlas on the CPU."""
        self.a = a
        self.n_files = len(a["file_rect"])
        self.n_lines = len(a["line_file"])
        self.n_rows = len(a["row_pos"])
        self.n_dirs = len(a["dir_rect"])
        self.file_scale = np.ones(self.n_files)
        self.dir_scale = np.ones(self.n_dirs)
        self.compute_heights()
        self.n_chars = len(a["chars"])
        if self.n_chars > CHARS_W * CHARS_W:
            raise SystemExit(f"error: {self.n_chars} characters exceed the "
                             f"{CHARS_W}x{CHARS_W} texture; the resident design stops here")
        self.paths = [f["path"] for f in a["index"]["files"]]
        self.langs = [f.get("lang") for f in a["index"]["files"]]
        self.refresh_dirs()
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
        self.hits_by_file = np.zeros(self.n_files, bool)
        self.hit_count = np.zeros(self.n_files, np.int64)
        self.dimmed = np.zeros(self.n_files, bool)
        self.lod = np.zeros(self.n_files, np.uint8)
        self.visible = np.zeros(self.n_files, bool)

    def refresh_dirs(self):
        """Directory labels, parents and children of the layout."""
        a = self.a
        jd = a["layout"]["dirs"]
        self.dir_labels = [d["label"] for d in jd]
        if jd and "parent" in jd[0]:
            self.dir_parent = np.array([d["parent"] for d in jd], np.int64)
            self.dir_children = [d["children"] for d in jd]
        else:
            self.dir_parent = np.array([d["parent"] for d in a["index"]["dirs"]], np.int64)
            self.dir_children = [d["children"] for d in a["index"]["dirs"]]

    def compute_heights(self):
        """z base and height per file and directory from the layout's
        weight: directories are terraces of DIR_STEP per level, files sit on
        their directory's terrace with a height of H_MAX * sqrt(weight /
        max). With the heights off (the default), or for a flat layout (a
        book), nothing rises: the 3D view is the sheet tilted."""
        a = self.a
        depth = a["dir_depth"].astype(np.float64)
        flat = bool(a["layout"].get("flat", False)) or not self.heights
        self.dir_z0 = np.maximum(depth - 1, 0) * (0.0 if flat else DIR_STEP)
        self.dir_h = np.where(depth >= 1, 0.0 if flat else DIR_STEP, 0.0)
        top = self.dir_z0 + self.dir_h
        m = a.get("file_weight")
        if m is None:
            m = np.ones(self.n_files)
        m = np.asarray(m, np.float64)
        self.file_h = H_MAX * np.sqrt(np.maximum(m, 0) / max(float(m.max()), 1e-9))
        if flat:
            self.file_h[:] = 0.0
        self.file_z0 = top[a["file_dir"]]
        self.file_top = self.file_z0 + self.file_h

    def set_heights(self, on):
        """The heights on or off: recompute and re-upload the file and
        directory textures (a small upload; the lines are untouched)."""
        if bool(self.a["layout"].get("flat", False)) or on == self.heights:
            return
        self.heights = bool(on)
        self.compute_heights()
        self.upload_layout()
        self.bind_textures()
        self.hover = None

    def set_tint(self, on):
        if self.tint is None:
            return
        self.tint_on = bool(on)

    def status_texts(self):
        """(the corpus or search line, the tint's label or '')."""
        status = f"{self.n_files:,} files · {self.n_lines:,} lines · {self.n_chars:,} chars"
        if self.results is not None and len(self.filter_text) >= 2:
            n = len(self.results["order"])
            status = f"{n:,} hits · {int(self.hits_by_file.sum()):,} files · {self.result_i + 1:,}/{n:,}"
        extra = self.tint_label if self.tint_on and self.tint_label else ""
        return status, extra

    # ---- window ---------------------------------------------------------
    def open_window(self):
        if not glfw.init():
            raise RuntimeError("glfw.init failed")
        glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
        glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
        glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
        glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
        self.win = glfw.create_window(1600, 1000, f"big-text: {self.name}", None, None)
        if not self.win:
            raise RuntimeError("window creation failed")
        glfw.make_context_current(self.win)
        glfw.swap_interval(0 if self.scripted else 1)
        # a window the screen cannot hold is clamped by macOS to a height that
        # varies by a point between runs (the reported work area itself
        # jitters by a point); size it to the work area explicitly, rounded
        # down to ten points with a little slack so the jitter cannot change
        # it, so the framebuffer, and every screenshot, is the same every time
        ax, ay, aw, ah = glfw.get_monitor_workarea(glfw.get_primary_monitor())
        fl, ft, fr, fb = glfw.get_window_frame_size(self.win)
        ww, wh = min(1600, aw - fl - fr), min(1000, (ah - ft - fb - 4) // 10 * 10)
        if (ww, wh) != tuple(glfw.get_window_size(self.win)):
            glfw.set_window_size(self.win, ww, wh)
            glfw.set_window_pos(self.win, ax + fl, ay + ft)
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
        # the map's viewport is the window minus a top strip (the filter
        # box), a bottom strip (the crumb trail and the status) and the right
        # column while the panel is open (map_w)
        s = self.px
        m, row = MARGIN_PT * s, 13 * s + 2 * 5 * s * 0.6      # a boxed row of 13 pt text
        self.top_h = m + row + m
        self.bottom_h = m + row + m
        self.map_y0 = self.top_h
        self.map_h = max(1.0, self.fb_h - self.top_h - self.bottom_h)

    def in_map(self, sx, sy):
        return 0 <= sx < self.map_w and self.map_y0 <= sy < self.map_y0 + self.map_h

    def setup_gl(self):
        a = self.a
        vt_src = ""
        if self.vt is not None:
            vt_src = "#define VT_TEXT 1\n" + (HERE / "shaders" / "vt_glyph.glsl").read_text()
        self.prog = {n: load_program(n, vt_src if n in ("line", "glyph") else "")
                     for n in ("dir", "file", "line", "rect", "text", "wall", "glyph")}
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
        self.rect_vao, self.rect_vbo = instanced_vao(self.quad_vbo, [(1, 4), (2, 4), (3, 2), (4, 1)])
        self.text_vao, self.text_vbo = instanced_vao(self.quad_vbo, [(1, 2), (2, 2), (3, 1), (4, 4)])
        glBindVertexArray(0)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)

        gpu = 0
        chars = padded_rows(a["chars"], CHARS_W)
        self.tex_chars = texture_2d(GL_R8UI, GL_RED_INTEGER, GL_UNSIGNED_BYTE, chars)
        self.tex_kinds = texture_2d(GL_R8UI, GL_RED_INTEGER, GL_UNSIGNED_BYTE,
                                    padded_rows(a["kinds"], CHARS_W))
        gpu += 2 * chars.size
        self.tex_line_f = self.tex_line_u = self.tex_file_f = self.tex_dir_f = self.tex_char_f = None
        gpu += self.upload_layout()
        self.file_u = padded_rows(np.zeros((self.n_files, 4), np.uint8), TEX_W)
        self.tex_file_u = texture_2d(GL_RGBA8UI, GL_RGBA_INTEGER, GL_UNSIGNED_BYTE, self.file_u)
        gpu += self.file_u.nbytes
        self.tex_glyphs = texture_2d(GL_R8, GL_RED, GL_UNSIGNED_BYTE,
                                     self.glyphs[:, :, None], GL_LINEAR, mipmap=True)
        gpu += int(self.glyphs.nbytes * 4 / 3)
        if self.ui_glyphs is self.glyphs:
            self.tex_ui = self.tex_glyphs
        else:
            self.tex_ui = texture_2d(GL_R8, GL_RED, GL_UNSIGNED_BYTE,
                                     self.ui_glyphs[:, :, None], GL_LINEAR, mipmap=True)
            gpu += int(self.ui_glyphs.nbytes * 4 / 3)
        self.vt_textures = ()
        if self.vt is not None:
            import vt_glyphs
            self.vt_textures = tuple(vt_glyphs.make_textures(self.vt[0], self.vt[1]))
            gpu += self.vt[0].nbytes + self.vt[1].nbytes
        self.gpu_bytes = gpu

        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDisable(GL_DEPTH_TEST)
        for n, p in self.prog.items():
            glUseProgram(p)
            for i, tex in enumerate(("uChars", "uKinds", "uGlyphs", "uLineF", "uLineU",
                                     "uFileF", "uFileU", "uDirF", "vt_curves", "vt_bands", "uCharF")):
                loc = glGetUniformLocation(p, tex)
                if loc >= 0:
                    glUniform1i(loc, UI_UNIT if n == "text" and tex == "uGlyphs" else i)
            loc = glGetUniformLocation(p, "uAdv")
            if loc >= 0:
                glUniform1fv(loc, 224, self.adv)
            loc = glGetUniformLocation(p, "uAdvMax")
            if loc >= 0:
                glUniform1f(loc, self.gm["char_aspect"])
            loc = glGetUniformLocation(p, "uProp")
            if loc >= 0:
                glUniform1i(loc, 1 if self.prop else 0)
            loc = glGetUniformLocation(p, "uHue")
            if loc >= 0:
                glUniform3fv(loc, 12, HUES)
            loc = glGetUniformLocation(p, "uKindColor")
            if loc >= 0:
                glUniform3fv(loc, 10, self.kind_colors)
            for name, col in (("uPage", self.page), ("uGround", self.bg), ("uBar", self.bar),
                              ("uBandColor", self.band if self.band is not None else self.bg)):
                loc = glGetUniformLocation(p, name)
                if loc >= 0:
                    glUniform3f(loc, *col)
            loc = glGetUniformLocation(p, "uBandFlat")
            if loc >= 0:
                glUniform1i(loc, 1 if self.band is not None else 0)
            loc = glGetUniformLocation(p, "uInk")
            if loc >= 0:
                glUniform3f(loc, *self.ink)
            loc = glGetUniformLocation(p, "uGlyph")
            if loc >= 0:
                gm, g = (self.ui_gm, self.ui_glyphs) if n == "text" else (self.gm, self.glyphs)
                glUniform4f(loc, gm["cell_w"], gm["cell_h"], g.shape[1], g.shape[0])
            loc = glGetUniformLocation(p, "uCharAspect")
            if loc >= 0:
                glUniform1f(loc, self.A)
            loc = glGetUniformLocation(p, "uVtMin")
            if loc >= 0:
                glUniform1f(loc, VT_MIN_PPL)
            loc = glGetUniformLocation(p, "uBandPx")
            if loc >= 0:
                glUniform1f(loc, BAND_PT * self.px)
        self.bind_textures()

    def z_scale(self):
        return 1.0 if self.proj == "3d" else 0.0

    def upload_layout(self):
        """(Re)upload the layout textures: one texel per visual row (position,
        row index in file; byte offset, file, indent | len), three per file
        and three per directory. Returns the bytes uploaded."""
        a = self.a
        for t in (self.tex_line_f, self.tex_line_u, self.tex_file_f, self.tex_dir_f):
            if t is not None:
                glDeleteTextures(1, [t])
        gpu = 0
        n_rows = self.n_rows
        rl = a["row_line"].astype(np.int64)
        lf = np.zeros((n_rows, 4), np.float32)
        lf[:, :2] = a["row_pos"]
        # z: the row's index within its file, for the LOD-0 sampling; w: the
        # row's width in line heights
        lf[:, 2] = np.arange(n_rows) - a["file_row0"][a["line_file"][rl]].astype(np.int64)
        if "row_width" in a:
            lf[:, 3] = a["row_width"]
        else:
            cap = a["file_cap"][a["line_file"][rl]].astype(np.int64)
            lf[:, 3] = np.minimum(a["row_len"].astype(np.int64), cap) * self.A
        buf = padded_rows(lf, TEX_W)
        self.tex_line_f = texture_2d(GL_RGBA32F, GL_RGBA, GL_FLOAT, buf)
        gpu += buf.nbytes
        if self.prop:                     # per character: its row and its x within the row
            row_len = a["row_len"].astype(np.int64)
            if int(row_len.sum()) != self.n_chars:
                raise SystemExit("error: the layout's rows do not cover the characters; rerun ./atlas_layout.py")
            cf = np.zeros((self.n_chars, 2), np.float32)
            cf[:, 0] = np.repeat(np.arange(n_rows, dtype=np.float32), row_len)
            cf[:, 1] = a["char_x"]
            if self.tex_char_f is not None:
                glDeleteTextures(1, [self.tex_char_f])
            buf = padded_rows(cf, CHARS_W)
            self.tex_char_f = texture_2d(GL_RG32F, GL_RG, GL_FLOAT, buf)
            gpu += buf.nbytes
        off = a["line_off"][:-1].astype(np.uint64)[rl] + a["row_col0"].astype(np.uint64)
        first = a["row_col0"] == 0
        indent = np.where(first, a["line_indent"][rl], 0).astype(np.uint32)
        lu = np.zeros((n_rows, 4), np.uint32)
        lu[:, 0] = (off & np.uint64(0xFFFFFFFF)).astype(np.uint32)
        lu[:, 1] = a["line_file"][rl]
        lu[:, 2] = indent | (a["row_len"].astype(np.uint32) << 16)
        lu[:, 3] = (off >> np.uint64(32)).astype(np.uint32)
        buf = padded_rows(lu, TEX_W)
        self.tex_line_u = texture_2d(GL_RGBA32UI, GL_RGBA_INTEGER, GL_UNSIGNED_INT, buf)
        gpu += buf.nbytes
        ff = np.zeros((3 * self.n_files, 4), np.float32)
        ff[0::3] = a["file_rect"]
        ff[1::3, 0] = a["file_pitch"]
        ff[1::3, 1] = a["file_colw"]
        ff[1::3, 2] = a["file_cap"]
        ff[1::3, 3] = a["file_hue"]
        ff[2::3, 0] = self.file_z0
        ff[2::3, 1] = self.file_h
        if self.tint is not None:
            ff[2::3, 2] = self.tint
        self.ff = ff
        buf = padded_rows(ff, TEX_W)
        self.tex_file_f = texture_2d(GL_RGBA32F, GL_RGBA, GL_FLOAT, buf)
        gpu += buf.nbytes
        df = np.zeros((3 * self.n_dirs, 4), np.float32)
        df[0::3] = a["dir_rect"]
        df[1::3, 0] = a["dir_pad"]
        df[1::3, 1] = a["dir_depth"]
        df[1::3, 2] = a["dir_hue"]
        df[2::3, 0] = self.dir_z0
        df[2::3, 1] = self.dir_h
        buf = padded_rows(df, TEX_W)
        self.tex_dir_f = texture_2d(GL_RGBA32F, GL_RGBA, GL_FLOAT, buf)
        gpu += buf.nbytes
        # directories draw in depth order so parents go under children
        self.dir_order = np.argsort(a["dir_depth"], kind="stable")
        return gpu

    def bind_textures(self):
        """Bind every texture to its unit, the vector tier's two included.
        Called after anything created or re-uploaded a texture: texture
        creation and glTexSubImage2D bind on whatever unit is active."""
        units = {0: self.tex_chars, 1: self.tex_kinds, 2: self.tex_glyphs, 3: self.tex_line_f,
                 4: self.tex_line_u, 5: self.tex_file_f, 6: self.tex_file_u, 7: self.tex_dir_f,
                 UI_UNIT: self.tex_ui}
        for i, tex in enumerate(self.vt_textures):
            units[8 + i] = tex
        if self.tex_char_f is not None:
            units[10] = self.tex_char_f
        for i, tex in units.items():
            glActiveTexture(GL_TEXTURE0 + i)
            glBindTexture(GL_TEXTURE_2D, tex)
        glActiveTexture(GL_TEXTURE0 + 5)

    def set_proj(self, proj):
        if proj == self.proj:
            return
        self.proj = proj
        if proj == "3d" and self.tilt == 0.0:
            self.tilt = 55.0
        self.hover = None

    def row_of(self, line, col):
        """(visual row, column within the row) of a character of a global
        line, following the wrap: the row of the line whose first column is
        the last at or before col."""
        a = self.a
        line = np.atleast_1d(np.asarray(line, np.int64))
        col = np.atleast_1d(np.asarray(col, np.int64))
        r0 = a["line_row0"][line].astype(np.int64)
        r1 = a["line_row0"][line + 1].astype(np.int64)
        row = r0.copy()
        col0 = a["row_col0"]
        for i in np.flatnonzero(r1 - r0 > 1):
            row[i] = r0[i] + max(int(np.searchsorted(col0[r0[i]:r1[i]], col[i], side="right")) - 1, 0)
        return row, col - col0[row].astype(np.int64)

    def col_offset(self, row, rc):
        """x of column rc of a visual row, from the row's left, in line
        heights: rc advances for a monospace face; for a proportional one
        the character's own x, or the row's width at its end."""
        a = self.a
        row = np.atleast_1d(np.asarray(row, np.int64))
        rc = np.atleast_1d(np.asarray(rc, np.int64))
        if not self.prop:
            return rc * self.A
        start = a["line_off"][a["row_line"][row]].astype(np.int64) + a["row_col0"][row].astype(np.int64)
        n = a["row_len"][row].astype(np.int64)
        inside = rc < n
        idx = np.minimum(start + np.minimum(rc, np.maximum(n - 1, 0)), self.n_chars - 1)
        return np.where(inside, a["char_x"][idx], a["row_width"][row])

    def uniform(self, prog, name):
        d = self.loc[prog]
        if name not in d:
            d[name] = glGetUniformLocation(self.prog[prog], name)
        return d[name]

    def camera_matrix(self):
        """World (x, y, z) to clip. 2D: orthographic, exactly the
        (world - offset) * scale mapping. 3D: a perspective camera at
        distance D from the focus (cx, cy, 0), tilted from top-down by
        self.tilt and turned by self.yaw, with D chosen so that one world
        unit at the focus is still self.zoom device pixels."""
        if self.proj != "3d":
            x0, y0, _, _ = self.view2d()
            sx, sy = 2.0 * self.zoom / self.map_w, 2.0 * self.zoom / self.map_h
            M = np.array([[sx, 0, 0, -1 - x0 * sx],
                          [0, -sy, 0, 1 + y0 * sy],
                          [0, 0, 0, 0],
                          [0, 0, 0, 1]], np.float64)
            self.focus_w = 1.0
        else:
            th, ph = math.radians(self.tilt), math.radians(self.yaw)
            tan_h = math.tan(math.radians(FOVY) / 2)
            D = self.map_h / (2.0 * self.zoom * tan_h)
            F = np.array([self.cx, self.cy, self.z_focus])
            eye = F + D * np.array([math.sin(th) * math.sin(ph), math.sin(th) * math.cos(ph), math.cos(th)])
            fwd = F - eye
            fwd /= np.linalg.norm(fwd)
            right = np.array([math.cos(ph), -math.sin(ph), 0.0])
            up = np.cross(fwd, right)
            V = np.eye(4)
            V[0, :3], V[1, :3], V[2, :3] = right, up, -fwd
            V[:3, 3] = -V[:3, :3] @ eye
            near, far = max(D * 0.02, 0.05), D * 12 + 4000.0
            aspect = self.map_w / self.map_h
            P = np.zeros((4, 4))
            P[0, 0] = 1.0 / (tan_h * aspect)
            P[1, 1] = 1.0 / tan_h
            P[2, 2] = -(far + near) / (far - near)
            P[2, 3] = -2.0 * far * near / (far - near)
            P[3, 2] = -1.0
            M = P @ V
            self.focus_w = D
            self.eye = eye
        self.M = M
        try:
            self.M_inv = np.linalg.inv(M) if self.proj == "3d" else None
        except np.linalg.LinAlgError:
            self.M_inv = None
        return M

    def set_camera_uniforms(self, prog, world=True):
        """world: the map's viewport (the window minus the panel column);
        else the whole window in device pixels for the UI"""
        p = self.prog[prog]
        glUseProgram(p)
        loc = self.uniform(prog, "uMVP")
        if world:
            glUniformMatrix4fv(loc, 1, GL_TRUE, self.M.astype(np.float32))
            glUniform4f(self.uniform(prog, "uView"), *self.view())
            glUniform1f(self.uniform(prog, "uScale"), self.zoom)
            glUniform1f(self.uniform(prog, "uFocusW"), self.focus_w)
            glUniform1f(self.uniform(prog, "uZScale"), self.z_scale())
            glUniform1i(self.uniform(prog, "uWorld"), 1)
            glUniform2f(self.uniform(prog, "uViewport"), self.map_w, self.map_h)
        else:
            glUniformMatrix4fv(loc, 1, GL_TRUE, np.eye(4, dtype=np.float32))
            glUniform1i(self.uniform(prog, "uWorld"), 0)
            glUniform2f(self.uniform(prog, "uViewport"), self.fb_w, self.fb_h)

    def unproject(self, sx, sy, wz=0.0):
        """World point on the plane z = wz under device pixel (sx, sy) in
        the 3D camera, or None if the ray does not hit it in front."""
        nx, ny = sx / self.map_w * 2.0 - 1.0, 1.0 - (sy - self.map_y0) / self.map_h * 2.0
        p0 = self.M_inv @ np.array([nx, ny, -1.0, 1.0])
        p1 = self.M_inv @ np.array([nx, ny, 1.0, 1.0])
        p0, p1 = p0[:3] / p0[3], p1[:3] / p1[3]
        d = p1 - p0
        if abs(d[2]) < 1e-12:
            return None
        t = (wz - p0[2]) / d[2]
        if t < 0:
            return None
        return p0 + t * d

    def project(self, wx, wy, wz=0.0):
        c = self.M @ np.array([wx, wy, wz, 1.0])
        w = c[3] if abs(c[3]) > 1e-9 else 1e-9
        return ((c[0] / w + 1.0) * 0.5 * self.map_w, (1.0 - c[1] / w) * 0.5 * self.map_h + self.map_y0)

    # ---- camera ---------------------------------------------------------
    @property
    def map_w(self):
        """Width of the map's viewport in device pixels: the window, minus
        the right column (panel plus margins) while the results panel is open."""
        column = (PANEL_PT + 2 * MARGIN_PT) * self.px if self.panel_rows else 0.0
        return self.fb_w - column

    def view2d(self):
        w, h = self.map_w / self.zoom, self.map_h / self.zoom
        return (self.cx - w / 2, self.cy - h / 2, self.cx + w / 2, self.cy + h / 2)

    def view(self):
        """The world rectangle in view: exact in 2D; in 3D the bounding box
        of the viewport corners unprojected onto the plane (rays that miss
        the plane count as far away), clamped to a few worlds around."""
        if self.proj != "3d" or self.M_inv is None:
            return self.view2d()
        pts = []
        far = 3.0 * max(self.W, self.H)
        for wz in (0.0, self.z_focus, self.z_focus + H_MAX):
            for sx, sy in ((0, self.map_y0), (self.map_w, self.map_y0),
                           (0, self.map_y0 + self.map_h), (self.map_w, self.map_y0 + self.map_h)):
                p = self.unproject(sx, sy, wz)
                if p is None:
                    pts.append((self.cx - far, self.cy - far)); pts.append((self.cx + far, self.cy + far))
                else:
                    pts.append((p[0], p[1]))
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (max(min(xs), -far), max(min(ys), -far), min(max(xs), self.W + far), min(max(ys), self.H + far))

    def fit_zoom(self):
        return min(self.map_w * 0.94 / self.W, self.map_h * 0.94 / self.H)

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
        if self.proj == "3d":             # perspective: back off until the map fits
            self.z_focus = 0.0
            corners = np.array([[0, 0], [self.W, 0], [0, self.H], [self.W, self.H]], np.float64)
            for _ in range(12):
                self.camera_matrix()
                c = np.concatenate((corners, np.zeros((4, 1)), np.ones((4, 1))), axis=1) @ self.M.T
                w = np.maximum(c[:, 3], 1e-6)
                over = float(max(np.abs(c[:, 0] / w).max(), np.abs(c[:, 1] / w).max()))
                if over <= 0.97:
                    break
                self.zoom /= over * 1.04

    def screen_to_world(self, sx, sy, wz=None):
        if self.proj == "3d" and self.M_inv is not None:
            p = self.unproject(sx, sy, self.z_focus if wz is None else wz)
            if p is None:                 # above the horizon: far along the view
                return (self.cx + (sx - self.map_w / 2) * 50.0 / self.zoom,
                        self.cy - 50.0 * self.map_h / self.zoom)
            return (float(p[0]), float(p[1]))
        return (self.cx + (sx - self.map_w / 2) / self.zoom,
                self.cy + (sy - self.map_y0 - self.map_h / 2) / self.zoom)

    def world_to_screen(self, wx, wy, wz=0.0):
        if self.proj == "3d":
            return self.project(wx, wy, wz)
        return ((wx - self.cx) * self.zoom + self.map_w / 2,
                (wy - self.cy) * self.zoom + self.map_y0 + self.map_h / 2)

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
        if self.proj == "3d":             # keep the plane point under the cursor
            self.camera_matrix()
            nx, ny = self.screen_to_world(sx, sy)
            self.cx += wx - nx
            self.cy += wy - ny
            return
        self.cx = wx - (sx - self.map_w / 2) / z
        self.cy = wy - (sy - self.map_y0 - self.map_h / 2) / z

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

    def fly_target(self, rect, margin=0.06):
        """(centre, zoom, view width, pitch) of the view that holds rect with a margin."""
        x0, y0, x1, y1 = rect
        rw, rh = (x1 - x0) * (1 + 2 * margin), (y1 - y0) * (1 + 2 * margin)
        w1 = max(rw, rh * self.map_w / self.map_h, 1e-6)
        c1 = np.array([(x0 + x1) / 2, (y0 + y1) / 2])
        pitch = self.ref_pitch((c1[0], c1[1]))
        z1 = self.clamp_zoom(self.map_w / w1, pitch)
        return c1, z1, self.map_w / z1, pitch

    def fly_to(self, rect, margin=0.06, complete=False):
        """van Wijk & Nuij smooth zoom-and-pan to a view containing rect."""
        c1, z1, w1, pitch = self.fly_target(rect, margin)
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
    def over_ui(self, sx, sy):
        return not self.in_map(sx, sy) or any(
            rc and in_rect(sx, sy, rc) for rc in (self.panel_rect, self.filter_rect))

    def on_mouse_button(self, win, button, action, mods):
        if self.scripted:             # real input must not disturb a scripted run
            return
        x, y = self.cursor_pt if self.cursor_pt else glfw.get_cursor_pos(win)
        sx, sy = x * self.px, y * self.px
        if action == glfw.PRESS:
            self.fly = None
            self.zoom_vel = 0.0
            now = time.perf_counter()
            last, self.last_press = self.last_press, None
            if (button == glfw.MOUSE_BUTTON_LEFT and last is not None and now - last[0] < DBL_CLICK_S
                    and abs(x - last[1]) < 4 and abs(y - last[2]) < 4 and not self.over_ui(sx, sy)):
                self.double_click(sx, sy)     # no drag, no click on the release
                self.drag = self.press = None
                return
            if button == glfw.MOUSE_BUTTON_LEFT:
                self.last_press = (now, x, y)
            self.tilting = bool(mods & glfw.MOD_ALT)
            self.drag = (x, y)
            self.press = (x, y)
        else:
            if self.press is not None:
                moved = abs(x - self.press[0]) >= 3 or abs(y - self.press[1]) >= 3
                if not moved and button == glfw.MOUSE_BUTTON_LEFT:
                    self.click(sx, sy)
            self.drag = None
            self.press = None

    def rect_at(self, sx, sy):
        """The file under the cursor, else the deepest directory there: its
        world rectangle, or None off the map."""
        f = self.hover[0] if self.hover is not None else None
        if f is None:
            wx, wy = self.screen_to_world(sx, sy)
            f = self.file_at(wx, wy)
        if f is not None:
            return tuple(float(v) for v in self.a["file_rect"][f])
        wx, wy = self.screen_to_world(sx, sy)
        r = self.a["dir_rect"]
        inside = (r[:, 0] <= wx) & (wx < r[:, 2]) & (r[:, 1] <= wy) & (wy < r[:, 3]) & (self.a["dir_depth"] >= 1)
        if not inside.any():
            return None
        d = int(np.flatnonzero(inside)[np.argmax(self.a["dir_depth"][inside])])
        return tuple(float(v) for v in r[d])

    def double_click(self, sx, sy):
        """Fly to the extents of the file or directory under the cursor; a
        second double click while still at those extents flies back to the
        view before."""
        rect = self.rect_at(sx, sy)
        if rect is None:
            return
        z = self.zoomed
        if z is not None and z["rect"] == rect:
            c1, z1, _, _ = self.fly_target(rect)
            at_extents = (abs(self.cx - c1[0]) < 1e-6 * self.W and abs(self.cy - c1[1]) < 1e-6 * self.H
                          and abs(self.zoom - z1) < 1e-6 * z1)
            if at_extents:
                bx, by, bz = z["back"]
                w, h = self.map_w / bz, self.map_h / bz
                self.zoomed = None
                self.fly_to((bx - w / 2, by - h / 2, bx + w / 2, by + h / 2), margin=0.0)
                return
        self.zoomed = {"rect": rect, "back": (self.cx, self.cy, self.zoom)}
        self.fly_to(rect)

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
            if self.tilting:              # Alt-drag: tilt and turn the 3D camera
                if self.proj != "3d":
                    self.set_proj("3d")
                    self.tilt = 0.0
                self.tilt = min(max(self.tilt + dy * 0.25 / self.px, 0.0), TILT_MAX)
                self.yaw = (self.yaw + dx * 0.25 / self.px) % 360.0
                self.drag = (x, y)
                return
            if self.proj == "3d":         # pan along the plane under the cursor
                self.camera_matrix()
                bx, by = self.screen_to_world(self.drag[0] * self.px, self.drag[1] * self.px)
                ax, ay = self.screen_to_world(x * self.px, y * self.px)
                self.cx -= ax - bx
                self.cy -= ay - by
                self.drag = (x, y)
                return
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
        elif key == glfw.KEY_C:
            self.set_tint(not self.tint_on)
        elif key == glfw.KEY_H:
            self.set_heights(not self.heights)
        elif key == glfw.KEY_R:
            self.fit()
        elif key == glfw.KEY_3:
            self.set_proj("2d" if self.proj == "3d" else "3d")
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
            key = np.lexsort((res["line"], res["file"]))     # by file, then line
            res = dict(res, order=key)
            self.results = res
            np.add.at(self.hit_count, res["file"], 1)
            self.hits_by_file = self.hit_count > 0
            self.build_panel_rows()
        self.update_file_flags()
        if fitted:                    # a fitted map stays fitted to the map viewport
            self.fit()

    def result(self, i):
        """(file, global line, col, len) of hit i in panel order."""
        r = self.results
        k = r["order"][i]
        return int(r["file"][k]), int(r["line"][k]), int(r["col"][k]), r["len"]

    def line_text(self, line):
        lo = self.a["line_off"]
        return self.a["chars"][lo[line]:lo[line + 1]].tobytes().decode(self.a["index"].get("encoding", "ascii"), "replace")

    def panel_chars(self):
        """characters per results row: 180 points wide, 11 point text"""
        return int((180 - 10) / (11 * self.ui_aspect))

    def build_panel_rows(self):
        """The panel: the hits by file, each with its line number and the
        line's text windowed so the hit shows. A row's third element is the
        hit's index, or -1."""
        r = self.results
        width = self.panel_chars()
        n = len(r["order"])
        rows = [(f"Hits ({n})", YELLOW, -1)]
        last_file = -1
        for i in range(n):
            f, line, col, ln = self.result(i)
            if f != last_file:
                rows.append((self.short_path(self.paths[f], width), UI_TEXT, -1))
                last_file = f
            j = line - int(self.a["file_line0"][f]) + 1
            raw = self.line_text(line)
            body = raw.lstrip()
            c = col - (len(raw) - len(body))     # hit column in the stripped text
            if c + ln > width - 6:               # window the text so the hit shows
                body = "…" + body[max(0, c - 8):]
            rows.append((f"{j:>5} {body}", UI_DIM, i))
        self.panel_rows = rows

    def short_path(self, path, width):
        return path if len(path) <= width else "…" + path[len(path) - width + 1:]

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
        f, line, col, ln = self.result(i)
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
        destination of a fly-to is at the text LOD."""
        p = float(self.a["file_pitch"][f])
        row, rc = self.row_of(line, col)
        x, y = (float(v) for v in self.a["row_pos"][int(row[0])])
        xc, yc = x + float(self.col_offset(row, rc)[0]) * p, y + p / 2
        hw, hh = cols / 2 * p * self.A, lines / 2 * p
        # keep the centre on the file, so the file under the view centre is
        # this one (the zoom reference and the 3D focus height depend on it)
        x0, y0, x1, y1 = (float(v) for v in self.a["file_rect"][f])
        xc = min(max(xc, x0 + 1e-3), max(x1 - 1e-3, x0 + 1e-3))
        yc = min(max(yc, y0 + 1e-3), max(y1 - 1e-3, y0 + 1e-3))
        return (xc - hw, yc - hh, xc + hw, yc + hh)

    def goto(self, spec, complete=True):
        """--goto path[:line]; the path may hold colons (a book's `Book One: 1805/...`)."""
        path, sep, line = spec.rpartition(":")
        if not sep or not line.isdigit():
            path, line = spec, ""
        matches = [i for i, p in enumerate(self.paths)
                   if p == path or p.endswith("/" + path)]
        if not matches:
            raise SystemExit(f"error: --goto: no file matches '{path}'")
        f = min(matches, key=lambda i: len(self.paths[i]))
        l0, l1 = (int(v) for v in self.a["file_line0"][f:f + 2])
        if line:
            j = min(max(int(line) - 1, 0), max(l1 - l0 - 1, 0))
            if l1 > l0:
                # centre a little way into the line, not on its first column
                col = min(30, int(self.a["line_len"][l0 + j]) // 2)
                self.fly_to(self.line_rect(f, l0 + j, col), complete=complete)
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
        if not self.in_map(sx, sy):
            return None
        wx, wy = self.screen_to_world(sx, sy)
        f = self.file_at(wx, wy)
        if self.proj == "3d":             # re-pick on that file's top, then its neighbour's
            for _ in range(2):
                z = float(self.file_top[f]) if f is not None else 0.0
                wx, wy = self.screen_to_world(sx, sy, z)
                f2 = self.file_at(wx, wy)
                if f2 == f:
                    break
                f = f2
        if f is None:
            return None
        a = self.a
        x0, y0 = a.get("file_text", a["file_rect"])[f, :2]     # the text block: a book page's inner rectangle
        p, k, rows, colw = (float(a["file_pitch"][f]), int(a["file_cols"][f]),
                            int(a["file_rows"][f]), float(a["file_colw"][f]))
        n = int(a["file_row0"][f + 1] - a["file_row0"][f])
        c = min(max(int((wx - x0) // colw), 0), max(k - 1, 0))
        r = int(math.floor((wy - y0 - p) / p))
        j = c * rows + r if 0 <= r < rows else -1
        if j >= n:
            j = -1
        col = int((wx - x0 - c * colw) // (p * self.A))
        if j >= 0:                      # visual row -> logical line and column
            row = int(a["file_row0"][f]) + j
            line = int(a["row_line"][row])
            col0 = int(a["row_col0"][row])
            xr = (wx - float(a["row_pos"][row, 0])) / p      # from the row's left, in line heights
            if self.prop:
                start = int(a["line_off"][line]) + col0
                xs = a["char_x"][start:start + int(a["row_len"][row])]
                col = col0 + max(int(np.searchsorted(xs, xr, side="right")) - 1, 0)
            else:
                col = col0 + max(int(xr // self.A), 0)
            j = line - int(a["file_line0"][f])
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
        if self.lod[f] >= 2 and j >= 0:
            text += f":{j + 1}"
            it = self.enclosing_item(f, j)
            if it is not None:
                text += f" {self.item_keyword(it, f)} {self.item_names[it]}"
        if self.lod[f] == 3 and self.results is not None:
            text += f" · {int(self.hit_count[f])} hits"
        return text

    def build_dir_tags(self):
        """The tag each directory draws, or None. A directory whose inner
        rectangle is filled by exactly one child directory (at least 85
        percent of it) draws no tag; the innermost directory of such a
        chain draws one tag joining the chain's names ('platform · src ·
        os'). The crumb trail still uses the plain labels."""
        a = self.a
        r, pad = a["dir_rect"], a["dir_pad"]
        area = (r[:, 2] - r[:, 0]) * (r[:, 3] - r[:, 1])
        inner = (np.maximum(r[:, 2] - r[:, 0] - 2 * pad, 0)
                 * np.maximum(r[:, 3] - r[:, 1] - 2 * pad, 0))
        self.dir_tags = [None] * self.n_dirs
        chain = [""] * self.n_dirs         # the names a hidden ancestor passes down
        for d in np.argsort(a["dir_depth"], kind="stable"):
            if d == 0:
                continue
            name = self.dir_labels[d].rstrip("/")
            prefix = chain[self.dir_parent[d]]
            kids = self.dir_children[d]
            hidden = len(kids) == 1 and area[kids[0]] >= 0.85 * inner[d]
            if hidden:
                chain[d] = f"{prefix} · {name}" if prefix else name
            else:
                self.dir_tags[d] = f"{prefix} · {name}" if prefix else self.dir_labels[d]

    def update_crumb(self):
        """Where the view centre is: the corpus, then the path of the file
        under the centre, or of the deepest directory there."""
        a = self.a
        f = self.file_at(self.cx, self.cy)
        if f is not None:
            parts = self.paths[f].split("/")
        else:
            r = a["dir_rect"]
            inside = (r[:, 0] <= self.cx) & (self.cx < r[:, 2]) & (r[:, 1] <= self.cy) & (self.cy < r[:, 3])
            parts = []
            if inside.any():
                d = int(np.flatnonzero(inside)[np.argmax(a["dir_depth"][inside])])
                while d > 0:
                    parts.append(self.dir_labels[d].rstrip("/"))
                    d = int(self.dir_parent[d])
                parts.reverse()
        self.crumb = "at " + " › ".join([self.name] + parts)

    # ---- frame ----------------------------------------------------------
    def project_rects(self, rects, z):
        """(visible, scale) per rectangle at height z through the 3D camera:
        visible when its projected corners overlap the viewport (a corner
        behind the camera counts as visible), scale = focus_w / w at the
        centre, the foreshortening of its pixel size."""
        n = len(rects)
        c = np.empty((n, 4, 4))
        c[:, 0, :2] = rects[:, [0, 1]]; c[:, 1, :2] = rects[:, [2, 1]]
        c[:, 2, :2] = rects[:, [0, 3]]; c[:, 3, :2] = rects[:, [2, 3]]
        c[:, :, 2] = z[:, None]
        c[:, :, 3] = 1.0
        clip = c @ self.M.T
        w = clip[:, :, 3]
        front = w > 1e-6
        ws = np.where(front, w, 1e-6)
        nx, ny = clip[:, :, 0] / ws, clip[:, :, 1] / ws
        inside = ((np.where(front, nx, -np.inf).max(axis=1) > -1) & (np.where(front, nx, np.inf).min(axis=1) < 1)
                  & (np.where(front, ny, -np.inf).max(axis=1) > -1) & (np.where(front, ny, np.inf).min(axis=1) < 1))
        visible = np.where(front.all(axis=1), inside, front.any(axis=1))
        wc = np.maximum(w.mean(axis=1), 1e-6)
        return visible, self.focus_w / wc

    def screen_box(self, rect, z=0.0):
        """The device-pixel bounding box of a world rectangle at height z, or
        None when a corner is behind the 3D camera."""
        x0, y0, x1, y1 = (float(v) for v in rect)
        if self.proj != "3d":
            pts = [self.world_to_screen(x0, y0), self.world_to_screen(x1, y1)]
        else:
            pts = []
            for wx, wy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
                c = self.M @ np.array([wx, wy, z, 1.0])
                if c[3] <= 1e-6:
                    return None
                pts.append(((c[0] / c[3] + 1.0) * 0.5 * self.map_w, (1.0 - c[1] / c[3]) * 0.5 * self.map_h + self.map_y0))
        return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))

    def focus_target(self):
        """The height the 3D camera should orbit: the top of the file under
        the view centre once that file is at least two pixels per line, the
        ground when zoomed out, blended between."""
        f = self.file_at(self.cx, self.cy)
        if f is None:                     # over a directory band: hold the height
            return self.z_focus
        w = min(max(float(self.a["file_pitch"][f]) * self.zoom / 2.0, 0.0), 1.0)
        return float(self.file_top[f]) * w

    def settle_focus(self):
        self.z_focus = self.focus_target() if self.proj == "3d" else 0.0

    def update_per_file(self):
        a = self.a
        if self.proj == "3d":
            self.z_focus += (self.focus_target() - self.z_focus) * (1.0 if self.scripted else 0.2)
        else:
            self.z_focus = 0.0
        self.camera_matrix()
        x0, y0, x1, y1 = self.view()
        r = a["file_rect"]
        if self.proj == "3d":
            self.visible, self.file_scale = self.project_rects(r, self.file_top)
            _, self.dir_scale = self.project_rects(a["dir_rect"], self.dir_z0 + self.dir_h)
        else:
            self.visible = (r[:, 2] > x0) & (r[:, 0] < x1) & (r[:, 3] > y0) & (r[:, 1] < y1)
            self.file_scale = np.ones(self.n_files)
            self.dir_scale = np.ones(self.n_dirs)
        ppl = a["file_pitch"] * self.zoom * self.file_scale
        self.ppl = ppl
        self.lod = ((ppl >= 1).astype(np.uint8) + (ppl >= 3) + (ppl >= 6)).astype(np.uint8)
        flags = np.zeros(self.n_files, np.uint8)
        cur = self.cursor_override if self.scripted else self.cursor
        self.hover = (self.hover_at(*cur) if cur is not None and self.drag is None
                      and not self.over_ui(*cur) else None)
        if self.hover is not None:
            flags[self.hover[0]] |= 1
        if self.results is not None and len(self.filter_text) >= 2:
            flags[self.hits_by_file] |= 2
            if self.current_file >= 0:
                flags[self.current_file] |= 4
            flags[self.dimmed] |= 8
        # LOD 0 draws every step-th line so the density stays about one
        # bar per pixel row; the step goes to the shader as two bytes
        self.file_step = np.ones(self.n_files, np.int64)
        sub = ppl < 1
        self.file_step[sub] = np.clip(np.ceil(1.0 / np.maximum(ppl[sub], 1e-9)), 1, 65535)
        fu = self.file_u.reshape(-1, 4)
        fu[:self.n_files, 0] = self.lod
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
        l0 = self.a["file_row0"]
        glUseProgram(self.prog["line"])
        loc = self.uniform("line", "uBase")
        glBindVertexArray(self.quad_vao)
        for f0, f1 in zip(edges[::2], edges[1::2]):
            base, end = int(l0[f0]), int(l0[f1])
            if end > base:
                glUniform1i(loc, base)
                glDrawArraysInstanced(GL_TRIANGLES, 0, 6, end - base)
        self.line_draws = len(edges) // 2

    def draw_visible_glyphs(self):
        """A proportional face: one instanced draw per run of consecutive
        visible files at the tokens LOD or above, one instance per
        character (glyph.glsl)."""
        vis = self.visible & (self.lod >= 2)
        if not vis.any():
            return
        edges = np.flatnonzero(np.diff(np.concatenate(([False], vis, [False]))))
        c0 = self.a["line_off"][self.a["file_line0"]].astype(np.int64)     # first character of each file
        glUseProgram(self.prog["glyph"])
        loc = self.uniform("glyph", "uBase")
        glBindVertexArray(self.quad_vao)
        for f0, f1 in zip(edges[::2], edges[1::2]):
            base, end = int(c0[f0]), int(c0[f1])
            if end > base:
                glUniform1i(loc, base)
                glDrawArraysInstanced(GL_TRIANGLES, 0, 6, end - base)

    def draw_instanced(self, prog, n):
        glUseProgram(self.prog[prog])
        glBindVertexArray(self.quad_vao)
        glDrawArraysInstanced(GL_TRIANGLES, 0, 6, n)

    def draw_rects(self, inst, world=True):
        """inst: float32 (n, 10): x0 y0 x1 y1 r g b a border_px fill_alpha."""
        if len(inst) == 0:
            return
        if inst.shape[1] == 10:           # UI boxes: no height
            inst = np.concatenate((inst, np.zeros((len(inst), 1), np.float32)), axis=1)
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
        inst[:, 4] = [atlas_font.glyph_cell(c, self.ui_gm) for c in text]
        inst[:, 5:8] = color
        inst[:, 8] = alpha
        return inst, n * cw

    def band_instances(self):
        """The kind bands: the item rectangles of visible files are filled
        bands in the item kind colour, under the sampled bars and the line
        bars alike so nothing flips at the 1 px cut, at 18 percent up to 3 px
        per line and fading to nothing by 6, where the outlines, drawn from
        3, have taken over (fills far, outlines near, and no cut between)."""
        a = self.a
        m = (self.visible & (self.lod <= 2))[self.item_rect_file]
        if not m.any():
            return np.zeros((0, 10), np.float32)
        rects = a["item_rect"][m]
        kinds = a["item_kind"][a["item_rect_item"][m]]
        inst = np.zeros((len(rects), 11), np.float32)
        inst[:, :4] = rects
        for k, c in self.item_colors.items():
            inst[kinds == k, 4:7] = c
        inst[(kinds < 1) | (kinds > 5), 4:7] = self.item_colors[5]
        inst[:, 7] = 1.0
        inst[:, 8] = 0.0
        inst[:, 9] = 0.18 * np.clip((6.0 - self.ppl[self.item_rect_file[m]]) / 3.0, 0.0, 1.0)
        inst[:, 10] = self.file_top[self.item_rect_file[m]] + 0.02
        return inst

    def item_instances(self):
        a = self.a
        m = (self.visible & (self.lod >= 2))[self.item_rect_file]
        if not m.any():
            return np.zeros((0, 10), np.float32)
        rects = a["item_rect"][m]
        kinds = a["item_kind"][a["item_rect_item"][m]]
        inst = np.zeros((len(rects), 11), np.float32)
        inst[:, :4] = rects
        for k, c in self.item_colors.items():
            inst[kinds == k, 4:7] = c
        inst[(kinds < 1) | (kinds > 5), 4:7] = self.item_colors[5]
        inst[:, 7] = 0.6
        inst[:, 8] = 1.5
        inst[:, 10] = self.file_top[self.item_rect_file[m]] + 0.06
        return inst

    def hit_instances(self):
        r = self.results
        if r is None or len(r["order"]) == 0:
            return np.zeros((0, 10), np.float32)
        a = self.a
        f = r["file"]
        m = self.visible[f] & (self.lod[f] >= 2)
        if not m.any():
            return np.zeros((0, 10), np.float32)
        idx = np.flatnonzero(m)
        ff = f[idx]
        p = a["file_pitch"][ff]
        row, rc = self.row_of(r["line"][idx], r["col"][idx])
        pos = a["row_pos"][row].astype(np.float64)
        rlen = a["row_len"][row].astype(np.int64)
        c0 = self.col_offset(row, np.minimum(rc, rlen)) * p
        c1 = self.col_offset(row, np.minimum(rc + r["len"], rlen)) * p
        inst = np.zeros((len(idx), 11), np.float32)
        inst[:, 0] = pos[:, 0] + c0
        inst[:, 1] = pos[:, 1]
        inst[:, 2] = pos[:, 0] + c1
        inst[:, 3] = pos[:, 1] + p
        inst[:, 4:7] = YELLOW
        inst[:, 7] = 1.0
        inst[:, 8] = 2.0
        inst[:, 10] = self.file_top[ff] + 0.06
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
        cw = size * self.ui_aspect
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

        # directory labels: a tag at the top-left of the visible part of each
        # directory's inner rectangle, so a directory zoomed into keeps its
        # tag at the corner of the view
        r = a["dir_rect"]
        side = np.minimum(r[:, 2] - r[:, 0], r[:, 3] - r[:, 1]) * self.zoom * self.dir_scale
        x0, y0, x1, y1 = self.view()
        vis = ((r[:, 2] > x0) & (r[:, 0] < x1) & (r[:, 3] > y0) & (r[:, 1] < y1)
               & (a["dir_depth"] >= 1) & (side >= 48))
        zs = self.z_scale()
        dtop = (self.dir_z0 + self.dir_h) * zs
        placed = []
        bh = size + 2 * pad * 0.6
        vx0, vy0 = 2 * s, self.map_y0 + 2 * s
        vx1, vy1 = self.map_w - 2 * s, self.map_y0 + self.map_h - 2 * s
        for d in sorted(np.flatnonzero(vis), key=lambda d: a["dir_depth"][d]):
            text = self.dir_tags[d]
            if text is None:               # hidden by a label chain
                continue
            bw = len(text) * cw + 2 * pad
            if bw > 0.4 * (r[d, 2] - r[d, 0]) * self.zoom * self.dir_scale[d]:
                continue
            sb = self.screen_box(r[d] + a["dir_pad"][d] * np.array([1, 1, -1, -1]), dtop[d])
            if sb is None:
                continue
            bx0, by0, bx1, by1 = max(sb[0], vx0), max(sb[1], vy0), min(sb[2], vx1), min(sb[3], vy1)
            if bx1 - bx0 < bw or by1 - by0 < bh:
                continue
            # a child's corner is inset by its padding only, so its tag would
            # sit on the parent's: slide it right along the top edge, past
            # the tag in the way (the tags then read as a trail, each on its
            # own directory's edge); down only if the right edge runs out
            sx, sy = bx0, by0
            for _ in range(8):
                hit = next((b for b in placed
                            if sx < b[2] and sx + bw > b[0] and sy < b[3] and sy + bh > b[1]), None)
                if hit is None:
                    break
                sx = hit[2] + 2 * s
                if sx + bw > bx1:
                    sx, sy = bx0, sy + bh + 2 * s
            else:
                continue                   # still on another tag after eight slides: no tag
            if sy + bh > by1:
                continue
            placed.append((sx, sy, sx + bw, sy + bh))
            box(sx, sy, sx + bw, sy + bh, UI_BOX, 0.85)
            label(sx + pad, sy + pad * 0.6, text)
        glEnable(GL_SCISSOR_TEST)          # labels stay inside the map viewport
        glScissor(0, int(self.fb_h - self.map_y0 - self.map_h), int(self.map_w), int(self.map_h))
        flush()                            # and go under the panels
        glDisable(GL_SCISSOR_TEST)

        # the bottom strip: the crumb trail left and, while the panel is
        # closed, the status right-aligned on the same row (the tint's label
        # in yellow before it), each cut to the room left
        row_y = self.fb_h - m - size - 2 * pad * 0.6
        bw = len(self.crumb) * cw + 2 * pad
        box(m, row_y, m + bw, self.fb_h - m, UI_BOX, 0.85)
        label(m + pad, row_y + pad * 0.6, self.crumb)
        status, extra = self.status_texts()
        if not self.panel_rows:
            x1 = self.map_w - m
            free = x1 - (m + bw + m)
            for text, color in ((extra, YELLOW), (status, UI_TEXT)):
                if not text or free < 6 * cw:
                    continue
                n_chars = min(len(text), int((free - 2 * pad) / cw))
                tw = n_chars * cw + 2 * pad
                box(x1 - tw, row_y, x1, self.fb_h - m, UI_BOX, 0.85)
                label(x1 - tw + pad, row_y + pad * 0.6, text, color=color, max_chars=n_chars)
                x1 -= tw + 2 * s
                free -= tw + 2 * s

        # the right column: status line, filter box, results panel. With the
        # panel open the column is its own strip beside the map and the
        # status wraps to the panel's width; otherwise the filter box sits
        # alone in the top right corner.
        pw = PANEL_PT * s
        cx0, cx1 = self.fb_w - m - pw, self.fb_w - m
        if self.panel_rows:               # the column: status (wrapped), filter box, panel
            parts = wrap_parts(status + (" · " + extra if extra else ""), self.panel_chars())
            row_h = small + 2 * s
            sy1 = m + len(parts) * row_h + 2 * pad * 0.6
            box(cx0, m, cx1, sy1, UI_BOX, 0.85)
            for k, part in enumerate(parts):
                label(cx0 + pad, m + pad * 0.6 + k * row_h, part, small)
            fy0 = sy1 + m
        else:
            fy0 = m
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

        # the top strip: the input hints, dim, top left, cut to the room
        # before the filter box; only the keys this corpus answers to
        hints = ["wheel zoom", "drag pan", "double-click fly", "/ filter", "Enter next hit", "3 tilt", "Alt-drag turn"]
        if not a["layout"].get("flat", False):
            hints.append("H heights")
        if self.tint is not None:
            hints.append(f"C {self.tint_label or 'tint'}")
        hints += ["R reset", "Q quit"]
        text = " · ".join(hints)
        free = cx0 - m - m
        n_chars = min(len(text), int((free - 2 * pad) / cw))
        if n_chars >= 6:
            tw = n_chars * cw + 2 * pad
            box(m, m, m + tw, m + size + 2 * pad * 0.6, UI_BOX, 0.85)
            label(m + pad, m + pad * 0.6, text, color=UI_DIM, max_chars=n_chars)

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
        glClearColor(self.bg[0], self.bg[1], self.bg[2], 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        self.update_per_file()
        # the map in its own viewport, the window minus the panel column
        glViewport(0, int(self.fb_h - self.map_y0 - self.map_h), int(self.map_w), int(self.map_h))
        for prog in ("dir", "file", "line", "wall", "glyph"):
            self.set_camera_uniforms(prog)
        if self.proj == "3d":
            glEnable(GL_DEPTH_TEST)
            glDepthFunc(GL_LEQUAL)
            # the terrace tops sit at the same depth as the files on them (at
            # every depth with the heights off): push them back a little so
            # the files never fight them
            glEnable(GL_POLYGON_OFFSET_FILL)
            glPolygonOffset(1.0, 2.0)
        self.draw_instanced("dir", self.n_dirs)
        glDisable(GL_POLYGON_OFFSET_FILL)
        if self.proj == "3d":
            glUseProgram(self.prog["wall"])
            glUniform1i(self.uniform("wall", "uNFiles"), self.n_files)
            self.draw_instanced("wall", 4 * (self.n_files + self.n_dirs))
        glUseProgram(self.prog["file"])
        glUniform1i(self.uniform("file", "uRingOnly"), 0)
        glUniform1i(self.uniform("file", "uTint"), 1 if self.tint_on else 0)
        self.draw_instanced("file", self.n_files)
        self.draw_rects(self.band_instances())      # kind bands under the bars
        self.draw_visible_lines()
        if self.prop:
            self.draw_visible_glyphs()
        self.draw_rects(self.item_instances())
        self.draw_rects(self.hit_instances())
        glUseProgram(self.prog["file"])          # borders over the text
        glUniform1i(self.uniform("file", "uRingOnly"), 1)
        self.draw_instanced("file", self.n_files)
        glDisable(GL_DEPTH_TEST)
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
            self.settle_focus()
            held = True
        if args.zoom is not None:
            self.set_zoom_ppl(args.zoom)
            held = True
        if args.filter:
            self.set_filter(args.filter, sync=True)
            held = True
        if args.step is not None:
            if self.results is None or len(self.results["order"]) == 0:
                raise SystemExit("error: --step needs hits; give --filter WORD with hits")
            self.goto_result((args.step - 1) % len(self.results["order"]), complete=True)
            held = True
        if getattr(args, "hover", None):
            path, line, col = args.hover.rsplit(":", 2)
            f = next(i for i, pth in enumerate(self.paths) if pth == path or pth.endswith("/" + path))
            gl = int(self.a["file_line0"][f]) + int(line) - 1
            self.fly_to(self.line_rect(f, gl, int(col) - 1, lines=30, cols=90), complete=True)
            row, rc = self.row_of(gl, int(col) - 1)
            x, y = (float(v) for v in self.a["row_pos"][int(row[0])])
            p = float(self.a["file_pitch"][f])
            xa, xb = float(self.col_offset(row, rc)[0]), float(self.col_offset(row, rc + 1)[0])
            self.settle_focus()
            self.camera_matrix()
            self.cursor_override = self.world_to_screen(x + (xa + xb) / 2 * p, y + p / 2,
                                                        float(self.file_top[f]) * self.z_scale())
            held = True
        return held

    def run_scripted(self):
        """--frames N: N frames of the opening zoom (fit to 16 px per line
        for the file at the corpus centre, through all four LODs) unless a
        flag pins the view, then --screenshot / --stats."""
        n = self.args.frames if self.args.frames is not None else 240
        held = self.apply_script_flags()
        centre = (self.W / 2, self.H / 2)
        if not held:
            self.fit()                    # the 3D fit backs off for perspective
        z0, z1 = self.zoom, 16.0 / self.ref_pitch(centre)
        last = time.perf_counter()
        for k in range(n):
            if not held:
                u = k / max(n - 1, 1)
                self.fly, self.zoom_vel = None, 0.0
                self.cx, self.cy = centre
                self.zoom = z0 * (z1 / z0) ** u
                self.z_focus = self.focus_target() if self.proj == "3d" else 0.0
                if not getattr(self.args, "no_hover", False):
                    self.cursor_override = (self.map_w / 2, self.map_y0 + self.map_h / 2)
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
        """--shots DIR: the standard set. The fitted map, the LODs at 2,
        4.5 and 16 px per line, the hover label, the filter and its first
        hit, the 3D projection with the heights on, fitted and close, and
        the tint when the corpus has one."""
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
        # the cursor on the file nearest the centre (the centre itself may be a gutter)
        r = self.a["file_rect"]
        fc = np.stack([(r[:, 0] + r[:, 2]) / 2, (r[:, 1] + r[:, 3]) / 2], axis=1)
        f = int(np.argmin(np.hypot(fc[:, 0] - self.cx, fc[:, 1] - self.cy)))
        self.camera_matrix()
        self.cursor_override = self.world_to_screen(fc[f, 0], fc[f, 1])
        snap("hover.png")
        self.cursor_override = None
        self.fit()
        self.set_filter(word, sync=True)
        snap("filter.png")
        if self.results is not None and len(self.results["order"]):
            self.goto_result((self.args.step or 1) - 1, complete=True)
        snap("result.png")
        self.set_filter("")
        # the 3D projection with the heights on: the whole map tilted, then a close pass
        heights = self.heights
        self.set_heights(True)
        self.tilt, self.yaw = 55.0, 12.0
        self.set_proj("3d")
        self.fit()
        snap("3d.png")
        self.set_zoom_ppl(3.0, centre)
        snap("3d_zoom.png")
        self.set_proj("2d")
        self.set_heights(heights)
        if self.tint is not None:
            self.set_tint(True)
            self.fit()
            snap(f"{self.tint_label or 'tint'}.png")
            self.set_tint(False)
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
                    self.win, f"big-text: {self.name} | {fps:5.1f} fps | "
                              f"{ppl:.1f} px/line at centre")
                fps_t, fps_n = now, 0
            if len(self.frame_times) > 10000:
                del self.frame_times[:5000]

    def run(self):
        print(f"{self.name}: {self.n_files} files, {self.n_lines} lines, {self.n_chars} chars, "
              f"{self.n_dirs} dirs; loaded in {self.load_seconds * 1000:.0f} ms, "
              f"GPU {self.gpu_bytes / 1e6:.1f} MB in {self.gpu_seconds * 1000:.0f} ms; "
              f"framebuffer {self.fb_w}x{self.fb_h} ({self.px:g}x)"
              + (f"; vector text from {VT_MIN_PPL:g} px per line" if self.vt is not None else "")
              + (f"; tint: {self.tint_label}" if self.tint is not None else ""))
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
    ap.add_argument("--vector-text", dest="vector_text", action="store_true", default=True,
                    help=f"draw glyphs with the vector tier (vt_glyphs.py) from {VT_MIN_PPL:g} device px per line (the default)")
    ap.add_argument("--no-vector-text", dest="vector_text", action="store_false",
                    help="draw glyphs from the raster atlas at every size")
    ap.add_argument("--frames", type=int, default=None,
                    help="run N scripted frames and exit (self-test)")
    ap.add_argument("--screenshot", default=None, help="save the final frame to this PNG")
    ap.add_argument("--goto", default=None, metavar="PATH[:LINE]",
                    help="start with the fly-to this file (and line) complete")
    ap.add_argument("--zoom", type=float, default=None, metavar="PX_PER_LINE",
                    help="zoom so the file under the view centre has this many device px per line")
    ap.add_argument("--filter", default=None, metavar="WORD", help="apply a filter")
    ap.add_argument("--step", type=int, default=None, metavar="K", help="step to hit K (1-based)")
    ap.add_argument("--proj", default=None, choices=["2d", "3d"],
                    help="start in 2D (default) or the tilted 3D projection; 3 toggles, Alt-drag tilts")
    ap.add_argument("--tilt", type=float, default=None, help="3D tilt in degrees (default 55)")
    ap.add_argument("--heights", action="store_true",
                    help="start with the 3D heights on: files rise by their weight, directories are terraces; H toggles them")
    ap.add_argument("--yaw", type=float, default=None, help="3D turn in degrees (default 0)")
    ap.add_argument("--tint", action="store_true",
                    help="start with the tint on, when the index has one; C toggles it")
    ap.add_argument("--no-hover", action="store_true",
                    help="scripted frames: no hover highlight or label on the file under the centre")
    ap.add_argument("--hover", default=None, metavar="PATH:LINE:COL",
                    help="fly there and hold the cursor on that cell (for screenshots)")
    ap.add_argument("--shots", default=None, metavar="DIR",
                    help="write the standard screenshot set to DIR and exit")
    ap.add_argument("--stats", action="store_true",
                    help="print load time, GPU memory and frame times after the scripted frames")
    args = ap.parse_args()
    check_atlas(args.atlas)
    Viewer(args).run()


if __name__ == "__main__":
    main()
