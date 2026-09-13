#!/usr/bin/env python3
"""Stage 2: lay out an index as a squarified treemap with wrapped columns.

Reads data/<name>_atlas/index.npz + index.json and writes layout.npz +
layout.json (see docs/DESIGN.md for the arrays). The root is [0, 1600] by
[0, 1600 / aspect] in world units, y down. Every directory is inset by its
padding (the band the viewer draws in the directory's hue) and its files and
subdirectories are squarified inside, biggest first, each file's area its
weight: tokens for code, lines for a book. A file is split into k equal
columns so that its text is as large as possible while a column still holds
the file's typical line length; lines longer than a column wrap inside it.
A book (index.json "corpus": "book") is laid out in reading order instead:
one grid of pages, as Dobbie's War and Peace, or with --book-layout parts
every part its own band of page rows; either way the layout is flat,
nothing rises in 3D.

  ./atlas_layout.py data/big-text_atlas --preview docs/shots/big-text_layout.png
"""
import argparse
import json
import math
import os
import time

import numpy as np

WORLD_W = 1600.0
PAD_FRAC, PAD_MIN, PAD_MAX = 0.015, 0.05, 4.0
PAD_LINES, PAD_MIN_LINES = 2.0, 0.01   # a treemap directory's padding: about two of its own lines
PAGE_GAP = 0.08            # a book's gutter between pages, as a fraction of the cell width, as Dobbie's grid
PAGE_MARGIN = (0.075, 0.09, 0.03)   # a book page's margins: left and right, top, bottom, as fractions of the page; the footer sits in the bottom one
AREA_PER_LINE = 34.0       # world area a line takes at pitch 1: p tall, about 60 characters of 0.57 p wide
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


def dir_padding_lines(w, h, lines):
    """A treemap directory's padding: PAD_LINES pitches of its typical file,
    the pitch estimated from its area over its lines, so the band is about
    two lines thick at whatever zoom reads the directory's own text and a
    hairline at the fit, instead of a fraction of the rectangle that grows
    to hundreds of pixels once the text is legible."""
    p = math.sqrt(max(w * h, 1e-9) / (AREA_PER_LINE * max(lines, 1)))
    pad = min(max(PAD_LINES * p, PAD_MIN_LINES), PAD_MAX)
    return min(pad, 0.25 * min(w, h))


def layout_tree(dirs, file_weight, file_dir, world, file_lines):
    """Directory and file rectangles by recursive squarification."""
    n_dirs, n_files = len(dirs), len(file_weight)
    dir_weight = np.zeros(n_dirs)
    dir_lines = np.zeros(n_dirs)
    for d in range(n_dirs - 1, -1, -1):      # pre-order: children come after parents
        dir_weight[d] = sum(file_weight[f] for f in dirs[d]["files"]) \
            + sum(dir_weight[c] for c in dirs[d]["children"])
        dir_lines[d] = sum(file_lines[f] for f in dirs[d]["files"]) \
            + sum(dir_lines[c] for c in dirs[d]["children"])
    dir_rect = np.zeros((n_dirs, 4))
    dir_pad = np.zeros(n_dirs)
    file_rect = np.zeros((n_files, 4))

    def place(d, rect):
        x0, y0, x1, y1 = rect
        dir_rect[d] = rect
        pad = dir_padding_lines(x1 - x0, y1 - y0, dir_lines[d])
        dir_pad[d] = pad
        kids = [(file_weight[f], 0, f) for f in dirs[d]["files"]] \
            + [(dir_weight[c], 1, c) for c in dirs[d]["children"]]
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


def book_flow(dirs, world, page_aspect):
    """Rectangles for a book as one grid: every page one cell of one size,
    rows filled left to right in reading order across the whole book, the
    parts not drawn (the layout's tree is the root alone, every page its
    file; the paths still carry part and chapter). The column count is the
    one that brings a cell closest to page_aspect from the wide side, so a
    page's single column holds its lines. Returns (dirs, file_dir,
    dir_rect, dir_pad, file_rect, cols)."""
    files = sorted(f for d in dirs for f in d["files"])   # index order is reading order
    n = len(files)
    root = dict(dirs[0], children=[], files=files)
    dir_rect, dir_pad, file_rect = np.zeros((1, 4)), np.zeros(1), np.zeros((n, 4))
    dir_rect[0] = (0.0, 0.0, world[0], world[1])
    pad = dir_padding(world[0], world[1])
    dir_pad[0] = pad
    x0, y0, x1, y1 = pad, pad, world[0] - pad, world[1] - pad
    best = None
    for cols in range(1, max(n, 1) + 1):
        rows = -(-n // cols)
        cw, ch = (x1 - x0) / cols, (y1 - y0) / rows
        gap = PAGE_GAP * cw
        if ch <= gap:                         # rows too short for a gutter: more columns
            continue
        ratio = (cw - gap) / (ch - gap) / page_aspect
        key = (ratio < 1, abs(math.log(ratio)))
        if best is None or key < best[0]:
            best = (key, cols, rows)
    _, cols, rows = best
    cw, ch = (x1 - x0) / cols, (y1 - y0) / rows
    g = PAGE_GAP * cw / 2                     # half a gutter on every side of a page
    for k, f in enumerate(files):
        i, j = divmod(k, cols)
        file_rect[f] = (x0 + j * cw + g, y0 + i * ch + g, x0 + (j + 1) * cw - g, y0 + (i + 1) * ch - g)
    return [root], np.zeros(n, np.int64), dir_rect, dir_pad, file_rect, cols


# ---------------------------------------------------------------- files

def file_columns(w, h, n, target):
    """(k, rows, pitch, col_w, capw): the most columns whose capacity, in
    line heights, still reaches the target width (the capacity falls
    monotonically with k), else k = 1."""
    if n == 0:
        n = 1
    chosen = None
    for k in range(1, min(MAX_COLS, n) + 1):
        rows = -(-n // k)
        p = h / (rows + 2)
        cw = w / (k + GAP * (k - 1))
        capw = cw / p if p > 0 else 0.0
        if chosen is not None and capw < target:
            break
        if capw >= target or chosen is None:
            chosen = (k, rows, p, cw, capw)
    k, rows, p, cw, capw = chosen
    k2 = -(-n // rows)                 # columns the rows can actually fill
    if k2 < k:                         # widen them instead of leaving empty strips
        cw = w / (k2 + GAP * (k2 - 1))
        chosen = (k2, rows, p, cw, cw / p if p > 0 else 0.0)
    return chosen


def file_tokens(kinds, line_off, file_line0):
    """Tokens per file: runs of one non-space kind, restarted at every line
    (lines are stored with no separator): the code adapter's weight."""
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


def rows_per_line_w(line_w, capw, hang_w):
    """The same by width, in line heights: the estimate the pitch iteration
    uses for a proportional face (the exact wrap follows in wrap_rows_w)."""
    extra = np.maximum(line_w - capw, 0.0)
    return 1 + np.ceil(extra / max(capw - hang_w, 1e-6)).astype(np.int64)


def layout_files(file_rect, file_line0, line_len, line_w, char_aspect, page=None, prop=None):
    """Per file: pitch, columns, rows per column, characters per column
    (for a monospace face), column width (with its gap), and the column's
    capacity in line heights. Long lines wrap inside their column, so the
    row count is found by iterating the pitch and the capacity to a fixed
    point; monospace columns narrower than WRAP_MIN characters clip instead.
    `line_w` is every line's width in line heights (characters times the
    aspect for a monospace face, the sum of the advances for a proportional
    one, `prop` = (advances table, hang width) then). For a book `page` is
    (lines, width): every file is pitched as a full page and aims at the
    book's line width, so a short last page keeps its chapter's text size
    and no page splits into columns."""
    n_files = len(file_rect)
    pitch = np.zeros(n_files)
    cols = np.zeros(n_files, np.int64)
    rows = np.zeros(n_files, np.int64)
    cap = np.zeros(n_files, np.int64)
    colw = np.zeros(n_files)
    capw_out = np.zeros(n_files)
    hang_w = prop[1] if prop else HANG * char_aspect
    for f in range(n_files):
        x0, y0, x1, y1 = file_rect[f]
        a, b = int(file_line0[f]), int(file_line0[f + 1])
        n = b - a
        lens, widths = line_len[a:b], line_w[a:b]
        if page is None:
            target = float(np.percentile(widths, 90)) if n else TARGET_MIN * char_aspect
            target = min(max(target, TARGET_MIN * char_aspect), TARGET_MAX * char_aspect)
            k, r, p, cw, capw = file_columns(x1 - x0, y1 - y0, n, target)
        else:
            k, r, p, cw, capw = file_columns(x1 - x0, y1 - y0, max(n, page[0]), page[1])
        if prop:
            c = CAP_MAX                       # every character wraps by width; nothing clips
            if n and (widths > capw).any():
                h = y1 - y0
                for _ in range(4):            # pitch and capacity to a fixed point
                    total = int(rows_per_line_w(widths, capw, hang_w).sum())
                    r = -(-total // k)
                    p2 = h / (r + 2)
                    capw2 = cw / p2 if p2 > 0 else 0.0
                    if abs(capw2 - capw) < 1e-9:
                        break
                    p, capw = p2, capw2
                r = -(-int(rows_per_line_w(widths, capw, hang_w).sum()) // k)
                p = h / (r + 2)
                capw = cw / p if p > 0 else 0.0
        else:
            c = min(int(capw / char_aspect), CAP_MAX)
            if n and c >= WRAP_MIN and (lens > c).any():
                h = y1 - y0
                for _ in range(4):            # pitch and capacity to a fixed point
                    cw_ = c if c >= WRAP_MIN else CAP_MAX
                    total = int(rows_per_line(lens, cw_).sum())
                    r = -(-total // k)
                    p = h / (r + 2)
                    c2 = min(int(cw / (p * char_aspect)) if p > 0 else 0, CAP_MAX)
                    if c2 == c:
                        break
                    c = c2
                cw_ = c if c >= WRAP_MIN else CAP_MAX
                r = -(-int(rows_per_line(lens, cw_).sum()) // k)
                p = h / (r + 2)
            capw = cw / p if p > 0 else 0.0
        pitch[f], cols[f], rows[f], cap[f], colw[f], capw_out[f] = p, k, r, c, cw * (1 + GAP), capw
    return pitch, cols, rows, cap, colw, capw_out


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


def wrap_rows_w(file_line0, line_off, char_w, line_w, capw, hang_w):
    """The visual rows of every line for a proportional face: like
    wrap_rows, but a line breaks where the next character would end past
    the column's capacity in line heights, continuation rows holding capw
    minus the hang. Only the lines wider than their column are walked.
    Returns (line_row0, row_line, row_col0, row_len, row_first)."""
    n_lines = len(line_w)
    per_file = np.diff(file_line0).astype(np.int64)
    lf = np.repeat(np.arange(len(per_file)), per_file)
    cap_l = capw[lf]
    nrows = np.ones(n_lines, np.int64)
    breaks = {}                                   # line -> the columns its rows start at
    for i in np.flatnonzero(line_w > cap_l + 1e-9):
        o0, o1 = int(line_off[i]), int(line_off[i + 1])
        w = char_w[o0:o1]
        cols, start, room = [0], 0, float(cap_l[i])
        acc = 0.0
        for j in range(o1 - o0):
            if acc + w[j] > room + 1e-9 and j > start:
                cols.append(j)
                start, acc, room = j, 0.0, float(cap_l[i]) - hang_w
            acc += w[j]
        breaks[i] = cols
        nrows[i] = len(cols)
    line_row0 = np.concatenate(([0], np.cumsum(nrows)))
    n_rows = int(line_row0[-1])
    row_line = np.repeat(np.arange(n_lines), nrows)
    ri = np.arange(n_rows) - np.repeat(line_row0[:-1], nrows)
    first = ri == 0
    col0 = np.zeros(n_rows, np.int64)
    for i, cols in breaks.items():
        col0[line_row0[i]:line_row0[i + 1]] = cols
    L = np.diff(line_off).astype(np.int64)[row_line]
    nxt = np.concatenate((col0[1:], [0]))
    last = np.concatenate((row_line[1:] != row_line[:-1], [True]))
    row_len = np.where(last, L - col0, nxt - col0)
    return line_row0, row_line, col0, np.maximum(row_len, 0), first


def char_positions(line_off, char_w, line_row0, row_line, row_col0, row_len, justify=None, chars=None):
    """(char_x, row_width): every character's x within its row and every
    row's width, in line heights, for a proportional face. `justify`, when
    given, is (row mask, target width per row): those rows are stretched to
    the target by widening their word gaps evenly, as set type is."""
    start = np.concatenate(([0.0], np.cumsum(char_w)))       # each character's start in the corpus
    row_start = line_off[row_line].astype(np.int64) + row_col0.astype(np.int64)
    row_end = row_start + row_len.astype(np.int64)
    char_row = np.repeat(np.arange(len(row_line)), row_len.astype(np.int64))
    char_x = start[:-1] - start[row_start][char_row]
    row_width = start[row_end] - start[row_start]
    if justify is not None:
        mask, target = justify
        is_space = (chars == 32).astype(np.int64)
        cum = np.concatenate(([0], np.cumsum(is_space)))       # spaces before each character
        gaps = cum[row_end] - cum[row_start]                   # word gaps per row
        extra = target - row_width
        ok = mask & (gaps > 0) & (extra > 0) & (extra < 0.35 * np.maximum(target, 1e-9))
        add = np.where(ok, extra / np.maximum(gaps, 1), 0.0)
        before = cum[:-1] - cum[row_start][char_row]           # gaps before this character in its row
        char_x = char_x + add[char_row] * before
        row_width = np.where(ok, target, row_width)
    return char_x.astype(np.float32), row_width.astype(np.float32)


def row_positions(file_rect, file_row0, pitch, rows, colw, row_first, hang_w):
    """Top left of every row's cell; a continuation row hangs in by hang_w
    line heights."""
    n_rows = len(row_first)
    per_file = np.diff(file_row0).astype(np.int64)
    rf = np.repeat(np.arange(len(per_file)), per_file)
    j = np.arange(n_rows) - file_row0[:-1][rf].astype(np.int64)
    c, r = j // rows[rf], j % rows[rf]
    x = file_rect[rf, 0] + c * colw[rf] + np.where(row_first, 0.0, hang_w * pitch[rf])
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
            wd = float(row_len[j]) * pitch[f] * s        # row_len holds the row's width in line heights here
            ind = int(row_indent[j]) * adv
            if wd <= ind:
                continue
            lx, ly = row_pos[j, 0] * s, row_pos[j, 1] * s
            dr.rectangle([lx + ind, ly, lx + wd, ly + bar_h], fill=bar)
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

def font_advances(face, leading=1.0):
    """The advances of a face under the repository, in line heights (the
    ink box stretched by `leading`): {"cell": the widest (the atlas cell),
    "per_glyph": 224 values for the bytes 32..255}, and whether they differ."""
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import atlas_font
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), face)
    asc, desc, adv, _, _ = atlas_font.font_box(path, 0, leading)
    per, proportional = atlas_font.font_advances(path, 0)
    line = asc + desc
    return {"cell": adv / line, "per_glyph": [a / line for a in per]}, proportional


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


def file_weight(z, meta):
    """(name, weight per file): the area every file gets. Tokens for code;
    lines for a book, so every full page is one cell of one size and stands
    level in 3D, only a short last page dipping."""
    if meta.get("corpus") == "book":
        return "lines", np.diff(z["file_line0"]).astype(np.float64)
    return "tokens", file_tokens(z["kinds"], z["line_off"], z["file_line0"])


def build_layout(args, z, meta, t0):
    dirs, files = meta["dirs"], meta["files"]
    file_line0, line_len, line_indent = z["file_line0"], z["line_len"], z["line_indent"]
    line_off = z["line_off"].astype(np.int64)
    file_dir = z["file_dir"].astype(np.int64)
    n_files, n_lines = len(files), len(line_len)
    # line widths in line heights: characters times the aspect, or, for a
    # proportional face named by the scheme, the sum of the glyph advances
    scheme = meta.get("scheme", {})
    prop, char_w, face = None, None, scheme.get("font")
    if face:
        advances, proportional = font_advances(face, float(scheme.get("leading", 1.0)))
        if proportional:
            args.char_aspect = advances["cell"]
            adv = np.zeros(256, np.float64)
            adv[32:32 + len(advances["per_glyph"])] = advances["per_glyph"]
            adv[:32] = adv[63]
            char_w = adv[z["chars"].astype(np.int64)]
            prop = (adv, HANG * float(np.mean(advances["per_glyph"])))
    if prop:
        start = np.concatenate(([0.0], np.cumsum(char_w)))
        line_w = start[line_off[1:]] - start[line_off[:-1]]
    else:
        line_w = line_len.astype(np.float64) * args.char_aspect
    aspect = parse_aspect(args.aspect)
    world = np.array([WORLD_W, WORLD_W / aspect])
    weight_name, metric = file_weight(z, meta)
    weight = np.maximum(metric, 1.0)          # an empty file still gets a sliver
    flat = meta.get("corpus") == "book"       # a book is one flat sheet: nothing rises in 3D

    # hue: the top-level directory's position among the root's children, plus
    # one so that the root and its loose files (hue 0) differ from the first
    top_hue = np.zeros(len(dirs), np.int64)
    for i, c in enumerate(dirs[0]["children"]):
        top_hue[c] = (i + 1) % N_HUES
    tree_hue = np.array([top_hue[d["top"]] for d in dirs], np.int64)
    file_hue = tree_hue[file_dir]
    dir_hue = tree_hue
    n_dirs = len(dirs)
    page, book_cols = None, 0
    if flat:
        # a page cell's aspect: the book's line width by its fullest page
        page = (int(meta.get("page_rows", meta.get("page_lines", np.diff(file_line0).max()))), float(np.percentile(line_w, 99.5)))
        mx, mt, mb = PAGE_MARGIN
        aspect_p = page[1] / (page[0] + 2) * (1 - mt - mb) / (1 - 2 * mx)   # the page around its text block
        if args.book_layout == "parts":
            dir_rect, dir_pad, file_rect, book_cols = book_layout(dirs, world, aspect_p)
        else:
            dirs, file_dir, dir_rect, dir_pad, file_rect, book_cols = book_flow(dirs, world, aspect_p)
            n_dirs, dir_hue = 1, np.zeros(1, np.int64)
            file_hue = np.zeros(n_files, np.int64)
    else:
        dir_rect, dir_pad, file_rect = layout_tree(dirs, weight, file_dir, world, np.diff(file_line0))
    dir_depth = np.zeros(n_dirs, np.int64)
    for d in range(1, n_dirs):
        dir_depth[d] = dir_depth[dirs[d]["parent"]] + 1

    # the text block: a book page's inner rectangle, the file itself for code
    file_text = file_rect.copy()
    if flat:
        mx, mt, mb = PAGE_MARGIN
        w, h = file_rect[:, 2] - file_rect[:, 0], file_rect[:, 3] - file_rect[:, 1]
        file_text += np.stack([mx * w, mt * h, -mx * w, -mb * h], axis=1)
    pitch, cols, rows, cap, colw, capw = layout_files(file_text, file_line0, line_len, line_w,
                                                      args.char_aspect, page, prop)
    if prop:
        # the exact wrap can take more rows than the estimate the pitch was
        # found with: lower the pitch of those files and wrap again until
        # every file's rows fit its columns
        fh = file_text[:, 3] - file_text[:, 1]
        for _ in range(8):
            line_row0, row_line, row_col0, row_len, row_first = wrap_rows_w(file_line0, line_off, char_w, line_w, capw, prop[1])
            need = -(-np.diff(line_row0[file_line0]).astype(np.int64) // cols)
            over = need > rows
            if not over.any():
                break
            rows[over] = need[over]
            pitch[over] = fh[over] / (rows[over] + 2)
            capw[over] = colw[over] / (1 + GAP) / pitch[over]
        justify = None
        if scheme.get("justify"):
            # a row is set flush when its line has more lines of its paragraph
            # after it: the next line of the file is not blank, and the line
            # itself starts at the margin (a centred heading does not)
            n_l = len(line_len)
            lf_ = z["line_file"].astype(np.int64)
            same_file = np.concatenate((lf_[1:] == lf_[:-1], [False]))
            next_full = np.zeros(n_l, bool)
            next_full[:-1] = line_len[1:] > 0
            line_ok = same_file & next_full & (line_indent == 0) & (line_len > 0)
            last_row = np.concatenate((row_line[1:] != row_line[:-1], [True]))
            row_ok = np.where(last_row, line_ok[row_line], True)   # a wrapped line's earlier rows are always set flush
            justify = (row_ok, capw[z["line_file"][row_line]])
        char_x, row_width = char_positions(line_off, char_w, line_row0, row_line, row_col0, row_len,
                                           justify, z["chars"])
    else:
        line_row0, row_line, row_col0, row_len, row_first = wrap_rows(file_line0, line_len, cap)
        char_x = None
        row_width = (np.minimum(row_len.astype(np.int64), cap[z["line_file"][row_line]].astype(np.int64)) * args.char_aspect).astype(np.float32)
    file_row0 = line_row0[file_line0]
    row_pos = row_positions(file_text, file_row0, pitch, rows, colw, row_first,
                            prop[1] if prop else HANG * args.char_aspect)
    row_indent = np.where(row_first, line_indent[row_line], 0)
    # items in rows: the first row of the start line to the first row of the end line
    f = z["item_file"].astype(np.int64)
    n_lines_f = np.diff(file_line0).astype(np.int64)
    gs = file_line0[f].astype(np.int64) + np.minimum(z["item_start"].astype(np.int64), n_lines_f[f])
    ge = file_line0[f].astype(np.int64) + np.minimum(z["item_end"].astype(np.int64), n_lines_f[f])
    s_row = line_row0[gs] - file_row0[f]
    e_row = line_row0[ge] - file_row0[f]
    irect, iitem = item_rects(z["item_file"], s_row, e_row, file_text, file_row0, pitch, rows, colw)

    np.savez(os.path.join(args.atlas, "layout.npz"),
             world=world.astype(np.float64),
             dir_rect=dir_rect.astype(np.float64), dir_pad=dir_pad.astype(np.float64),
             dir_depth=dir_depth.astype(np.uint16), dir_hue=dir_hue.astype(np.uint8),
             file_rect=file_rect.astype(np.float64), file_text=file_text.astype(np.float64),
             file_pitch=pitch.astype(np.float64),
             file_cols=cols.astype(np.uint16), file_rows=rows.astype(np.uint32),
             file_cap=cap.astype(np.uint16), file_colw=colw.astype(np.float64),
             file_hue=file_hue.astype(np.uint8), file_row0=file_row0.astype(np.uint32),
             file_weight=np.asarray(metric, np.float64), file_dir=np.asarray(file_dir, np.uint32),
             line_row0=line_row0.astype(np.uint32), row_line=row_line.astype(np.uint32),
             row_col0=row_col0.astype(np.uint16), row_len=row_len.astype(np.uint16),
             row_pos=row_pos.astype(np.float32), row_width=row_width,
             item_rect=irect.astype(np.float32), item_rect_item=iitem.astype(np.uint32),
             **({"char_x": char_x} if char_x is not None else {}))
    jdirs = []
    for d in range(n_dirs):
        label = dirs[d].get("label") or (meta["name"] if d == 0 else os.path.basename(dirs[d]["path"]) + "/")
        jdirs.append({"label": label, "rect": [float(v) for v in dir_rect[d]],
                      "depth": int(dir_depth[d]), "hue": int(dir_hue[d]),
                      "parent": int(dirs[d]["parent"]), "children": [int(c) for c in dirs[d]["children"]],
                      "files": [int(f) for f in dirs[d]["files"]]})
    with open(os.path.join(args.atlas, "layout.json"), "w") as fh:
        json.dump({"world": [float(world[0]), float(world[1])], "aspect": args.aspect,
                   "weight": weight_name, "flat": bool(flat), "char_aspect": args.char_aspect,
                   "proportional": bool(prop), "font": face, "hang": HANG, "dirs": jdirs}, fh)

    sides = np.minimum(file_rect[:, 2] - file_rect[:, 0], file_rect[:, 3] - file_rect[:, 1])
    n_rows = len(row_line)
    print(f"{args.atlas} [{weight_name}]: {n_files} files, {n_dirs} dirs, "
          + (f"{book_cols} pages across, " if book_cols else "")
          + f"{n_lines} lines in "
          f"{n_rows} rows ({n_rows - n_lines} wrapped), {len(irect)} item rects; "
          f"world {world[0]:.0f}x{world[1]:.1f}, pitch {pitch.min():.4f}..{pitch.max():.3f} "
          f"(median {np.median(pitch):.3f}), columns 1..{cols.max()} (mean {cols.mean():.2f}), "
          f"{int((sides < 0.02).sum())} slivers under 0.02, {time.time() - t0:.2f}s")
    if args.preview:
        render_preview(args.preview, world, jdirs, dir_rect, dir_pad, dir_hue, file_rect,
                       file_hue, pitch, cols, rows, cap, colw, file_row0, row_pos, row_width,
                       row_indent, args.char_aspect, args.preview_width)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("atlas", metavar="ATLAS_DIR", help="data/<name>_atlas with index.npz + index.json")
    ap.add_argument("--aspect", default="16:9", help="world aspect W:H (default 16:9)")
    ap.add_argument("--book-layout", choices=["flow", "parts"], default="flow",
                    help="a book: flow (default) is one grid of pages in reading order; parts gives "
                         "every part its own band of page rows")
    ap.add_argument("--char-aspect", type=float, default=None,
                    help="character advance / line pitch of the font (default: atlas_font.py's "
                         "metric for the bundled font, JetBrains Mono NL 0.571; 0.6 if it is missing)")
    ap.add_argument("--preview", default=None, metavar="PNG",
                    help="render the layout with Pillow at 2400 px wide")
    ap.add_argument("--preview-width", type=int, default=2400,
                    help="preview width in pixels (default 2400)")
    args = ap.parse_args()
    if args.char_aspect is None:
        args.char_aspect = default_char_aspect()

    t0 = time.time()
    z = np.load(os.path.join(args.atlas, "index.npz"))
    with open(os.path.join(args.atlas, "index.json")) as f:
        meta = json.load(f)
    build_layout(args, z, meta, t0)


if __name__ == "__main__":
    main()
