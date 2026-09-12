#!/usr/bin/env python3
"""Vector-texture glyph tier (Will Dobbie's "GPU text rendering with vector
textures"), the optional text rung of big-text above about 40 device pixels
per line.

Builds, for ASCII 32 to 126 of a monospace font, an RGBA8 atlas texture that
holds each glyph's quadratic bezier outline and a grid of cells listing the
curves that cross each cell, in Dobbie's format, so that
shaders/vt_glyph.glsl can compute exact anti-aliased coverage per fragment by
ray casting, at any magnification.

Coordinates: every glyph lives in the same cell box atlas_font.py uses, the
advance width by one line pitch (ascent + descent from hhea), baseline at
ascent from the top, uv (0, 0) top left and (1, 1) bottom right, the same uv
the raster atlas cell is sampled with. Points are stored as 16-bit fixed point
over [COORD_OFFSET, COORD_OFFSET + COORD_SCALE] on both axes so glyphs may
overhang the box a little.

Atlas layout (width 256 texels, each texel four bytes r g b a):

  row 0            directory: texel (code, 0) = header position (hx, hy) of
                   ASCII `code` as two ushorts (r g = x, b a = y); codes
                   outside 32..126 point at the space glyph
  header (hx, hy)  grid origin (gx, gy) as two ushorts
  (hx + 1, hy)     grid size (gw, gh) as two ushorts
  (hx + 2 + i, hy) point i of the glyph as two ushorts; the k-th curve of a
                   contour uses points at coordIndex 2 + off + 2k, +1, +2 so
                   consecutive curves share their end points; the contour is
                   closed by repeating its first point
  grid             gw by gh texels at (gx, gy): four one-byte coordIndex
                   values per cell texel, a second texel at (x + gw, y) for
                   cells with more than four curves; index values below 2 are
                   empty slots
  cell flags       Dobbie's trick: byte0 < byte1 means "read the second
                   texel", byte2 < byte3 means "the cell centre is inside"

Usage:
  ./vt_glyphs.py --font /System/Library/Fonts/Menlo.ttc [--index 0] [--grid 12]
                 [--out data/vt_menlo.npz]

The npz holds `atlas` (uint8 [h, w, 4]), `glyph_table` (int32 [128, 4] =
grid_x, grid_y, grid_w, grid_h in texels, indexed by ASCII code), `metrics`
(float64: char_aspect, ascent, descent, upem, advance) and `params` (int64:
grid, first, last). Upload the atlas as GL_RGBA8UI (see make_texture) and sample it
with the usampler2D in shaders/vt_glyph.glsl.
"""
import argparse
import os
import time
from pathlib import Path

import numpy as np

ATLAS_W = 256              # coordIndex is one byte, so a glyph's row is short
COORD_SCALE = 2.0          # stored 16-bit value v maps to v / 65535 * SCALE + OFFSET
COORD_OFFSET = -0.5
FIRST, LAST = 32, 126
MAX_CURVES_PER_CELL = 8    # two texels of four indices


# ---------------------------------------------------------------- outlines

def font_outlines(font_path, index=0, max_err=0.5, box=None):
    """Quadratic outlines of ASCII 32..126 in font units, plus metrics.

    `box` = (ascent, descent) in font units replaces the hhea line box, so
    the cell can be made identical to another tier's (the viewer passes
    atlas_font.font_box's, which is widened to the real glyph extents).

    Returns (glyphs, metrics): glyphs[code] is a list of contours, each an
    array [n, 3, 2] of quadratic beziers (p0, control, p2) that closes on
    itself; straight segments carry their midpoint as control. Composite
    glyphs are decomposed, TrueType implied on-curve points are inserted and
    cubic (CFF) segments are converted with cu2qu within `max_err` units of
    error on a 1000 unit em."""
    from fontTools.ttLib import TTFont
    from fontTools.pens.recordingPen import DecomposingRecordingPen

    font = TTFont(font_path, fontNumber=index)
    upem = font["head"].unitsPerEm
    hhea = font["hhea"]
    ascent, descent = hhea.ascent, -hhea.descent
    if ascent + descent <= 0:
        os2 = font["OS/2"]
        ascent, descent = os2.sTypoAscender, -os2.sTypoDescender
    if box is not None:
        ascent, descent = box
    cmap = font.getBestCmap()
    glyph_set = font.getGlyphSet()
    hmtx = font["hmtx"]
    notdef = ".notdef" if ".notdef" in glyph_set else None
    glyphs = {}
    advances = []
    for code in range(FIRST, LAST + 1):
        name = cmap.get(code, notdef)
        if name is None:
            glyphs[code] = []
            continue
        advances.append(hmtx[name][0])
        pen = DecomposingRecordingPen(glyph_set)
        glyph_set[name].draw(pen)
        glyphs[code] = contours_from_pen(pen.value, max_err * upem / 1000.0)
    advance = max(set(advances), key=advances.count) if advances else upem // 2
    metrics = {"upem": upem, "ascent": ascent, "descent": descent,
               "advance": advance, "char_aspect": advance / (ascent + descent)}
    return glyphs, metrics


def contours_from_pen(ops, max_err):
    """Turn recorded pen operations into closed quadratic contours."""
    contours = []
    cur = []          # list of (p0, c, p2) tuples
    start = None
    last = None

    def quad_spline(offs, end):
        """qCurveTo semantics: off-curve points with implied on-curve
        midpoints between consecutive ones, ending at `end`."""
        nonlocal last
        if not offs:
            if end != last:
                cur.append((last, mid(last, end), end))
            last = end
            return
        for i, c in enumerate(offs):
            p = end if i == len(offs) - 1 else mid(c, offs[i + 1])
            cur.append((last, c, p))
            last = p

    def close():
        nonlocal cur, start, last
        if cur:
            if last != start:
                cur.append((last, mid(last, start), start))
            contours.append(np.array(cur, np.float64))
        cur, start, last = [], None, None

    for op, args in ops:
        if op == "moveTo":
            close()
            start = last = tuple(args[0])
        elif op == "lineTo":
            p = tuple(args[0])
            if p != last:
                cur.append((last, mid(last, p), p))
            last = p
        elif op == "qCurveTo":
            pts = [tuple(p) if p is not None else None for p in args]
            if pts[-1] is None:
                # TrueType contour of off-curve points only: the on-curve
                # points are implied between every pair, including the wrap
                offs = pts[:-1]
                close()
                start = last = mid(offs[-1], offs[0])
                quad_spline(offs + offs[:1], start)
                continue
            quad_spline(pts[:-1], pts[-1])
        elif op == "curveTo":
            from fontTools.cu2qu.cu2qu import curve_to_quadratic
            from fontTools.pens.basePen import decomposeSuperBezierSegment
            pts = [tuple(p) for p in args]
            cubics = [tuple(pts)] if len(pts) == 3 else decomposeSuperBezierSegment(pts)
            for c1, c2, end in cubics:
                quad = curve_to_quadratic([last, c1, c2, end], max_err)
                quad_spline([tuple(q) for q in quad[1:-1]], tuple(quad[-1]))
        elif op in ("closePath", "endPath"):
            close()
    close()
    return [c for c in contours if len(c)]


def mid(a, b):
    return ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)


def signed_area(contours):
    """Shoelace area over the control polygons (sign is all that is used)."""
    total = 0.0
    for c in contours:
        pts = c[:, 0, :]
        x, y = pts[:, 0], pts[:, 1]
        total += 0.5 * np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y)
    return total


# ---------------------------------------------------------------- geometry

def quad_roots(p0, p1, p2):
    """t in [0, 1] where the quadratic bezier coordinate hits zero.

    Arrays broadcast together; returns [..., 2] with NaN for missing roots.
    Uses the cancellation-free form so straight segments (control at the
    midpoint) fall out as one root and one infinity."""
    a = p0 - 2.0 * p1 + p2
    b = p0 - p1
    d = b * b - a * p0
    with np.errstate(divide="ignore", invalid="ignore"):
        s = np.sqrt(np.where(d >= 0, d, np.nan))
        q = b + np.where(b >= 0, s, -s)
        t1 = q / a
        t2 = p0 / q
        lin = np.abs(a) < 1e-12 * (np.abs(p0) + np.abs(p1) + np.abs(p2) + 1e-30)
        t2 = np.where(lin, p0 / (2.0 * b), t2)
        t1 = np.where(lin, np.nan, t1)
    t = np.stack([t1, t2], axis=-1)
    return np.where((t >= 0.0) & (t <= 1.0), t, np.nan)


def bezier_at(p, t):
    """p [..., 3, 2], t [...] -> points [..., 2]."""
    mt = 1.0 - t
    return (mt * mt)[..., None] * p[..., 0, :] + (2 * t * mt)[..., None] * p[..., 1, :] + (t * t)[..., None] * p[..., 2, :]


def bezier_tangent(p, t):
    mt = 1.0 - t
    return 2.0 * mt[..., None] * (p[..., 1, :] - p[..., 0, :]) + 2.0 * t[..., None] * (p[..., 2, :] - p[..., 1, :])


def winding(curves, points, signed=True):
    """Non-zero winding number of each point [m, 2] w.r.t. curves [n, 3, 2],
    cast along +x: crossings with t in [0, 1) count by tangent direction.
    With signed=False it is the plain crossing count (even-odd rule)."""
    if len(curves) == 0:
        return np.zeros(len(points), np.int64)
    py = points[:, 1][:, None]                          # [m, 1]
    y = curves[None, :, :, 1] - py[..., None]            # [m, n, 3]
    t = quad_roots(y[..., 0], y[..., 1], y[..., 2])      # [m, n, 2]
    t = np.where(t < 1.0, t, np.nan)
    tf = np.nan_to_num(t, nan=0.0)
    pos = bezier_at(curves[None, :, None, :, :], tf)      # [m, n, 2, 2]
    tan = bezier_tangent(curves[None, :, None, :, :], tf)
    hit = ~np.isnan(t) & (pos[..., 0] > points[:, 0][:, None, None])
    weight = np.sign(tan[..., 1]) if signed else np.ones(tan.shape[:-1])
    return np.sum(np.where(hit, weight, 0.0), axis=(1, 2)).astype(np.int64)


def orient(contours):
    """Give every contour the TrueType direction the shader assumes: outer
    contours clockwise (negative shoelace area, y up), holes counter-clockwise.
    A contour is a hole when its first point lies inside an odd number of the
    other contours (even-odd, so mixed input directions do not matter). Fonts
    mix directions more often than one would think: SF Mono is
    counter-clockwise except for its mirrored glyphs, CFF fonts are all
    counter-clockwise."""
    out = []
    for i, c in enumerate(contours):
        others = [k for j, k in enumerate(contours) if j != i]
        depth = 0
        if others:
            depth = int(winding(np.concatenate(others), c[0, 0, :][None], signed=False)[0])
        area = signed_area([c])
        if (area > 0) == (depth % 2 == 0):
            c = np.ascontiguousarray(c[::-1, ::-1, :])
        out.append(c)
    return out


def curves_in_rect(curves, x0, y0, x1, y1, eps=1e-9):
    """Boolean [n]: does each quadratic bezier touch the rectangle?
    True if an end point is inside or the curve crosses an edge."""
    n = len(curves)
    if n == 0:
        return np.zeros(0, bool)
    ends = curves[:, [0, 2], :]
    inside = ((ends[..., 0] >= x0 - eps) & (ends[..., 0] <= x1 + eps)
              & (ends[..., 1] >= y0 - eps) & (ends[..., 1] <= y1 + eps)).any(axis=1)
    hit = inside
    for axis, lo, hi, other_lo, other_hi in ((0, x0, x1, y0, y1), (1, y0, y1, x0, x1)):
        for edge in (lo, hi):
            c = curves[:, :, axis] - edge
            t = quad_roots(c[:, 0], c[:, 1], c[:, 2])       # [n, 2]
            tf = np.nan_to_num(t, nan=0.0)
            pos = bezier_at(curves[:, None, :, :], tf)[..., 1 - axis]
            ok = ~np.isnan(t) & (pos >= other_lo - eps) & (pos <= other_hi + eps)
            hit = hit | ok.any(axis=1)
    return hit


# ---------------------------------------------------------------- packing

def pack_cell(indices, inside):
    """Two texels of four bytes for one grid cell.

    `indices` are the coordIndex values (all >= 2, distinct) of the curves
    that cross the cell. Dobbie's flags: byte0 < byte1 signals a second
    texel, byte2 < byte3 signals that the cell centre is inside the glyph.
    Index 1 is an empty slot the shader skips, used to force a flag."""
    idx = sorted(indices, reverse=True)
    n = len(idx)
    if n > MAX_CURVES_PER_CELL:
        raise ValueError(f"{n} curves in one cell (max {MAX_CURVES_PER_CELL})")
    t2 = [0, 0, 0, 0]
    if n == 0:
        t1 = [0, 0, 0, 1 if inside else 0]
    elif n == 1:
        t1 = [idx[0], 0, 0, 1 if inside else 0]
    elif n == 2:
        t1 = [idx[0], idx[1], 0, 1 if inside else 0]
    elif n == 3:
        t1 = [idx[0], 0, idx[2], idx[1]] if inside else [idx[0], idx[1], idx[2], 0]
    elif n == 4:
        t1 = [idx[0], idx[1], idx[3], idx[2]] if inside else idx
    else:
        a, b, c, d = idx[3], idx[0], idx[1], idx[2]     # a < b: more than four
        t1 = [a, b, d, c] if inside else [a, b, c, d]
        rest = idx[4:]
        t2 = rest + [0] * (4 - len(rest))
    assert (t1[0] < t1[1]) == (n > 4) and (t1[2] < t1[3]) == bool(inside), (t1, n, inside)
    return t1, t2


def ushort_texel(x, y):
    x, y = int(round(x)), int(round(y))
    assert 0 <= x <= 65535 and 0 <= y <= 65535, (x, y)
    return [x >> 8, x & 255, y >> 8, y & 255]


def quantize(v, even=False):
    """Cell-box coordinate -> 16-bit fixed point over the COORD range.
    On-curve points go to even values so a straight segment's midpoint
    control is exactly representable and the shader's solver sees a = 0."""
    q = (np.asarray(v, np.float64) - COORD_OFFSET) / COORD_SCALE * 65535.0
    if even:
        q = np.round(q / 2.0) * 2.0
    else:
        q = np.round(q)
    return np.clip(q, 0, 65535)


def dequantize(q):
    return np.asarray(q, np.float64) / 65535.0 * COORD_SCALE + COORD_OFFSET


def normalize_glyph(contours, metrics):
    """Font units -> cell box uv (x over the advance, y down from ascent),
    quantized and back so the CPU grid sees exactly the shader's curves.
    Returns (curves [n, 3, 2] in uv, coord_index [n], points [m, 2] u16)."""
    adv = metrics["advance"]
    height = metrics["ascent"] + metrics["descent"]
    points, coord_index, curves = [], [], []
    clipped = 0
    for c in contours:
        n = len(c)
        u = c[..., 0] / adv
        v = (metrics["ascent"] - c[..., 1]) / height
        uv = np.stack([u, v], axis=-1)                          # [n, 3, 2]
        lo, hi = COORD_OFFSET, COORD_OFFSET + COORD_SCALE
        clipped += int(((uv < lo) | (uv > hi)).any(axis=(1, 2)).sum())
        on = quantize(uv[:, 0, :], even=True)                    # start points
        ctl = quantize(uv[:, 1, :])
        end = np.roll(on, -1, axis=0)                            # closed: next start
        straight = np.all(np.abs(c[:, 1, :] - (c[:, 0, :] + c[:, 2, :]) * 0.5) < 1e-9, axis=1)
        ctl[straight] = (on[straight] + end[straight]) * 0.5     # exact midpoints
        base = 2 + len(points)
        for k in range(n):
            coord_index.append(base + 2 * k)
            points.append(on[k])
            points.append(ctl[k])
            curves.append(np.stack([on[k], ctl[k], end[k]]))
        points.append(on[0])                                      # close the contour
    if curves:
        curves = dequantize(np.array(curves))
        points = np.array(points)
    else:
        curves = np.zeros((0, 3, 2))
        points = np.zeros((0, 2))
    return curves, np.array(coord_index, np.int64), points, clipped


def grid_cells(curves, coord_index, grid):
    """Per cell (row-major, y down): list of coordIndex values and inside flag."""
    cells = []
    overflow = 0
    centres = np.array([[(i + 0.5) / grid, (j + 0.5) / grid] for j in range(grid) for i in range(grid)])
    inside = winding(curves, centres) != 0
    for j in range(grid):
        for i in range(grid):
            hit = curves_in_rect(curves, i / grid, j / grid, (i + 1) / grid, (j + 1) / grid, eps=0.5 / 65535)
            idx = [int(v) for v in coord_index[hit]]
            overflow = max(overflow, len(idx))
            cells.append((idx, bool(inside[j * grid + i])))
    return cells, overflow


def build(font_path, index=0, grid=12, box=None):
    """-> (rgba8 atlas uint8 [h, w, 4], glyph_table int32 [128, 4]);
    glyph_table[code] = (grid_x, grid_y, grid_w, grid_h) in texels."""
    atlas, glyph_table, _ = build_atlas(font_path, index, grid, box=box)
    return atlas, glyph_table


def build_atlas(font_path, index=0, grid=12, verbose=False, box=None):
    """build() plus the metrics dict (char_aspect, ascent, descent, upem,
    advance, grid, max_points, max_per_cell, refined) as a third value.

    A glyph whose cells overflow MAX_CURVES_PER_CELL at `grid` gets its own
    finer grid (2x, then 4x); the atlas stores each glyph's grid size, so the
    shader does not care. The grid blocks are shelf-packed below the curve rows."""
    t0 = time.time()
    glyphs, metrics = font_outlines(font_path, index, box=box)
    for c in glyphs:
        glyphs[c] = orient(glyphs[c])
    n_glyphs = LAST - FIRST + 1
    per_glyph = []
    max_points = max_per_cell = refined = 0
    clipped = 0
    for code in range(FIRST, LAST + 1):
        curves, coord_index, points, clip = normalize_glyph(glyphs[code], metrics)
        clipped += clip
        if 2 + len(points) > ATLAS_W:
            raise ValueError(f"glyph {code} ({chr(code)!r}) has {len(points)} points, "
                             f"more than fit one atlas row of {ATLAS_W}")
        g = grid
        while True:
            cells, per_cell = grid_cells(curves, coord_index, g)
            if per_cell <= MAX_CURVES_PER_CELL:
                break
            if g >= grid * 4 or 4 * g > ATLAS_W:
                raise ValueError(f"glyph {code} ({chr(code)!r}) has {per_cell} curves in one "
                                 f"cell at grid {g} (max {MAX_CURVES_PER_CELL})")
            g *= 2
        refined += g != grid
        max_points = max(max_points, len(points))
        max_per_cell = max(max_per_cell, per_cell)
        per_glyph.append((points, cells, g))
    # rows: 0 directory, 1..n_glyphs curves, then the grid blocks (2g wide
    # for the two index texels, g tall) shelf-packed largest first
    y0 = 1 + n_glyphs
    order = sorted(range(n_glyphs), key=lambda i: -per_glyph[i][2])
    pos = {}
    x = y = shelf = 0
    for i in order:
        g = per_glyph[i][2]
        if x + 2 * g > ATLAS_W:
            x, y, shelf = 0, y + shelf, 0
        pos[i] = (x, y0 + y)
        x, shelf = x + 2 * g, max(shelf, g)
    height = y0 + y + shelf
    atlas = np.zeros((height, ATLAS_W, 4), np.uint8)
    glyph_table = np.zeros((128, 4), np.int32)
    for i, (points, cells, g) in enumerate(per_glyph):
        code = FIRST + i
        hx, hy = 0, 1 + i
        gx, gy = pos[i]
        atlas[0, code] = ushort_texel(hx, hy)
        atlas[hy, hx] = ushort_texel(gx, gy)
        atlas[hy, hx + 1] = ushort_texel(g, g)
        for k, (px, py) in enumerate(points):
            atlas[hy, hx + 2 + k] = ushort_texel(px, py)
        for k, (idx, inside) in enumerate(cells):
            cx, cy = k % g, k // g
            t1, t2 = pack_cell(idx, inside)
            atlas[gy + cy, gx + cx] = t1
            atlas[gy + cy, gx + g + cx] = t2
        glyph_table[code] = (gx, gy, g, g)
    for code in list(range(0, FIRST)) + [127]:
        atlas[0, code] = atlas[0, FIRST]
        glyph_table[code] = glyph_table[FIRST]
    if verbose:
        print(f"{Path(font_path).name}[{index}] {n_glyphs} glyphs, grid {grid}x{grid}"
              f"{f' ({refined} refined to a finer grid)' if refined else ''}, "
              f"atlas {ATLAS_W}x{height} RGBA8 ({atlas.nbytes / 1024:.0f} KB), "
              f"max {max_points} points per glyph, max {max_per_cell} curves per cell, "
              f"{clipped} points clipped to the coordinate range, "
              f"char_aspect {metrics['char_aspect']:.4f}, {time.time() - t0:.2f} s")
    metrics = dict(metrics, grid=grid, max_points=max_points, max_per_cell=max_per_cell, refined=refined)
    return atlas, glyph_table, metrics


def save(path, atlas, glyph_table, metrics):
    np.savez(path, atlas=atlas, glyph_table=glyph_table,
             metrics=np.array([metrics["char_aspect"], metrics["ascent"], metrics["descent"], metrics["upem"],
                               metrics["advance"]], np.float64),
             params=np.array([metrics["grid"], FIRST, LAST], np.int64))


def load(path):
    """-> (atlas, glyph_table, metrics dict)"""
    z = np.load(path)
    m = z["metrics"]
    p = z["params"]
    metrics = {"char_aspect": float(m[0]), "ascent": float(m[1]), "descent": float(m[2]),
               "upem": float(m[3]), "advance": float(m[4]), "grid": int(p[0]), "first": int(p[1]), "last": int(p[2])}
    return z["atlas"], z["glyph_table"], metrics


def make_texture(atlas):
    """Upload the atlas as a GL_RGBA8UI texture (nearest, no mipmaps) and
    return the texture id; bind it to the usampler2D `vt_atlas`."""
    from OpenGL.GL import (glGenTextures, glBindTexture, glTexParameteri, glTexImage2D, glPixelStorei,
                           GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER, GL_NEAREST,
                           GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE, GL_RGBA8UI,
                           GL_RGBA_INTEGER, GL_UNSIGNED_BYTE, GL_UNPACK_ALIGNMENT)
    tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, tex)
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    h, w = atlas.shape[:2]
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8UI, w, h, 0, GL_RGBA_INTEGER, GL_UNSIGNED_BYTE,
                 np.ascontiguousarray(atlas, np.uint8))
    return tex


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--font", default="/System/Library/Fonts/Menlo.ttc", help="TTF/TTC/OTF path")
    ap.add_argument("--index", type=int, default=0, help="face index in a collection")
    ap.add_argument("--grid", type=int, default=12, help="cells per glyph box side")
    ap.add_argument("--out", default=None, help="output npz (default data/vt_<font>.npz)")
    args = ap.parse_args()
    out = args.out or os.path.join("data", f"vt_{Path(args.font).stem.lower()}.npz")
    atlas, table, metrics = build_atlas(args.font, args.index, args.grid, verbose=True)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    save(out, atlas, table, metrics)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
