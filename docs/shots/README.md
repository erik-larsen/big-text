# Screenshots

Acceptance evidence for the two reference corpora, produced by the viewer's `--shots` mode on the development machine (M4 MacBook, a 1470 by 820 point window sized to the screen's work area, 2940 by 1640 device pixel framebuffer, the same in every run). The two `*_layout.png` files are `atlas_layout.py --preview` renders, everything else is a framebuffer grab from `atlas_viewer.py`.

Regenerate everything with:

```
./atlas_index.py . --out data/big-text_atlas
./atlas_index.py makepad/draw makepad/platform --out data/makepad-draw_atlas
./atlas_resolve.py data/big-text_atlas
./atlas_resolve.py data/makepad-draw_atlas
./atlas_history.py data/big-text_atlas
./atlas_history.py data/makepad-draw_atlas
./atlas_layout.py data/big-text_atlas --preview docs/shots/big-text_layout.png   # writes every metric's layout and the layers layout
./atlas_layout.py data/makepad-draw_atlas --preview docs/shots/makepad-draw_layout.png
./atlas_viewer.py data/big-text_atlas --shots docs/shots/big-text --filter Viewer --stats
./atlas_viewer.py data/makepad-draw_atlas --shots docs/shots/makepad-draw --filter Window --stats    # also churn, age, changes, history
./atlas_viewer.py data/makepad-draw_atlas --proj 3d --tilt 55 --yaw 12 --hover platform/src/os/linux/x11/x11_sys.rs:8:12 --frames 2 --screenshot docs/shots/makepad-draw/3d_text.png
./atlas_viewer.py data/makepad-draw_atlas --inspector --frames 1 --screenshot docs/shots/makepad-draw/coverage.png
./atlas_viewer.py data/makepad-draw_atlas --inspect Window --frames 2 --screenshot docs/shots/makepad-draw/inspector.png
./atlas_viewer.py data/makepad-draw_atlas --hover platform/src/os/linux/x11/x11_sys.rs:8:12 --frames 2 --screenshot docs/shots/makepad-draw/hover_symbol.png
./atlas_viewer.py data/makepad-draw_atlas --compare '~100' --history --select platform/src/os/linux/vulkan.rs --frames 1 --screenshot docs/shots/makepad-draw/history_file.png
./atlas_viewer.py data/big-text_atlas --goto README.md:219 --zoom 14 --frames 2 --screenshot docs/shots/big-text/wrapped.png
./atlas_viewer.py data/big-text_atlas --goto atlas_viewer.py:399 --zoom 120 --frames 3 --screenshot docs/shots/big-text/text_120_raster.png --no-vector-text
./atlas_viewer.py data/big-text_atlas --goto atlas_viewer.py:399 --zoom 120 --frames 3 --screenshot docs/shots/big-text/text_120_vector.png
./atlas_viewer.py data/makepad-draw_atlas --proj 3d --tilt 55 --yaw 12 --color churn --history --goto platform/src/window.rs:1173 --zoom 2.2 --frames 2 --screenshot docs/shots/makepad-draw/hero.png
./atlas_viewer.py data/big-text_atlas --proj 3d --tilt 55 --yaw 12 --color churn --history --frames 1 --no-hover --screenshot docs/shots/big-text/hero.png
./atlas_viewer.py data/big-text_atlas --proj 3d --tilt 55 --yaw 12 --hover atlas_resolve.py:71:5 --frames 2 --screenshot docs/shots/big-text/3d_text.png
./atlas_viewer.py data/big-text_atlas --hover atlas_viewer.py:399:7 --frames 2 --screenshot docs/shots/big-text/hover_symbol.png
./atlas_viewer.py data/big-text_atlas --inspect Viewer --frames 2 --screenshot docs/shots/big-text/inspector.png
./atlas_viewer.py data/big-text_atlas --inspector --frames 1 --screenshot docs/shots/big-text/coverage.png
./atlas_viewer.py data/big-text_atlas --compare '~22' --history --select atlas_viewer.py --frames 1 --screenshot docs/shots/big-text/history_file.png
./book_index.py --gutenberg 2600 --out data/war-and-peace_atlas
./atlas_layout.py data/war-and-peace_atlas --preview docs/shots/war-and-peace_layout.png
./atlas_viewer.py data/war-and-peace_atlas --shots docs/shots/war-and-peace --filter Natasha --stats
./atlas_viewer.py data/war-and-peace_atlas --proj 3d --tilt 55 --yaw 12 --goto "p. 481:1" --zoom 3 --frames 2 --no-hover --screenshot docs/shots/war-and-peace/hero.png
./atlas_viewer.py data/war-and-peace_atlas --goto "p. 17:1" --zoom 11 --frames 2 --no-hover --screenshot docs/shots/war-and-peace/opening.png
```

"N px per line" below means N device pixels per line for the file under the view centre (`atlas_resolve.py` in big-text, `platform/src/draw_shader.rs` in makepad-draw); files with a larger or smaller pitch sit on other rungs in the same frame, which is the per-file ladder at work.

## The README's opening image

- `big-text/hero.png`: big-text's own source fitted in 3D at tilt 55 and yaw 12 with the churn lens on and the revision rail shown: the most edited files ember, the roofs at the rungs their sizes allow.
- `makepad-draw/hero.png`: the same on makepad-draw at 2.2 px per line about `platform/src/window.rs`: the near roofs at the tokens and text rungs, the far ones at bars.
- `war-and-peace/hero.png`: War and Peace in 3D at tilt 55 and yaw 12, 3 px per line about page 481 (Book Five): the near pages at the text rung, the far rows at bars, the Books as terraces with their labels stacked along the left edge. The README's second image.

## Layout previews

- `big-text_layout.png`: the layout stage's own render of big-text (33 files, about 9,500 lines): the `docs/`, `tests/`, `shaders/` and `notes/` bands, files as 1 px outlines wrapped into columns, one grey bar per line from indent to length.
- `war-and-peace_layout.png`: the book: the front matter, then every Book a full-width band of whole page rows, 65 pages across, one grey bar per line, a chapter's last page short.
- `makepad-draw_layout.png`: the same for makepad/draw plus makepad/platform (493 files, 280,607 lines, 53 directories): nested directory bands in the top-level hue, big files wrapped into up to 14 columns, tiny files as slivers.

## big-text (its own source: 33 files, about 9,500 lines, 23 commits)

The default example, so the counts move by a few lines whenever this documentation is edited.

- `big-text/overview.png`: the map fitted to the window: the root-level scripts as bordered rectangles wrapped into columns, the `docs/`, `tests/`, `shaders/` and `notes/` bands with their labels; at this size the files sit on the line-bars rung.
- `big-text/bars.png`: 2 px per line: the grey line-bars rung.
- `big-text/tokens.png`: 4.5 px per line: one coloured block per token (keywords blue, types yellow, strings pink, comments green) and item outlines in the item-kind colour.
- `big-text/text.png`: 16 px per line: legible syntax-coloured JetBrains Mono text from the vector tier (the default from 12 px per line), function outlines still drawn.
- `big-text/hover.png`: the text view with the cursor at the window centre: the hovered file gets the light fill and the label `atlas_resolve.py:71 def parser_for` (path, line, enclosing item).
- `big-text/filter.png`: the fitted map with the filter `Viewer` applied: the three hit files outlined in yellow, every other file dimmed, the results panel listing 1 definition (`class Viewer` at `atlas_viewer.py:399`, tagged `class`) and 4 references, the status line reading `1 definitions · 4 references · 3 files · 0/5`.
- `big-text/result.png`: after stepping to result 1: the fly-to landed on `class Viewer:` at the text rung with the hit box filled yellow, the file's 3 px yellow border, the current row highlighted in the panel and the counter at `1/5`.
- `big-text/wrapped.png`: `--goto README.md:219 --zoom 14`: this README's longest paragraph wrapped inside its column, continuation rows hanging in by two characters.
- `big-text/text_120_raster.png`: `atlas_viewer.py:399` at 120 px per line with `--no-vector-text`, drawn from the 64 px raster atlas: the magnified glyphs are visibly soft.
- `big-text/text_120_vector.png`: the same view with the default vector tier: the glyphs are crisp at any magnification.
- `big-text/hover_symbol.png`: `--hover atlas_viewer.py:399:7`: the cursor on `Viewer` at its definition, the label `atlas_viewer.py:399 class Viewer · 1 definition · 2 references` (the references the resolver tied to this class).
- `big-text/inspector.png`: the Inspector on `class Viewer` (`--inspect Viewer`): kind, name, path and line, its references grouped by file, each a click away.
- `big-text/coverage.png`: the Inspector with nothing selected (`--inspector`): the resolver's coverage block, one line per resolution status with its count.
- `big-text/references.png`: the map laid out by the references metric: the scripts everything imports grow, the documents shrink.
- `big-text/3d.png`, `3d_zoom.png`: the references layout in the 3D projection at tilt 55 and yaw 12, fitted and at 3 px per line about the centre: the most referenced scripts as the tallest blocks, the ladder on every roof.
- `big-text/3d_text.png`: `--proj 3d --hover atlas_resolve.py:71:5`: text zoom on a roof in perspective with the hover label.
- `big-text/layers.png`, `selection.png`: the Layers lens fitted (two ranks, the 13 mutually importing scripts as one cycle, 13 unreferenced files in their own row), and `atlas_viewer.py` selected: the 7 files it uses and the 10 that use it lit in teal, the Inspector listing them after its history.
- `big-text/churn.png`, `age.png`: the churn and age lenses over the fitted map.
- `big-text/changes.png`: the Changes lens between the first commit and HEAD with the rail shown: 29 files added in green, 3 changed in amber, 2 removed and counted in the status line.
- `big-text/history.png`: the corpus loaded at the first commit, 6 files and 225 lines, the rail tagging it.
- `big-text/history_file.png`: `atlas_viewer.py` selected with the Changes lens on: the Inspector's History block under the selection (commits, dates, lines, newest five commits) before the Uses and Used-by lists.

## makepad-draw (493 files, 280,607 lines, 10,182,050 chars)

- `makepad-draw/overview.png`: the fitted map: `platform/` in green and `draw/` in teal, nested directory bands with stacked labels (`platform/ src/ os/ linux/ android/`), files as bordered tiles at this size, tiny files as slivers.
- `makepad-draw/bars.png`: 2 px per line inside `platform/src`: the line-bars rung with the directory padding drawn as wide green bands.
- `makepad-draw/tokens.png`: 4.5 px per line: the tokens rung with item outlines; neighbouring files with a smaller pitch are still on the bars rung, one with a larger pitch is already text.
- `makepad-draw/text.png`: 16 px per line: legible Rust with struct, enum and impl outlines.
- `makepad-draw/hover.png`: the same view hovered at the centre: label `platform/src/draw_shader.rs:232 enum DrawShaderInputPacking`.
- `makepad-draw/filter.png`: the fitted map with the filter `Window`: 31 hit files outlined in yellow at the overview, the rest dimmed, the panel populated with 1 definition (`pub type Window = XID`) and 147 references grouped by file, the status line `1 definitions · 147 references · 31 files · 0/148`.
- `makepad-draw/references.png`: the map laid out by the references metric (`--shots` takes it after the results; the toolbar or M switches live): the x11 bindings and the script VM grow, the generated code shrinks.
- `makepad-draw/3d.png`: the references layout in the 3D projection at tilt 55 and yaw 12, fitted: the fan-in skyline, directories as terraces, tags on the terrace edges.
- `makepad-draw/3d_zoom.png`: the same at 3 px per line about the centre: text on the near roofs, bars on the far ones, walls occluding.
- `makepad-draw/3d_text.png`: `--proj 3d --hover platform/src/os/linux/x11/x11_sys.rs:8:12`: text zoom on a roof in perspective with the hover label.
- `makepad-draw/layers.png`: the Layers lens fitted: rows by dependency rank, the 355-file cycle grouped, hues from the tree.
- `makepad-draw/selection.png`: the file with the highest fan-in (x11_sys.rs) selected in the Layers lens: its 224 dependants lit in teal, the rest dimmed, the Inspector listing them.
- `makepad-draw/coverage.png`: the Inspector with nothing selected (`--inspector`): the resolver's coverage block, one line per resolution status with its count.
- `makepad-draw/inspector.png`: the Inspector on the enum variant `Window` (`--inspect Window`): kind and name, path and line, the enclosing enum, the definition and reference counts, and the references grouped by file, each a click away.
- `makepad-draw/churn.png`: the fitted map with the churn lens: every file's fill tinted ember by the log of its lines added and removed over the whole history; the linux and web platform code and the generated bindings are the hottest, the status line reads `churn · all history`.
- `makepad-draw/age.png`: the age lens: fill by rank of the last commit, newest teal, the oldest untouched files dark; the status line names the newest commit's date.
- `makepad-draw/changes.png`: the Changes lens between the commit 100 back (`1f6b12f`) and HEAD with the rail shown: added files green, changed files amber by the fraction of lines touched, the two revisions tagged on the rail, the status `changes · 1f6b12f → HEAD · 78 added · 203 changed · 0 removed`, equal to `git diff --name-status` for the two directories.
- `makepad-draw/history.png`: the corpus loaded at `1f6b12f` (415 files, 221,045 lines), the rail tagging it, the status `at 1f6b12f 2026-07-30`; its resolver and other layouts are read from the cache (a first load gets them in the background a second or two after the swap), so the toolbar has everything but the churn metric, which is HEAD's history.
- `makepad-draw/history_file.png`: `vulkan.rs` selected with the Changes lens on: the Inspector's History block under the selection (commits, dates, lines, newest five commits) before the Uses and Used-by lists.
- `makepad-draw/hover_symbol.png`: the cursor held on `Window` in x11_sys.rs at text zoom: the label reads the resolver's view, `type Window · 2 definitions · 102 references`.
- `makepad-draw/result.png`: after stepping to result 1: the fly-to ends on `pub type Window = XID;` at 41 px per line with the hit box, the 3 px file border and the crumb trail `← makepad-draw › platform › src › os › linux › x11`.

## War and Peace (Project Gutenberg 2600: 18 parts, 366 chapters, 1401 pages, 63,191 lines, 2.63 million glyphs)

`--shots` takes the first seven images only: a book has no resolver and no history, so no references layout, Layers lens, lenses or rail.

- `war-and-peace/overview.png`: the map fitted: `Front Matter` on top, then the 15 Books and two epilogues as bands of whole page rows in reading order, each labelled, each page a cell at the line-bars rung, the last row of every Book short.
- `war-and-peace/bars.png`, `tokens.png`, `text.png`: 2, 4.5 and 16 px per line about the centre (Book Eight): pages as blocks of grey bars, then pages of word blocks, then pages of legible prose, the empty right end of a band as a coloured field.
- `war-and-peace/hover.png`: the text view hovered at the centre: the label `Book Eight: 1811 - 12/Chapter IX/p. 657` (part, chapter, page; a book has no items).
- `war-and-peace/filter.png`: the fitted map with the filter `Natasha`: 1,213 mentions on 299 pages, every hit page outlined in yellow, the panel listing them by page, the status line `0 definitions · 1213 references · 299 files · 0/1213`.
- `war-and-peace/result.png`: after stepping to the first mention (Book One, Chapter XI, p. 56, line 41), the fly-to at text zoom with the hit filled yellow.
- `war-and-peace/opening.png`: `--goto "p. 17:1" --zoom 11`: the book's first pages, the contents on the front-matter pages above `Book One: 1805` and Chapter I.

## Frame times

`--frames 300 --stats` runs the scripted zoom from the fitted map to 16 px per line at the corpus centre, through all four rungs, on an M4 MacBook at a 2940 by 1658 framebuffer: big-picture 1.15 ms mean, 3.42 ms 99th percentile; makepad-draw 1.63 ms mean, 4.37 ms 99th percentile (acceptance asks for under 16 ms and under 33 ms). The full makepad tree (`./atlas_index.py makepad --out data/makepad_atlas`, 7145 files, 3.67 million lines, 380 MB on the GPU) gives 4.48 ms mean and 23.66 ms 99th percentile, the tail being the fitted view where every line is visible; held at 16 px per line it runs at 1.31 ms mean, 4.93 ms 99th percentile.
