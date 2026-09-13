#!/usr/bin/env python3
"""Stage 2: lay out an index as a squarified treemap with wrapped columns.

Reads data/<name>_atlas/index.npz + index.json and writes layout.npz +
layout.json (see docs/DESIGN.md for the arrays). The root is [0, 1600] by
[0, 1600 / aspect] in world units, y down. Every directory is inset by its
padding (the band the viewer draws in the directory's hue) and its files and
subdirectories are squarified inside, biggest first. A file is split into k
equal columns so that its text is as large as possible while a column still
holds the file's typical line length; lines longer than a column are clipped.

  ./atlas_layout.py data/big-picture_atlas --preview docs/shots/big-picture_layout.png
"""
import argparse
from pathlib import Path
import json
import math
import os
import time

import numpy as np

WORLD_W = 1600.0
PAD_FRAC, PAD_MIN, PAD_MAX = 0.015, 0.05, 4.0
GAP = 0.06                 # column gap as a fraction of the column width
MAX_COLS = 64
TARGET_MIN, TARGET_MAX = 24, 100
CAP_MAX = 4096             # lines are capped at 4096 columns by atlas_index.py
N_HUES = 12


# ---------------------------------------------------------------- treemap

def worst_ratio(side, s, mn, mx):
    """Worst aspect ratio of a row with area sum s, min mn and max mx laid
    along a side of length side (Bruls, Huizing, van Wijk 2000)."""
    return max(side * side * mx / (s * s), s * s / (side * side * mn))


def squarify(areas, x0, y0, x1, y1):
    """Rectangles (n, 4) for areas sorted descending, filling the box."""
    n = len(areas)
    rects = np.zeros((n, 4))
    if n == 0:
        return rects
    w, h = x1 - x0, y1 - y0
    total = float(sum(areas))
    if w <= 0 or h <= 0 or total <= 0:
        rects[:] = (x0, y0, x0, y0)
        return rects
    areas = [a / total * w * h for a in areas]
    x, y, i = x0, y0, 0
    while i < n:
        side = min(w, h)
        s, mn, mx, j = areas[i], areas[i], areas[i], i + 1
        worst = worst_ratio(side, s, mn, mx)
        while j < n:
            s2, mn2, mx2 = s + areas[j], min(mn, areas[j]), max(mx, areas[j])
            w2 = worst_ratio(side, s2, mn2, mx2)
            if w2 > worst:
                break
            s, mn, mx, worst, j = s2, mn2, mx2, w2, j + 1
        last = j == n
        if w >= h:                          # a vertical strip on the left
            rw = w if last else s / h
            yy = y
            for k in range(i, j):
                rh = areas[k] / rw
                rects[k] = (x, yy, x + rw, yy + rh)
                yy += rh
            rects[j - 1, 3] = y + h
            x += rw
            w -= rw
        else:                               # a horizontal strip on top
            rh = h if last else s / w
            xx = x
            for k in range(i, j):
                rw = areas[k] / rh
                rects[k] = (xx, y, xx + rw, y + rh)
                xx += rw
            rects[j - 1, 2] = x + w
            y += rh
            h -= rh
        i = j
    return rects


def dir_padding(w, h):
    pad = min(max(PAD_FRAC * min(w, h), PAD_MIN), PAD_MAX)
    return min(pad, 0.25 * min(w, h))      # keep the inner rectangle real for slivers


def layout_tree(dirs, file_weight, file_dir, world):
    """Directory and file rectangles by recursive squarification."""
    n_dirs, n_files = len(dirs), len(file_weight)
    dir_weight = np.zeros(n_dirs)
    for d in range(n_dirs - 1, -1, -1):      # pre-order: children come after parents
        dir_weight[d] = sum(file_weight[f] for f in dirs[d]["files"]) \
            + sum(dir_weight[c] for c in dirs[d]["children"])
    dir_rect = np.zeros((n_dirs, 4))
    dir_pad = np.zeros(n_dirs)
    file_rect = np.zeros((n_files, 4))

    def place(d, rect):
        x0, y0, x1, y1 = rect
        dir_rect[d] = rect
        pad = dir_padding(x1 - x0, y1 - y0)
        dir_pad[d] = pad
        kids = [(file_weight[f], 0, f) for f in dirs[d]["files"]] \
            + [(dir_weight[c], 1, c) for c in dirs[d]["children"]]
        if dirs[d].get("rows"):           # the Layers lens: children stacked top to bottom
            # row heights by weight^0.6, so a giant cycle does not squeeze
            # the thin layers above and below it into slivers
            shares = [max(k[0], 1e-9) ** 0.6 for k in kids]
            total = sum(shares) or 1.0
            h = (y1 - y0 - 2 * pad)
            rects, y = [], y0 + pad
            for share in shares:
                dy = h * share / total
                rects.append((x0 + pad, y, x1 - pad, y + dy))
                y += dy
        else:
            kids.sort(key=lambda k: -k[0])
            rects = squarify([k[0] for k in kids], x0 + pad, y0 + pad, x1 - pad, y1 - pad)
        for (wt, is_dir, idx), r in zip(kids, rects):
            if is_dir:
                place(idx, tuple(r))
            else:
                file_rect[idx] = r

    place(0, (0.0, 0.0, world[0], world[1]))
    return dir_rect, dir_pad, file_rect


# ---------------------------------------------------------------- book

def book_layout(dirs, world, page_aspect):
    """Rectangles for a book (index.json "corpus": "book", parts over
    pages): every part a full-width band of whole page rows in reading
    order, every page one cell of one size, rows filled left to right, the
    last row of a part short. The column count is the one that brings a
    cell closest to page_aspect (width over height) from the wide side, so
    a page's single column holds its lines."""
    n_dirs = len(dirs)
    parts = dirs[0]["children"]
    counts = [len(dirs[p]["files"]) for p in parts]
    n_files = sum(len(d["files"]) for d in dirs)
    dir_rect, dir_pad, file_rect = np.zeros((n_dirs, 4)), np.zeros(n_dirs), np.zeros((n_files, 4))
    dir_rect[0] = (0.0, 0.0, world[0], world[1])
    pad = dir_padding(world[0], world[1])
    dir_pad[0] = pad
    x0, y0, x1, y1 = pad, pad, world[0] - pad, world[1] - pad
    best = None
    for cols in range(1, max(counts, default=1) + 1):
        rows = sum(-(-c // cols) for c in counts)
        ratio = ((x1 - x0) / cols) / ((y1 - y0) / rows) / page_aspect
        key = (ratio < 1, abs(math.log(ratio)))
        if best is None or key < best[0]:
            best = (key, cols, rows)
    _, cols, rows = best
    row_h = (y1 - y0) / rows
    y = y0
    for p, count in zip(parts, counts):
        r = -(-count // cols)
        dir_rect[p] = (x0, y, x1, y + r * row_h)
        pp = dir_padding(x1 - x0, r * row_h)
        dir_pad[p] = pp
        cw, ch = (x1 - x0 - 2 * pp) / cols, (r * row_h - 2 * pp) / r
        for k, f in enumerate(dirs[p]["files"]):
            i, j = divmod(k, cols)
            file_rect[f] = (x0 + pp + j * cw, y + pp + i * ch, x0 + pp + (j + 1) * cw, y + pp + (i + 1) * ch)
        y += r * row_h
    return dir_rect, dir_pad, file_rect, cols


# ---------------------------------------------------------------- layers

def file_graph(resolve_path, n_files):
    """Directed edges between files: A -> B when a reference in A resolves
    to an entity defined in B. Returns (src, dst, count)."""
    r = np.load(resolve_path)
    ok = r["ref_ent"] >= 0
    src = r["ref_file"][ok].astype(np.int64)
    dst = r["ent_file"][r["ref_ent"][ok]].astype(np.int64)
    m = src != dst
    key = src[m] * n_files + dst[m]
    u, cnt = np.unique(key, return_counts=True)
    return u // n_files, u % n_files, cnt


def strongly_connected(n, src, dst):
    """Tarjan, iterative. Component ids come out in reverse topological
    order: every successor of a component has a smaller id."""
    adj = [[] for _ in range(n)]
    for a, b in zip(src.tolist(), dst.tolist()):
        adj[a].append(b)
    index, low, on, comp = [-1] * n, [0] * n, [False] * n, [-1] * n
    stack, counter, ncomp = [], 0, 0
    for root in range(n):
        if index[root] != -1:
            continue
        work = [(root, 0)]
        while work:
            v, i = work[-1]
            if i == 0:
                index[v] = low[v] = counter
                counter += 1
                stack.append(v)
                on[v] = True
            recurse = False
            while i < len(adj[v]):
                w = adj[v][i]
                i += 1
                if index[w] == -1:
                    work[-1] = (v, i)
                    work.append((w, 0))
                    recurse = True
                    break
                elif on[w]:
                    low[v] = min(low[v], index[w])
            if recurse:
                continue
            work[-1] = (v, i)
            if low[v] == index[v]:
                while True:
                    w = stack.pop()
                    on[w] = False
                    comp[w] = ncomp
                    if w == v:
                        break
                ncomp += 1
            work.pop()
            if work:
                u, _ = work[-1]
                low[u] = min(low[u], low[v])
    return np.array(comp, np.int64), ncomp


def layers_dirs(meta, resolve_path, weight):
    """The Layers lens as a pseudo directory tree: rank rows (highest rank
    on top) holding files and, for cycles of more than one file, a group.
    Rank is the longest path down to a file that references nothing in the
    corpus; files with no edges at all sit in a bottom row of their own."""
    n = len(meta["files"])
    src, dst, cnt = file_graph(resolve_path, n)
    comp, ncomp = strongly_connected(n, src, dst)
    csrc, cdst = comp[src], comp[dst]
    succ = [set() for _ in range(ncomp)]
    for a, b in zip(csrc.tolist(), cdst.tolist()):
        if a != b:
            succ[a].add(b)
    rank = np.zeros(ncomp, np.int64)
    for c in range(ncomp):                 # successors have smaller ids
        rank[c] = max((rank[d] + 1 for d in succ[c]), default=0)
    connected = np.zeros(n, bool)
    connected[src] = True
    connected[dst] = True
    file_rank = np.where(connected, rank[comp], -1)
    members = [[] for _ in range(ncomp)]
    for f in range(n):
        members[comp[f]].append(f)
    dirs = [{"path": "", "parent": -1, "children": [], "files": [], "top": 0,
             "label": meta["name"], "rows": True, "hue": 0}]
    file_dir = np.zeros(n, np.int64)
    for r in sorted(set(file_rank.tolist()), reverse=True):
        in_row = np.flatnonzero(file_rank == r)
        row = len(dirs)
        label = f"layer {r} · {len(in_row)} files" if r >= 0 else f"no references · {len(in_row)} files"
        dirs.append({"path": f"layer{r}", "parent": 0, "children": [], "files": [], "top": row,
                     "label": label, "hue": (r % 12) if r >= 0 else 0})
        dirs[0]["children"].append(row)
        done = set()
        for f in sorted(in_row.tolist(), key=lambda f: meta["files"][f]["path"]):
            c = int(comp[f])
            if len(members[c]) > 1:
                if c in done:
                    continue
                done.add(c)
                g = len(dirs)
                dirs.append({"path": f"layer{r}/cycle{c}", "parent": row, "children": [],
                             "files": list(members[c]), "top": row,
                             "label": f"cycle · {len(members[c])} files", "hue": dirs[row]["hue"]})
                dirs[row]["children"].append(g)
                for m in members[c]:
                    file_dir[m] = g
            else:
                dirs[row]["files"].append(f)
                file_dir[f] = row
    stats = {"edges": int(len(src)), "cycles": int(sum(1 for m in members if len(m) > 1)),
             "in_cycles": int(sum(len(m) for m in members if len(m) > 1)),
             "layers": int(file_rank.max()) + 1, "unconnected": int((~connected).sum())}
    return dirs, file_dir, stats


# ---------------------------------------------------------------- files

def file_columns(w, h, n, target, char_aspect):
    """(k, rows, pitch, col_w, cap): the most columns whose capacity still
    reaches target characters (cap falls monotonically with k), else k = 1."""
    if n == 0:
        n = 1
    chosen = None
    for k in range(1, min(MAX_COLS, n) + 1):
        rows = -(-n // k)
        p = h / (rows + 2)
        cw = w / (k + GAP * (k - 1))
        cap = int(cw / (p * char_aspect)) if p > 0 else 0
        if chosen is not None and cap < target:
            break
        if cap >= target or chosen is None:
            chosen = (k, rows, p, cw, cap)
    k, rows, p, cw, cap = chosen
    k2 = -(-n // rows)                 # columns the rows can actually fill
    if k2 < k:                         # widen them instead of leaving empty strips
        cw = w / (k2 + GAP * (k2 - 1))
        chosen = (k2, rows, p, cw, int(cw / (p * char_aspect)) if p > 0 else 0)
    return chosen


def file_tokens(kinds, line_off, file_line0):
    """Tokens per file: runs of one non-space kind, restarted at every line
    (lines are stored with no separator). The video's default area metric."""
    kinds = kinds.astype(np.int16)
    prev = np.concatenate(([0], kinds[:-1]))
    start = (kinds != 0) & (prev != kinds)
    lo = line_off[:-1].astype(np.int64)
    lo = lo[lo < len(kinds)]
    start[lo] |= kinds[lo] != 0
    cum = np.concatenate(([0], np.cumsum(start)))
    fo = line_off[file_line0].astype(np.int64)      # first byte of each file
    return np.diff(cum[fo]).astype(np.float64)


HANG = 2                   # columns a wrapped continuation row hangs in by
WRAP_MIN = 16              # narrower columns clip instead of wrapping


def rows_per_line(line_len, capw):
    """Visual rows a line takes in a column of capw characters: 1, or 1 plus
    the continuation rows of capw - HANG characters each."""
    L = line_len.astype(np.int64)
    extra = np.maximum(L - capw, 0)
    return 1 + (extra + (capw - HANG) - 1) // (capw - HANG)


def layout_files(file_rect, file_line0, line_len, char_aspect, page=None):
    """Per file: pitch, columns, rows per column, characters per column,
    column width (with its gap). Long lines wrap inside their column, so the
    row count is found by iterating the pitch and the capacity to a fixed
    point; columns narrower than WRAP_MIN characters clip instead. For a
    book `page` is (lines, width): every file is pitched as a full page and
    aims at the book's line width, so a short last page keeps its chapter's
    text size and no page splits into columns."""
    n_files = len(file_rect)
    pitch = np.zeros(n_files)
    cols = np.zeros(n_files, np.int64)
    rows = np.zeros(n_files, np.int64)
    cap = np.zeros(n_files, np.int64)
    colw = np.zeros(n_files)
    for f in range(n_files):
        x0, y0, x1, y1 = file_rect[f]
        a, b = int(file_line0[f]), int(file_line0[f + 1])
        n = b - a
        lens = line_len[a:b]
        if page is None:
            target = int(np.percentile(lens, 90)) if n else TARGET_MIN
            target = min(max(target, TARGET_MIN), TARGET_MAX)
            k, r, p, cw, c = file_columns(x1 - x0, y1 - y0, n, target, char_aspect)
        else:
            k, r, p, cw, c = file_columns(x1 - x0, y1 - y0, max(n, page[0]), page[1], char_aspect)
        c = min(c, CAP_MAX)
        if n and c >= WRAP_MIN and (lens > c).any():
            h = y1 - y0
            for _ in range(4):                # pitch and capacity to a fixed point
                capw = c if c >= WRAP_MIN else CAP_MAX
                total = int(rows_per_line(lens, capw).sum())
                r = -(-total // k)
                p = h / (r + 2)
                c2 = min(int(cw / (p * char_aspect)) if p > 0 else 0, CAP_MAX)
                if c2 == c:
                    break
                c = c2
            capw = c if c >= WRAP_MIN else CAP_MAX
            r = -(-int(rows_per_line(lens, capw).sum()) // k)
            p = h / (r + 2)
        pitch[f], cols[f], rows[f], cap[f], colw[f] = p, k, r, c, cw * (1 + GAP)
    return pitch, cols, rows, cap, colw


def wrap_rows(file_line0, line_len, cap):
    """The visual rows of every line: line_row0 (n_lines + 1), and per row
    its line, first column, length and whether it is the line's first row."""
    n_lines = len(line_len)
    per_file = np.diff(file_line0).astype(np.int64)
    lf = np.repeat(np.arange(len(per_file)), per_file)
    capw = np.where(cap[lf] >= WRAP_MIN, cap[lf], CAP_MAX)
    nrows = rows_per_line(line_len, capw)
    line_row0 = np.concatenate(([0], np.cumsum(nrows)))
    n_rows = int(line_row0[-1])
    row_line = np.repeat(np.arange(n_lines), nrows)
    ri = np.arange(n_rows) - np.repeat(line_row0[:-1], nrows)
    cw = capw[row_line]
    first = ri == 0
    col0 = np.where(first, 0, cw + (ri - 1) * (cw - HANG))
    L = line_len.astype(np.int64)[row_line]
    row_len = np.where(first, np.minimum(L, cw), np.minimum(L - col0, cw - HANG))
    return line_row0, row_line, col0, np.maximum(row_len, 0), first


def row_positions(file_rect, file_row0, pitch, rows, colw, row_first, char_aspect):
    n_rows = len(row_first)
    per_file = np.diff(file_row0).astype(np.int64)
    rf = np.repeat(np.arange(len(per_file)), per_file)
    j = np.arange(n_rows) - file_row0[:-1][rf].astype(np.int64)
    c, r = j // rows[rf], j % rows[rf]
    x = file_rect[rf, 0] + c * colw[rf] + np.where(row_first, 0.0, HANG * pitch[rf] * char_aspect)
    y = file_rect[rf, 1] + pitch[rf] * (1 + r)
    return np.stack([x, y], axis=1)


def item_rects(item_file, s_row, e_row, file_rect, file_row0, pitch, rows, colw):
    """One rectangle per column an item spans; s_row and e_row are the
    item's first and one-past-last visual rows, file-relative."""
    f = item_file.astype(np.int64)
    n_rows_f = np.diff(file_row0).astype(np.int64)[f]
    s = s_row.astype(np.int64)
    e = np.minimum(e_row.astype(np.int64), n_rows_f)
    ok = e > s
    f, s, e = f[ok], s[ok], e[ok]
    ids = np.nonzero(ok)[0]
    rw = rows[f]
    c0, c1 = s // rw, (e - 1) // rw
    cnt = c1 - c0 + 1
    rep = np.repeat(np.arange(len(f)), cnt)
    first = np.repeat(np.cumsum(cnt) - cnt, cnt)
    c = c0[rep] + (np.arange(len(rep)) - first)
    r0 = np.where(c == c0[rep], s[rep] - c * rw[rep], 0)
    r1 = np.where(c == c1[rep], e[rep] - c * rw[rep], rw[rep])
    ff = f[rep]
    x0 = file_rect[ff, 0] + c * colw[ff]
    x1 = x0 + colw[ff] / (1 + GAP)
    y0 = file_rect[ff, 1] + pitch[ff] * (1 + r0)
    y1 = file_rect[ff, 1] + pitch[ff] * (1 + r1)
    return np.stack([x0, y0, x1, y1], axis=1), ids[rep]


# ---------------------------------------------------------------- preview

def hue_rgb(i):
    """12 pastel hues; the viewer has its own palette, this is the preview's."""
    import colorsys
    r, g, b = colorsys.hsv_to_rgb((i % N_HUES) / N_HUES, 0.55, 0.9)
    return int(r * 255), int(g * 255), int(b * 255)


def mix(a, b, t):
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def render_preview(path, world, dirs, dir_rect, dir_pad, dir_hue, file_rect, file_hue,
                   pitch, cols, rows, cap, colw, file_row0, row_pos, row_len, row_indent,
                   char_aspect, width=2400):
    from PIL import Image, ImageDraw, ImageFont
    s = width / world[0]
    H = int(round(world[1] * s))
    bg = (28, 28, 30)
    img = Image.new("RGB", (width, H), bg)
    dr = ImageDraw.Draw(img)

    def px(r):
        return [r[0] * s, r[1] * s, r[2] * s, r[3] * s]

    for d in range(1, len(dirs)):             # pre-order: parents first, children paint inside
        x0, y0, x1, y1 = px(dir_rect[d])
        band = mix(bg, hue_rgb(dir_hue[d]), 0.4)
        dr.rectangle([x0, y0, max(x1 - 1, x0), max(y1 - 1, y0)], fill=band)
        p = dir_pad[d] * s
        if x1 - x0 > 2 * p + 1 and y1 - y0 > 2 * p + 1:
            dr.rectangle([x0 + p, y0 + p, x1 - p - 1, y1 - p - 1], fill=bg)
    outline = mix(bg, (138, 143, 154), 0.7)
    bar = mix(bg, (164, 168, 176), 0.8)
    n_bars = 0
    for f in range(len(file_rect)):
        x0, y0, x1, y1 = px(file_rect[f])
        p = pitch[f] * s
        a, b = int(file_row0[f]), int(file_row0[f + 1])
        dr.rectangle([x0, y0, max(x1 - 1, x0), max(y1 - 1, y0)], outline=outline, width=1)
        if b == a or y1 - y0 < 3:
            continue
        step = max(1, int(math.ceil(1 / p)))  # sub-pixel pitch: one sampled row per pixel row
        adv = pitch[f] * char_aspect * s
        bar_h = max(p, 1) - 1
        for j in range(a, b, step):
            ln = int(row_len[j])
            ind = min(int(row_indent[j]), ln)
            if ln <= ind:
                continue
            lx, ly = row_pos[j, 0] * s, row_pos[j, 1] * s
            dr.rectangle([lx + ind * adv, ly, lx + ln * adv, ly + bar_h], fill=bar)
            n_bars += 1
    try:
        font = ImageFont.load_default(size=11)
    except TypeError:
        font = ImageFont.load_default()
    for d in range(1, len(dirs)):
        x0, y0, x1, y1 = px(dir_rect[d])
        if min(x1 - x0, y1 - y0) < 48:
            continue
        label = dirs[d]["label"]
        p = dir_pad[d] * s
        tw = dr.textlength(label, font=font)
        if tw + 6 > 0.4 * (x1 - x0):
            continue
        dr.rectangle([x0 + p, y0 + p, x0 + p + tw + 6, y0 + p + 14], fill=(42, 42, 46))
        dr.text((x0 + p + 3, y0 + p + 1), label, fill=(230, 230, 230), font=font)
    img.save(path)
    print(f"wrote {path} ({width}x{H}, {n_bars} bars)")


# ---------------------------------------------------------------- main

def default_char_aspect():
    """atlas_font.py's advance / line box for its default font, else 0.6."""
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import atlas_font
        asc, desc, adv, _, _ = atlas_font.font_box(atlas_font.DEFAULT_FONT, 0)
        return adv / (asc + desc)
    except Exception as e:                # the bundled font is missing: the spec's default
        print(f"char aspect: atlas_font metrics unavailable ({e}); using 0.6")
        return 0.6


def parse_aspect(text):
    if ":" in text:
        a, b = text.split(":")
        return float(a) / float(b)
    return float(text)


METRICS = ["tokens", "references", "churn", "lines", "chars", "bytes"]


def file_metric(name, z, files, file_line0, atlas):
    if name == "tokens":
        return file_tokens(z["kinds"], z["line_off"], file_line0)
    if name == "references":
        rp = Path(atlas) / "resolve.npz"
        if not rp.exists():
            return None
        return np.load(rp)["file_refs_in"].astype(np.float64)
    if name == "churn":
        hp = Path(atlas) / "history.npz"
        if not hp.exists():
            return None
        hz = np.load(hp)
        # lines added plus removed over the whole history, floored at 1 so
        # an untouched file keeps a sliver
        return np.maximum(hz["file_added"].astype(np.float64) + hz["file_removed"], 1.0)
    if name == "chars":
        return z["file_chars"].astype(np.float64)
    if name == "lines":
        return np.diff(file_line0).astype(np.float64)
    return np.array([f["bytes"] for f in files], np.float64)


def build_layout(args, z, meta, metric_name, metric, t0, lens="folders"):
    dirs, files = meta["dirs"], meta["files"]
    file_line0, line_len, line_indent = z["file_line0"], z["line_len"], z["line_indent"]
    file_dir = z["file_dir"].astype(np.int64)
    n_files, n_lines = len(files), len(line_len)
    aspect = parse_aspect(args.aspect)
    world = np.array([WORLD_W, WORLD_W / aspect])
    weight = np.maximum(metric, 1.0)          # an empty file still gets a sliver

    # hue: the top-level directory's position among the root's children, plus
    # one so that the root and its loose files (hue 0) differ from the first;
    # files keep their tree's hue in every lens
    top_hue = np.zeros(len(dirs), np.int64)
    for i, c in enumerate(dirs[0]["children"]):
        top_hue[c] = (i + 1) % N_HUES
    tree_hue = np.array([top_hue[d["top"]] for d in dirs], np.int64)
    file_hue = tree_hue[file_dir]
    lens_stats = {}
    if lens == "layers":
        dirs, file_dir, lens_stats = layers_dirs(meta, Path(args.atlas) / "resolve.npz", weight)
        dir_hue = np.array([d["hue"] for d in dirs], np.int64)
    else:
        dir_hue = tree_hue
    n_dirs = len(dirs)
    page, book_cols = None, 0
    if meta.get("corpus") == "book":
        # a page cell's aspect: the book's line width by its fullest page
        page = (int(meta.get("page_lines", np.diff(file_line0).max())), int(np.percentile(line_len, 99.5)))
        dir_rect, dir_pad, file_rect, book_cols = book_layout(dirs, world, page[1] * args.char_aspect / (page[0] + 2))
    else:
        dir_rect, dir_pad, file_rect = layout_tree(dirs, weight, file_dir, world)
    dir_depth = np.zeros(n_dirs, np.int64)
    for d in range(1, n_dirs):
        dir_depth[d] = dir_depth[dirs[d]["parent"]] + 1

    pitch, cols, rows, cap, colw = layout_files(file_rect, file_line0, line_len, args.char_aspect, page)
    line_row0, row_line, row_col0, row_len, row_first = wrap_rows(file_line0, line_len, cap)
    file_row0 = line_row0[file_line0]
    row_pos = row_positions(file_rect, file_row0, pitch, rows, colw, row_first, args.char_aspect)
    row_indent = np.where(row_first, line_indent[row_line], 0)
    # items in rows: the first row of the start line to the first row of the end line
    f = z["item_file"].astype(np.int64)
    n_lines_f = np.diff(file_line0).astype(np.int64)
    gs = file_line0[f].astype(np.int64) + np.minimum(z["item_start"].astype(np.int64), n_lines_f[f])
    ge = file_line0[f].astype(np.int64) + np.minimum(z["item_end"].astype(np.int64), n_lines_f[f])
    s_row = line_row0[gs] - file_row0[f]
    e_row = line_row0[ge] - file_row0[f]
    irect, iitem = item_rects(z["item_file"], s_row, e_row, file_rect, file_row0, pitch, rows, colw)

    # layout.npz is the layout the viewer opens with: tokens for code, a book's one layout
    primary = metric_name == "tokens" or meta.get("corpus") == "book"
    suffix = "_layers" if lens == "layers" else ("" if primary else "_" + metric_name)
    np.savez(os.path.join(args.atlas, f"layout{suffix}.npz"),
             world=world.astype(np.float64),
             dir_rect=dir_rect.astype(np.float64), dir_pad=dir_pad.astype(np.float64),
             dir_depth=dir_depth.astype(np.uint16), dir_hue=dir_hue.astype(np.uint8),
             file_rect=file_rect.astype(np.float64), file_pitch=pitch.astype(np.float64),
             file_cols=cols.astype(np.uint16), file_rows=rows.astype(np.uint32),
             file_cap=cap.astype(np.uint16), file_colw=colw.astype(np.float64),
             file_hue=file_hue.astype(np.uint8), file_row0=file_row0.astype(np.uint32),
             file_metric=np.asarray(metric, np.float64), file_dir=np.asarray(file_dir, np.uint32),
             line_row0=line_row0.astype(np.uint32), row_line=row_line.astype(np.uint32),
             row_col0=row_col0.astype(np.uint16), row_len=row_len.astype(np.uint16),
             row_pos=row_pos.astype(np.float32),
             item_rect=irect.astype(np.float32), item_rect_item=iitem.astype(np.uint32))
    jdirs = []
    for d in range(n_dirs):
        label = dirs[d].get("label") or (meta["name"] if d == 0 else os.path.basename(dirs[d]["path"]) + "/")
        jdirs.append({"label": label, "rect": [float(v) for v in dir_rect[d]],
                      "depth": int(dir_depth[d]), "hue": int(dir_hue[d]),
                      "parent": int(dirs[d]["parent"]), "children": [int(c) for c in dirs[d]["children"]],
                      "files": [int(f) for f in dirs[d]["files"]]})
    with open(os.path.join(args.atlas, f"layout{suffix}.json"), "w") as fh:
        json.dump({"world": [float(world[0]), float(world[1])], "aspect": args.aspect,
                   "metric": metric_name, "lens": lens, "char_aspect": args.char_aspect,
                   "hang": HANG, "lens_stats": lens_stats, "dirs": jdirs}, fh)

    sides = np.minimum(file_rect[:, 2] - file_rect[:, 0], file_rect[:, 3] - file_rect[:, 1])
    n_rows = len(row_line)
    print(f"{args.atlas} [{lens if lens != 'folders' else metric_name}]: {n_files} files, {n_dirs} dirs, "
          + (f"{lens_stats['layers']} layers, {lens_stats['cycles']} cycles of {lens_stats['in_cycles']} files, "
             f"{lens_stats['unconnected']} unconnected; " if lens_stats else "")
          + (f"{book_cols} pages across, " if book_cols else "")
          + f"{n_lines} lines in "
          f"{n_rows} rows ({n_rows - n_lines} wrapped), {len(irect)} item rects; "
          f"world {world[0]:.0f}x{world[1]:.1f}, pitch {pitch.min():.4f}..{pitch.max():.3f} "
          f"(median {np.median(pitch):.3f}), columns 1..{cols.max()} (mean {cols.mean():.2f}), "
          f"{int((sides < 0.02).sum())} slivers under 0.02, {time.time() - t0:.2f}s")
    if args.preview and lens == "folders" and metric_name == (args.metric if args.metric != "all" else "tokens"):
        render_preview(args.preview, world, jdirs, dir_rect, dir_pad, dir_hue, file_rect,
                       file_hue, pitch, cols, rows, cap, colw, file_row0, row_pos, row_len,
                       row_indent, args.char_aspect, args.preview_width)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("atlas", metavar="ATLAS_DIR", help="data/<name>_atlas with index.npz + index.json")
    ap.add_argument("--aspect", default="16:9", help="world aspect W:H (default 16:9)")
    ap.add_argument("--metric", choices=["all"] + METRICS, default="all",
                    help="file size metric for the treemap; 'all' (default) writes layout.npz "
                         "(tokens) plus layout_references.npz (when resolve.npz exists) and "
                         "layout_lines.npz, which the viewer switches between")
    ap.add_argument("--lens", choices=["all", "folders", "layers"], default="all",
                    help="folders: the directory treemap; layers: files in rows by dependency rank "
                         "with cycles grouped (needs resolve.npz); all (default) writes both")
    ap.add_argument("--char-aspect", type=float, default=None,
                    help="character advance / line pitch of the font (default: atlas_font.py's "
                         "metric for the bundled font, JetBrains Mono NL 0.571; 0.6 if it is missing)")
    ap.add_argument("--preview", default=None, metavar="PNG",
                    help="render the tokens layout with Pillow at 2400 px wide")
    ap.add_argument("--preview-width", type=int, default=2400,
                    help="preview width in pixels (default 2400)")
    args = ap.parse_args()
    if args.char_aspect is None:
        args.char_aspect = default_char_aspect()

    t0 = time.time()
    z = np.load(os.path.join(args.atlas, "index.npz"))
    with open(os.path.join(args.atlas, "index.json")) as f:
        meta = json.load(f)
    names = ["tokens", "references", "churn", "lines"] if args.metric == "all" else [args.metric]
    if meta.get("corpus") == "book" and args.metric == "all":
        names = ["lines"]                 # every page is one cell whatever the metric; lines
                                          # keeps full pages level in 3D, only a short last page dips
    for name in names:
        metric = file_metric(name, z, meta["files"], z["file_line0"], args.atlas)
        if metric is None:
            if args.metric == "all":
                continue
            need = "history.npz; run ./atlas_history.py" if name == "churn" else "resolve.npz; run ./atlas_resolve.py"
            raise SystemExit(f"error: --metric {name} needs {args.atlas}/{need} {args.atlas}")
        if args.lens in ("folders", "all"):
            build_layout(args, z, meta, name, metric, t0)
    if args.lens in ("layers", "all"):
        if (Path(args.atlas) / "resolve.npz").exists():
            metric = file_metric("tokens", z, meta["files"], z["file_line0"], args.atlas)
            build_layout(args, z, meta, "tokens", metric, t0, lens="layers")
        elif args.lens == "layers":
            raise SystemExit(f"error: --lens layers needs {args.atlas}/resolve.npz; "
                             f"run ./atlas_resolve.py {args.atlas}")


if __name__ == "__main__":
    main()
