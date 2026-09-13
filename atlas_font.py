#!/usr/bin/env python3
"""Stage 3: raster glyph atlas for the text LOD and the UI text.

Renders the 95 printable ASCII glyphs (32..126) with Pillow into a grid of
equal cells: 16 columns, 6 rows for ASCII plus one extra row of UI symbols
(arrows, the middle dot, the ellipsis) that the viewer's crumb trail and
status line use. Each cell is `cell` pixels tall (one line pitch) and
round(cell * A) wide, where A = advance / (ascent + descent) is the
character aspect the layout stage also uses; for a proportional face the
advance is the widest glyph's, every glyph drawn from the cell's left edge,
and the metrics carry each glyph's own advance for the layout and the
shaders.

The line box is the face's ASCII ink extents: the top of the tallest and
the bottom of the deepest glyph among 32..126 (JetBrains Mono's dollar and
at sign, 1.050 em), not the OS/2 box, whose leading differs wildly between
faces (JetBrains Mono's is 1.32 em) and would change every proportion with
the font. The baseline sits at ascent from the top of the cell. Both glyph
tiers use this box.

    ./atlas_font.py --self-test atlas.png      # writes the PNG, prints metrics
    build_atlas(font_path, index, cell) -> (np.uint8[h, w], metrics)
"""
import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont

DEFAULT_FONT = str(Path(__file__).resolve().parent / "fonts" / "JetBrainsMonoNL-Regular.ttf")
COLS, ROWS_ASCII, FIRST, LAST = 16, 6, 32, 126
# row 6 of the atlas: symbols the UI uses that are not ASCII. Cell index
# = 96 + position. Missing glyphs fall back to the ASCII stand-in.
EXTRA = [("←", "<"), ("›", ">"), ("·", "."), ("…", "-"),
         ("→", ">"), ("↑", "^"), ("↓", "v")]
ROWS = ROWS_ASCII + 1


def font_advances(font_path, index=0):
    """The advance of every ASCII glyph 32..126 in font units (the notdef's
    for a missing one), and whether they differ: a proportional face."""
    tt = TTFont(font_path, fontNumber=index)
    cmap = tt.getBestCmap()
    hmtx = tt["hmtx"]
    adv = [hmtx[cmap.get(c, ".notdef")][0] if cmap.get(c, ".notdef") in hmtx.metrics else 0
           for c in range(FIRST, LAST + 1)]
    return adv, len(set(adv)) > 1


def font_box(font_path, index, leading=1.0):
    """(ascent, descent, advance, units_per_em, have) in font units: the line
    box is the ASCII ink extents (the OS/2 or hhea box only when no glyph has
    bounds), stretched by `leading` (a book's air between lines, half above
    and half below), the advance is M's for a monospace face and the widest
    glyph's for a proportional one, `have` the characters the face covers."""
    tt = TTFont(font_path, fontNumber=index)
    upm = tt["head"].unitsPerEm
    cmap = tt.getBestCmap()
    gs = tt.getGlyphSet()
    from fontTools.pens.boundsPen import BoundsPen
    advances, proportional = font_advances(font_path, index)
    adv = max(advances) if proportional else tt["hmtx"][cmap.get(ord("M"), ".notdef")][0]
    top = bottom = None
    for c in range(FIRST, LAST + 1):
        g = cmap.get(c)
        if g is None:
            continue
        pen = BoundsPen(gs)
        gs[g].draw(pen)
        if pen.bounds:
            t, b = int(np.ceil(pen.bounds[3])), int(np.ceil(-pen.bounds[1]))
            top = t if top is None else max(top, t)
            bottom = b if bottom is None else max(bottom, b)
    if top is None or bottom is None or top + bottom <= 0:
        if "OS/2" in tt and tt["OS/2"].sTypoAscender > 0:
            top, bottom = tt["OS/2"].sTypoAscender, -tt["OS/2"].sTypoDescender
        else:
            top, bottom = tt["hhea"].ascent, -tt["hhea"].descent
    have = {chr(c) for c in cmap}
    bottom = max(bottom, 0)
    if leading != 1.0:
        extra = (leading - 1.0) * (top + bottom)
        top, bottom = int(round(top + extra / 2)), int(round(bottom + extra / 2))
    return top, bottom, adv, upm, have


def build_atlas(font_path=DEFAULT_FONT, index=0, cell=64, leading=1.0):
    """Rasterise the atlas. Returns (coverage uint8 [h, w], metrics dict)."""
    asc, desc, adv, upm, have = font_box(font_path, index, leading)
    line = asc + desc
    A = adv / line
    cell_w = int(round(cell * A))
    size = cell * upm / line                     # px per em so that line == cell
    font = ImageFont.truetype(font_path, size=size, index=index)
    baseline = cell * asc / line
    img = Image.new("L", (COLS * cell_w, ROWS * cell), 0)
    draw = ImageDraw.Draw(img)
    for c in range(FIRST, LAST + 1):
        k = c - FIRST
        x, y = (k % COLS) * cell_w, (k // COLS) * cell
        draw.text((x, y + baseline), chr(c), font=font, fill=255, anchor="ls")
    for i, (sym, alt) in enumerate(EXTRA):
        k = ROWS_ASCII * COLS + i
        x, y = (k % COLS) * cell_w, (k // COLS) * cell
        draw.text((x, y + baseline), sym if sym in have else alt,
                  font=font, fill=255, anchor="ls")
    advances, proportional = font_advances(font_path, index)
    metrics = {"cell_w": cell_w, "cell_h": cell, "char_aspect": A,
               "proportional": proportional,
               "advances": [a / line for a in advances],      # per glyph, in line heights
               "leading": leading,
               "cols": COLS, "rows": ROWS, "first": FIRST,
               "extra": {sym: ROWS_ASCII * COLS + i
                         for i, (sym, _) in enumerate(EXTRA)},
               "font": font_path, "index": index}
    return np.asarray(img, np.uint8).copy(), metrics


def mean_ink(font_path, index=0, leading=1.0, freq=None):
    """How dark the face's text is: the mean coverage of a glyph's advance
    box, averaged over the ASCII glyphs weighted by `freq` (95 counts for
    32..126, a corpus's letter frequencies; uniform when None), and the
    fraction of characters that are spaces. A scheme's ink is derived from
    these so bars and blocks carry the mean ink of the text they replace."""
    atlas, m = build_atlas(font_path, index, 64, leading)
    cw, ch, A = m["cell_w"], m["cell_h"], m["char_aspect"]
    freq = np.ones(95) if freq is None else np.asarray(freq, np.float64)
    cov = np.zeros(95)
    for k in range(95):
        x, y = (k % COLS) * cw, (k // COLS) * ch
        w = max(int(round(cw * m["advances"][k] / A)), 1)     # the glyph's advance box inside the cell
        cov[k] = atlas[y:y + ch, x:x + w].mean() / 255.0
    ink = float((cov[1:] * freq[1:]).sum() / max(freq[1:].sum(), 1.0))
    space = float(freq[0] / max(freq.sum(), 1.0))
    return ink, space


def glyph_cell(ch, metrics):
    """Atlas cell index for one character (UI text path)."""
    o = ord(ch)
    if FIRST <= o <= LAST:
        return o - FIRST
    return metrics["extra"].get(ch, ord("?") - FIRST)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--font", default=DEFAULT_FONT,
                    help="TTF/TTC/OTF (default: the bundled JetBrains Mono NL)")
    ap.add_argument("--index", type=int, default=0, help="face index in a TTC")
    ap.add_argument("--cell", type=int, default=64, help="cell height in pixels")
    ap.add_argument("--self-test", metavar="OUT.png", default=None,
                    help="write the atlas as a PNG and print the metrics")
    args = ap.parse_args()
    atlas, metrics = build_atlas(args.font, args.index, args.cell)
    if args.self_test:
        Image.fromarray(atlas).save(args.self_test)
        print(f"atlas {atlas.shape[1]}x{atlas.shape[0]} -> {args.self_test}")
    print(json.dumps(metrics, indent=1))


if __name__ == "__main__":
    main()
