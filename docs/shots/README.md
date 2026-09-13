# Screenshots

Every image is a framebuffer grab from `atlas_viewer.py` on the development machine (M4 MacBook, a 1470 by 820 point window sized to the screen's work area, 2940 by 1640 device pixel framebuffer, the same in every run), except the three `*_layout.png` files, which are `atlas_layout.py --preview` renders. Three corpora: big-text's own source (the README's default example), the whole emscripten tree (the scale example, cloned next to big-text), and War and Peace (the book).

Regenerate everything with:

```
./atlas_index.py . --out data/big-text_atlas
./atlas_index.py ../emscripten --out data/emscripten_atlas
./book_index.py --gutenberg 2600 --out data/war-and-peace_atlas
./atlas_layout.py data/big-text_atlas --preview docs/shots/big-text_layout.png
./atlas_layout.py data/emscripten_atlas --preview docs/shots/emscripten_layout.png
./atlas_layout.py data/war-and-peace_atlas --preview docs/shots/war-and-peace_layout.png
./atlas_viewer.py data/big-text_atlas --shots docs/shots/big-text --filter Viewer --stats
./atlas_viewer.py data/emscripten_atlas --shots docs/shots/emscripten --filter EM_JS --stats
./atlas_viewer.py data/war-and-peace_atlas --shots docs/shots/war-and-peace --filter Natásha --stats
./atlas_viewer.py data/big-text_atlas --proj 3d --heights --tilt 55 --yaw 12 --tint --frames 1 --no-hover --screenshot docs/shots/big-text/hero.png
./atlas_viewer.py data/big-text_atlas --proj 3d --heights --tilt 55 --yaw 12 --hover atlas_index.py:169:5 --frames 2 --screenshot docs/shots/big-text/3d_text.png
./atlas_viewer.py data/big-text_atlas --goto README.md:62 --zoom 14 --frames 2 --screenshot docs/shots/big-text/wrapped.png
./atlas_viewer.py data/big-text_atlas --goto atlas_viewer.py:266 --zoom 120 --frames 3 --screenshot docs/shots/big-text/text_120_raster.png --no-vector-text
./atlas_viewer.py data/big-text_atlas --goto atlas_viewer.py:266 --zoom 120 --frames 3 --screenshot docs/shots/big-text/text_120_vector.png
./atlas_viewer.py data/emscripten_atlas --proj 3d --heights --tilt 55 --yaw 12 --tint --goto tools/link.py:1500 --zoom 2.2 --frames 2 --screenshot docs/shots/emscripten/hero.png
./atlas_viewer.py data/emscripten_atlas --proj 3d --heights --tilt 55 --yaw 12 --hover emcc.py:199:5 --frames 2 --screenshot docs/shots/emscripten/3d_text.png
./atlas_viewer.py data/war-and-peace_atlas --proj 3d --tilt 55 --yaw 12 --goto "p. 481:1" --zoom 3 --frames 2 --no-hover --screenshot docs/shots/war-and-peace/hero.png
./atlas_viewer.py data/war-and-peace_atlas --goto "p. 21:22" --zoom 11 --frames 2 --no-hover --screenshot docs/shots/war-and-peace/opening.png
```

The line numbers in the `--goto` and `--hover` commands are the ones that held when the images were taken (`class Viewer`, `def tokenize`, the README's longest line, emcc's `def main`); they move as the files are edited.

"N px per line" below means N device pixels per line for the file under the view centre; files with a larger or smaller pitch sit on other LODs in the same frame, which is the per-file level of detail at work.

## The standard set

`--shots DIR` writes the same nine images for any corpus:

- `overview.png`: the map fitted to the window.
- `bars.png`, `tokens.png`, `text.png`: 2, 4.5 and 16 px per line about the corpus centre: the line-bars LOD, the tokens LOD with item outlines, the text LOD.
- `hover.png`: the text view with the cursor on the file nearest the window centre: the hovered file's light border and label.
- `filter.png`: the fitted map with the `--filter` word applied: every hit file outlined in yellow, the rest dimmed, the panel listing the hits by file, the status line counting them.
- `result.png`: after stepping to the first hit: the fly-to landed at the text LOD with the hit filled yellow, the file's 3 px border, the row highlighted in the panel.
- `3d.png`, `3d_zoom.png`: the 3D projection with the heights on, at tilt 55 and yaw 12, fitted and at 3 px per line about the centre.
- `<label>.png`, when the corpus has a tint (`churn.png` for code): the fitted map with the tint on.

## The README's opening images

- `big-text/hero.png`: big-text's own source fitted in 3D at tilt 55 and yaw 12 with the heights and the tint on: the most edited files ember, the roofs at the LODs their sizes allow.
- `war-and-peace/hero.png`: War and Peace in 3D at tilt 55 and yaw 12, 3 px per line about page 481 (Book Five): one flat grid of pages in perspective (a book has no heights), the near pages at the text LOD, the far rows at bars. The README's second image.
- `emscripten/hero.png`: the same on emscripten at 2.2 px per line about `tools/link.py` with the tint on: the near roofs at the tokens and text LODs, the far ones at bars. Not in the README.

## Layout previews

- `big-text_layout.png`: the layout stage's own render of big-text (29 files, about 7,200 lines): the `docs/`, `tests/` and `shaders/` bands, files as 1 px outlines wrapped into columns, one grey bar per line from indent to length.
- `emscripten_layout.png`: the same for the whole emscripten tree (11,495 files, 2.80 million lines, 698 directories): nested directory bands in the top-level hue, `test/` and `system/` the largest, big files wrapped into up to 64 columns, tiny files as slivers.
- `war-and-peace_layout.png`: the book: one grid of 1,467 pages in reading order, 58 across, in gutters, one grey bar per line, a chapter's last page short.

## big-text (its own source: 29 files, about 7,200 lines)

The default example, so the counts move whenever this documentation is edited. The standard set with `--filter Viewer` (14 hits in 5 files), plus:

- `big-text/3d_text.png`: `--proj 3d --heights --hover atlas_index.py:169:5`: text zoom on a roof in perspective with the hover label `atlas_index.py:169 def tokenize`.
- `big-text/wrapped.png`: `--goto README.md:62 --zoom 14`: this README's longest paragraph wrapped inside its column, continuation rows hanging in by two characters.
- `big-text/text_120_raster.png`: `class Viewer` at 120 px per line with `--no-vector-text`, drawn from the 64 px raster atlas: the magnified glyphs are visibly soft.
- `big-text/text_120_vector.png`: the same view with the default vector tier: the glyphs are crisp at any magnification.
- `big-text/churn.png`: the tint: every file's fill ember by the log of its lines added and removed over the repository's history; a young repository, so every file carries some.

## emscripten (11,495 files, 2,800,173 lines, 91.7 million chars)

The whole tree as git tracks it, test suites and system libraries included, at the commit cloned. The standard set with `--filter EM_JS` (126 hits in 53 files, grouped by file), plus `3d_text.png` (`--proj 3d --heights --hover emcc.py:199:5`, text zoom on a roof in perspective with the hover label `emcc.py:199 def main`) and `churn.png` (the tint over 30,274 commits: every file's fill ember by the log of its lines added and removed, the vendored test suites under `test/third_party` the brightest, having landed whole).

## War and Peace (Project Gutenberg 2600: 18 parts, 366 chapters, 1,468 Letter pages of 40 reflowed lines, 63,124 lines, 2.67 million glyphs)

In the book's own scheme, Dobbie's: white pages and black text on his demo's blue-grey ground, the text in Literata, justified, with a book's leading, every glyph placed by its own advance, the quotes curly and the accents kept, the pages fading to pale grey as the text gives way to blocks and bars, every page with its running footer. The standard set with `--filter Natásha` (1,213 hits on 310 pages), a book having no tint, plus:

- `war-and-peace/overview.png`: the map fitted: one grid of pages in reading order, the front matter's contents pages first, each page a cell at the line-bars LOD; no part is drawn, the crumb trail names the one under the centre.
- `war-and-peace/hover.png`: the text view with the cursor on the page nearest the centre (the centre itself is a gutter): the label `Book Nine: 1812/Chapter XII/p. 783:20` (part, chapter, page and line; a book has no items).
- `war-and-peace/result.png`: after stepping to the first mention (Book One, Chapter XI, p. 63, line 32), the fly-to at text zoom with the hit filled yellow and the panel listing the hits by page.
- `war-and-peace/3d.png`, `3d_zoom.png`: the grid tilted, nothing extruded: a book's layout is flat whatever the heights setting.
- `war-and-peace/opening.png`: `--goto "p. 21:22" --zoom 11`: the last pages of the contents, then page 21, `BOOK ONE: 1805` and Chapter I.

## Frame times

`--frames 300 --stats --no-hover` runs the scripted zoom from the fitted map to 16 px per line at the corpus centre, through all four LODs, on an M4 MacBook at a 2940 by 1640 framebuffer: big-text 1.4 ms mean, 3.9 ms 99th percentile; War and Peace 2.4 ms mean, 7.6 ms 99th percentile over the shots run; emscripten (279 MB on the GPU) 5.5 ms mean and 25 ms 99th percentile, the tail being the fitted view where every line is visible, and held at 16 px per line (`--zoom 16`) 3.3 ms mean, 6.9 ms 99th percentile.
