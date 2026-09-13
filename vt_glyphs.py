#!/usr/bin/env python3
"""Vector glyph tier on the Slug algorithm: the text rung of big-text above
VT_MIN_PPL device pixels per line, where the raster atlas would be
magnified.

Builds, for ASCII 32 to 126 of a monospace face, the two textures the
shader in shaders/vt_glyph.glsl reads, in the form of Eric Lengyel's
reference implementation (https://github.com/EricLengyel/Slug, MIT or
Apache-2.0; the algorithm's patent was dedicated to the public domain on
2026-03-17):

  curves  float32 [h, 4096, 4]   one texel per quadratic bezier: (p1.x, p1.y,
                                 p2.x, p2.y); the next texel's xy is p3, which
                                 for a closed contour is the next curve's p1,
                                 so a contour of n curves takes n + 1 texels,
                                 the last holding (p1 of the first curve, 0, 0);
                                 a contour never straddles a row end
  bands   uint16  [h, 4096, 2]   row 0 is the directory, four texels per code
                                 0..127: (glyph block x, y), (vertical bands
                                 - 1, horizontal bands - 1), (bbox x0, y0),
                                 (bbox x1, y1), the bbox in 16-bit fixed point
                                 over [-0.5, 1.5]; codes outside 32..126 point
                                 at the space glyph. Glyph blocks follow from
                                 row 1 in one linear address space that wraps
                                 at 4096 (address a is texel (a & 4095,
                                 a >> 12)): nh horizontal band headers (curve
                                 count, offset of the band's list from the
                                 block start), nv vertical headers, then the
                                 lists, one texel per curve holding that
                                 curve's texel (x, y) in `curves`

Glyph space is the cell box the raster tier uses too: x over the advance, y
UP from the box bottom (the shader maps the cell's y-down uv onto it), the
box being the face's ASCII ink extents so both tiers draw a glyph at the
same size and baseline. Coordinates are float32. A straight segment is
stored as {p1, p2, p2}, the second endpoint duplicated, as the reference
recommends.

Bands are equal-width over the glyph's ink bounding box; the number per
axis, 1 to 32, is the one that minimises the fullest band. A curve is in a
horizontal band when its control points' y range, widened by half the
largest pixel the tier serves (0.5 / min_ppl) plus 1/1024, overlaps the
band; vertical bands the same in x. A straight horizontal curve is in no
horizontal band and a straight vertical curve in no vertical band, since a
ray parallel to a line never crosses it. Horizontal lists are sorted by the
curves' maximum x descending, vertical lists by maximum y descending, which
is what the shader's early exit relies on.

Usage:
  ./vt_glyphs.py [--font fonts/JetBrainsMonoNL-Regular.ttf] [--index 0]
                 [--min-ppl 12] [--out data/vt_<font>.npz]

  build_atlas(font, index, min_ppl, box) -> (curves, bands, metrics)
  save / load the npz; make_textures(curves, bands) -> (tex_curves, tex_bands)
  to bind as the sampler2D `vt_curves` and the usampler2D `vt_bands`.
"""
import argparse
import os
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
DEFAULT_FONT = str(HERE / "fonts" / "JetBrainsMonoNL-Regular.ttf")
FIRST, LAST = 32, 126
TEX_W = 4096               # both textures, as in the reference (kLogBandTextureWidth 12)
LOG_W = 12
MAX_BANDS = 32             # the ceiling per axis; 16 left the fullest bands longer at large sizes, 64 thrashed the cache
BAND_EPS = 1.0 / 1024.0    # the reference's band overlap epsilon, in em
COORD_OFFSET = -0.5        # the directory's bbox fixed point covers [-0.5, 1.5]
COORD_SCALE = 2.0


# ---------------------------------------------------------------- outlines

def font_outlines(font_path, index=0, max_err=0.5, box=None):
    """Quadratic outlines of ASCII 32..126 in font units, plus metrics.

    `box` = (ascent, descent) in font units is the line box; by default the
    face's ASCII ink extents from atlas_font.font_box, the same box the
    raster tier uses, so both tiers draw a glyph at the same size and
    baseline.

    Returns (glyphs, metrics): glyphs[code] is a list of contours, each an
    array [n, 3, 2] of quadratic beziers (p1, control, p2) that closes on
    itself; straight segments carry their second endpoint as the control.
    Composite glyphs are decomposed, TrueType implied on-curve points are
    inserted and cubic (CFF) segments are converted with cu2qu within
    `max_err` units of error on a 1000 unit em."""
    from fontTools.ttLib import TTFont
    from fontTools.pens.recordingPen import DecomposingRecordingPen

    font = TTFont(font_path, fontNumber=index)
    upem = font["head"].unitsPerEm
    hhea = font["hhea"]
    ascent, descent = hhea.ascent, -hhea.descent
    if ascent + descent <= 0:
        os2 = font["OS/2"]
        ascent, descent = os2.sTypoAscender, -os2.sTypoDescender
    if box is None:
        import atlas_font
        box = atlas_font.font_box(font_path, index)[:2]
    ascent, descent = box
    cmap = font.getBestCmap()
    glyph_set = font.getGlyphSet()
    hmtx = font["hmtx"]
    notdef = ".notdef" if ".notdef" in glyph_set else None
    glyphs = {}
    advances = []
    per_glyph = {}
    for code in range(FIRST, LAST + 1):
        name = cmap.get(code, notdef)
        if name is None:
            glyphs[code] = []
            continue
        advances.append(hmtx[name][0])
        per_glyph[code] = hmtx[name][0]
        pen = DecomposingRecordingPen(glyph_set)
        glyph_set[name].draw(pen)
        glyphs[code] = contours_from_pen(pen.value, max_err * upem / 1000.0)
    proportional = len(set(advances)) > 1
    advance = (max(advances) if proportional else max(set(advances), key=advances.count)) if advances else upem // 2
    metrics = {"upem": upem, "ascent": ascent, "descent": descent,
               "advance": advance, "char_aspect": advance / (ascent + descent),
               "proportional": proportional,
               "advances": [per_glyph.get(code, advance) / (ascent + descent) for code in range(FIRST, LAST + 1)]}
    return glyphs, metrics


def contours_from_pen(ops, max_err):
    """Turn recorded pen operations into closed quadratic contours."""
    contours = []
    cur = []          # list of (p1, c, p2) tuples
    start = None
    last = None

    def line(a, b):
        cur.append((a, b, b))                    # a straight segment: {p1, p2, p2}

    def quad_spline(offs, end):
        """qCurveTo semantics: off-curve points with implied on-curve
        midpoints between consecutive ones, ending at `end`."""
        nonlocal last
        if not offs:
            if end != last:
                line(last, end)
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
                line(last, start)
            contours.append(np.array(cur, np.float64))
        cur, start, last = [], None, None

    for op, args in ops:
        if op == "moveTo":
            close()
            start = last = tuple(args[0])
        elif op == "lineTo":
            p = tuple(args[0])
            if p != last:
                line(last, p)
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
    """Shoelace area over the start points (sign is all that is used)."""
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
    Uses the cancellation-free form; a linear coordinate (a = 0) gives one
    root."""
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
    """Give every contour of a glyph the same convention: outer contours
    clockwise (negative shoelace area, y up), holes counter-clockwise. The
    shader's coverage takes an absolute value, so either convention renders,
    but one glyph must not mix them. A contour is a hole when its first
    point lies inside an odd number of the other contours (even-odd, so
    mixed input directions do not matter). Fonts mix directions more often
    than one would think: SF Mono is counter-clockwise except for its
    mirrored glyphs, CFF fonts are all counter-clockwise."""
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


# ---------------------------------------------------------------- glyph space and bands

def normalize_glyph(contours, metrics, adv=None):
    """Font units -> glyph space: x over the advance (the glyph's own for a
    proportional face, so its box is its advance box), y up from the box
    bottom (the baseline sits at descent / (ascent + descent)). Returns a
    list of contour arrays [n, 3, 2] and the count of control points
    outside the directory's bbox range."""
    adv = metrics["advance"] if adv is None else adv
    height = metrics["ascent"] + metrics["descent"]
    out, clipped = [], 0
    lo, hi = COORD_OFFSET, COORD_OFFSET + COORD_SCALE
    for c in contours:
        u = c[..., 0] / adv
        v = (c[..., 1] + metrics["descent"]) / height
        uv = np.stack([u, v], axis=-1)
        clipped += int(((uv < lo) | (uv > hi)).any(axis=(1, 2)).sum())
        out.append(uv)
    return out, clipped


def band_lists(curves, axis, n_bands, lo, hi, widen):
    """Per band of `n_bands` equal widths over [lo, hi] along `axis` (1 for
    horizontal bands, 0 for vertical), the indices of the curves whose
    widened extent along that axis overlaps it, sorted for the shader:
    horizontal bands by the curves' maximum x descending, vertical by
    maximum y descending. Curves parallel to the ray (straight along the
    band axis) are left out."""
    coord = curves[:, :, axis]
    cmin, cmax = coord.min(axis=1), coord.max(axis=1)
    straight = (cmax - cmin) < 1e-9
    other = curves[:, :, 1 - axis].max(axis=1)
    edges = lo + (hi - lo) * np.arange(n_bands + 1) / n_bands
    lists = []
    for j in range(n_bands):
        members = np.flatnonzero(~straight & (cmax + widen >= edges[j]) & (cmin - widen <= edges[j + 1]))
        members = members[np.argsort(-other[members], kind="stable")]
        lists.append(members)
    return lists


def choose_bands(curves, axis, lo, hi, widen, max_bands=MAX_BANDS):
    """The band count from 1 to max_bands that minimises the fullest band
    (the smaller count on a tie), and its lists."""
    best = None
    for n in range(1, max_bands + 1):
        lists = band_lists(curves, axis, n, lo, hi, widen)
        fullest = max((len(m) for m in lists), default=0)
        if best is None or fullest < best[0]:
            best = (fullest, n, lists)
        if fullest == 0:
            break
    return best[1], best[2], best[0]


def quantize(v):
    q = (np.asarray(v, np.float64) - COORD_OFFSET) / COORD_SCALE * 65535.0
    return np.clip(np.round(q), 0, 65535).astype(np.int64)


class Packer:
    """Texels appended to one linear address space of TEX_W-wide rows."""

    def __init__(self, channels, dtype, start=0):
        self.channels, self.dtype = channels, dtype
        self.addr = start

    def reserve(self, n, contiguous=False):
        """Address of `n` texels; with contiguous=True they do not straddle
        a row end (the shader reads curve pairs and headers without wrap)."""
        if contiguous and (self.addr & (TEX_W - 1)) + n > TEX_W:
            self.addr = (self.addr + TEX_W - 1) & ~(TEX_W - 1)
        a = self.addr
        self.addr += n
        return a

    def array(self):
        rows = max(-(-self.addr // TEX_W), 1)
        return np.zeros((rows, TEX_W, self.channels), self.dtype)


def build_atlas(font_path=DEFAULT_FONT, index=0, min_ppl=12.0, box=None, verbose=False, max_bands=MAX_BANDS):
    """-> (curves float32 [h, 4096, 4], bands uint16 [h, 4096, 2], metrics).

    metrics: char_aspect, ascent, descent, upem, advance, min_ppl, n_curves,
    max_band (the fullest band of any glyph), n_bands (all bands), and the
    two texture heights."""
    t0 = time.time()
    glyphs, metrics = font_outlines(font_path, index, box=box)
    widen = 0.5 / max(float(min_ppl), 1.0) + BAND_EPS
    n_glyphs = LAST - FIRST + 1

    # pass one: glyph space, curve texels per contour, bands
    per_glyph = []
    curve_pack = Packer(4, np.float32)
    n_curves = clipped = 0
    max_band = n_bands = 0
    for code in range(FIRST, LAST + 1):
        adv = metrics["advances"][code - FIRST] * (metrics["ascent"] + metrics["descent"]) if metrics["proportional"] else None
        contours, clip = normalize_glyph(orient(glyphs[code]), metrics, max(adv, 1e-6) if adv is not None else None)
        clipped += clip
        locs = []                       # (address, contour) per contour
        for c in contours:
            a = curve_pack.reserve(len(c) + 1, contiguous=True)
            locs.append((a, c))
        curves = np.concatenate(contours) if contours else np.zeros((0, 3, 2))
        n_curves += len(curves)
        if len(curves):
            pts = curves.reshape(-1, 2)
            bbox = (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())
            nh, hlists, fh = choose_bands(curves, 1, bbox[1], bbox[3], widen, max_bands)
            nv, vlists, fv = choose_bands(curves, 0, bbox[0], bbox[2], widen, max_bands)
        else:
            bbox = (0.0, 0.0, 1.0, 1.0)
            nh, hlists, fh, nv, vlists, fv = 1, [np.zeros(0, np.int64)], 0, 1, [np.zeros(0, np.int64)], 0
        max_band = max(max_band, fh, fv)
        n_bands += nh + nv
        per_glyph.append((locs, bbox, hlists, vlists))

    # pass two: the band texture, directory first
    band_pack = Packer(2, np.uint16, start=TEX_W)      # row 0 is the directory
    blocks = []
    for locs, bbox, hlists, vlists in per_glyph:
        nh, nv = len(hlists), len(vlists)
        total = nh + nv + sum(len(m) for m in hlists) + sum(len(m) for m in vlists)
        base = band_pack.reserve(nh + nv, contiguous=True)      # headers never wrap
        band_pack.addr = base + total
        blocks.append(base)
    curves_tex = curve_pack.array()
    bands_tex = band_pack.array()

    def texel(a):
        return a & (TEX_W - 1), a >> LOG_W

    for i, (locs, bbox, hlists, vlists) in enumerate(per_glyph):
        code = FIRST + i
        curve_xy = []
        for a, c in locs:
            for k in range(len(c)):
                x, y = texel(a + k)
                curves_tex[y, x, :2] = c[k, 0]
                curves_tex[y, x, 2:] = c[k, 1]
                curve_xy.append((x, y))
            x, y = texel(a + len(c))
            curves_tex[y, x, :2] = c[0, 0]                     # closing texel: p3 of the last curve
        curve_xy = np.array(curve_xy, np.int64).reshape(-1, 2)
        base = blocks[i]
        nh, nv = len(hlists), len(vlists)
        off = nh + nv
        for j, members in enumerate(list(hlists) + list(vlists)):
            x, y = texel(base + j)
            bands_tex[y, x] = (len(members), off)
            for k, m in enumerate(members):
                mx, my = texel(base + off + k)
                bands_tex[my, mx] = curve_xy[m]
            off += len(members)
        bx, by = texel(base)
        q = quantize(bbox)
        bands_tex[0, 4 * code] = (bx, by)
        bands_tex[0, 4 * code + 1] = (nv - 1, nh - 1)          # bandMax: x counts vertical bands, y horizontal
        bands_tex[0, 4 * code + 2] = (q[0], q[1])
        bands_tex[0, 4 * code + 3] = (q[2], q[3])
    for code in list(range(0, FIRST)) + [127]:
        bands_tex[0, 4 * code:4 * code + 4] = bands_tex[0, 4 * FIRST:4 * FIRST + 4]

    metrics = dict(metrics, min_ppl=float(min_ppl), n_curves=int(n_curves), max_band=int(max_band),
                   n_bands=int(n_bands), curve_rows=int(curves_tex.shape[0]), band_rows=int(bands_tex.shape[0]))
    if verbose:
        print(f"{Path(font_path).name}[{index}] {n_glyphs} glyphs, {n_curves} curves in "
              f"{curves_tex.shape[1]}x{curves_tex.shape[0]} RGBA32F ({curves_tex.nbytes / 1024:.0f} KB), "
              f"{n_bands} bands (fullest {max_band} curves, widened by {widen:.4f} em for {min_ppl:g} px per line) in "
              f"{bands_tex.shape[1]}x{bands_tex.shape[0]} RG16UI ({bands_tex.nbytes / 1024:.0f} KB), "
              f"{clipped} control points outside the bbox range, "
              f"char_aspect {metrics['char_aspect']:.4f}, {time.time() - t0:.2f} s")
    return curves_tex, bands_tex, metrics


def save(path, curves, bands, metrics):
    np.savez(path, curves=curves, bands=bands,
             metrics=np.array([metrics["char_aspect"], metrics["ascent"], metrics["descent"], metrics["upem"],
                               metrics["advance"], metrics["min_ppl"]], np.float64),
             params=np.array([metrics["n_curves"], metrics["max_band"], metrics["n_bands"], FIRST, LAST], np.int64))


def load(path):
    """-> (curves, bands, metrics dict)"""
    z = np.load(path)
    m, p = z["metrics"], z["params"]
    metrics = {"char_aspect": float(m[0]), "ascent": float(m[1]), "descent": float(m[2]), "upem": float(m[3]),
               "advance": float(m[4]), "min_ppl": float(m[5]), "n_curves": int(p[0]), "max_band": int(p[1]),
               "n_bands": int(p[2]), "first": int(p[3]), "last": int(p[4])}
    return z["curves"], z["bands"], metrics


def make_textures(curves, bands):
    """Upload the two textures (nearest, no mipmaps) and return their ids;
    bind them to the sampler2D `vt_curves` and the usampler2D `vt_bands`."""
    from OpenGL.GL import (glGenTextures, glBindTexture, glTexParameteri, glTexImage2D, glPixelStorei,
                           GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER, GL_NEAREST,
                           GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE, GL_RGBA32F, GL_RGBA, GL_FLOAT,
                           GL_RG16UI, GL_RG_INTEGER, GL_UNSIGNED_SHORT, GL_UNPACK_ALIGNMENT)
    ids = []
    for buf, internal, fmt, typ in ((np.ascontiguousarray(curves, np.float32), GL_RGBA32F, GL_RGBA, GL_FLOAT),
                                    (np.ascontiguousarray(bands, np.uint16), GL_RG16UI, GL_RG_INTEGER, GL_UNSIGNED_SHORT)):
        tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, tex)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
        h, w = buf.shape[:2]
        glTexImage2D(GL_TEXTURE_2D, 0, internal, w, h, 0, fmt, typ, buf)
        ids.append(tex)
    return ids[0], ids[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--font", default=DEFAULT_FONT, help="TTF/TTC/OTF path (default: the bundled JetBrains Mono NL)")
    ap.add_argument("--index", type=int, default=0, help="face index in a collection")
    ap.add_argument("--min-ppl", type=float, default=12.0,
                    help="the smallest device pixels per line the tier serves; bands are widened for it")
    ap.add_argument("--out", default=None, help="output npz (default data/vt_<font>.npz)")
    args = ap.parse_args()
    out = args.out or os.path.join("data", f"vt_{Path(args.font).stem.lower()}.npz")
    curves, bands, metrics = build_atlas(args.font, args.index, args.min_ppl, verbose=True)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    save(out, curves, bands, metrics)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
