#!/usr/bin/env python3
"""Stage 3: raster glyph atlas for the text rung and the UI text.

Renders the 95 printable ASCII glyphs (32..126) with Pillow into a grid of
equal cells: 16 columns, 6 rows for ASCII plus one extra row of UI symbols
(arrows, the middle dot, the ellipsis) that the viewer's crumb trail and
status line use. Each cell is `cell` pixels tall (one line pitch) and
round(cell * A) wide, where A = advance / (ascent + descent) is the
character aspect the layout stage also uses.

The line box is the font's OS/2 typographic ascent + descent, widened if
any ASCII glyph pokes out of it (Menlo's braces do), so nothing is clipped.
The baseline sits at ascent from the top of the cell.

    ./atlas_font.py --self-test atlas.png      # writes the PNG, prints metrics
    build_atlas(font_path, index, cell) -> (np.uint8[h, w], metrics)
"""
import argparse
import json

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont

DEFAULT_FONT = "/System/Library/Fonts/Menlo.ttc"
COLS, ROWS_ASCII, FIRST, LAST = 16, 6, 32, 126
# row 6 of the atlas: symbols the UI uses that are not ASCII. Cell index
# = 96 + position. Missing glyphs fall back to the ASCII stand-in.
EXTRA = [("←", "<"), ("›", ">"), ("·", "."), ("…", "-"),
         ("→", ">"), ("↑", "^"), ("↓", "v")]
ROWS = ROWS_ASCII + 1


def font_box(font_path, index):
    """(ascent, descent, advance, units_per_em) in font units, the line box
    widened to the real extents of the ASCII glyphs."""
    tt = TTFont(font_path, fontNumber=index)
    upm = tt["head"].unitsPerEm
    if "OS/2" in tt and tt["OS/2"].sTypoAscender > 0:
        asc, desc = tt["OS/2"].sTypoAscender, -tt["OS/2"].sTypoDescender
    else:
        asc, desc = tt["hhea"].ascent, -tt["hhea"].descent
    cmap = tt.getBestCmap()
    gs = tt.getGlyphSet()
    from fontTools.pens.boundsPen import BoundsPen
    adv = tt["hmtx"][cmap.get(ord("M"), ".notdef")][0]
    for c in range(FIRST, LAST + 1):
        g = cmap.get(c)
        if g is None:
            continue
        pen = BoundsPen(gs)
        gs[g].draw(pen)
        if pen.bounds:
            asc = max(asc, int(np.ceil(pen.bounds[3])))
            desc = max(desc, int(np.ceil(-pen.bounds[1])))
    have = {chr(c) for c in cmap}
    return asc, desc, adv, upm, have


def build_atlas(font_path=DEFAULT_FONT, index=0, cell=64):
    """Rasterise the atlas. Returns (coverage uint8 [h, w], metrics dict)."""
    asc, desc, adv, upm, have = font_box(font_path, index)
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
    metrics = {"cell_w": cell_w, "cell_h": cell, "char_aspect": A,
               "cols": COLS, "rows": ROWS, "first": FIRST,
               "extra": {sym: ROWS_ASCII * COLS + i
                         for i, (sym, _) in enumerate(EXTRA)},
               "font": font_path, "index": index}
    return np.asarray(img, np.uint8).copy(), metrics


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
                    help="TTF/TTC/OTF (Linux: /usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf)")
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
