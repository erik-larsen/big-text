# Screenshots

Acceptance evidence for the two reference corpora, produced by the viewer's `--shots` mode on the development machine (M4 MacBook, a 1470 by 820 point window sized to the screen's work area, 2940 by 1640 device pixel framebuffer, the same in every run). The two `*_layout.png` files are `atlas_layout.py --preview` renders, everything else is a framebuffer grab from `atlas_viewer.py`.

Regenerate everything with:

```
./atlas_index.py big-picture --out data/big-picture_atlas
./atlas_index.py makepad/draw makepad/platform --out data/makepad-draw_atlas
./atlas_resolve.py data/big-picture_atlas
./atlas_resolve.py data/makepad-draw_atlas
./atlas_history.py data/big-picture_atlas
./atlas_history.py data/makepad-draw_atlas
./atlas_layout.py data/big-picture_atlas --preview docs/shots/big-picture_layout.png   # writes the tokens, references and lines layouts
./atlas_layout.py data/makepad-draw_atlas --preview docs/shots/makepad-draw_layout.png
./atlas_viewer.py data/big-picture_atlas --shots docs/shots/big-picture --filter Viewer --stats
./atlas_viewer.py data/makepad-draw_atlas --shots docs/shots/makepad-draw --filter Window --stats    # also churn, age, changes, history
./atlas_viewer.py data/makepad-draw_atlas --proj 3d --tilt 55 --yaw 12 --hover platform/src/os/linux/x11/x11_sys.rs:8:12 --frames 2 --screenshot docs/shots/makepad-draw/3d_text.png
./atlas_viewer.py data/makepad-draw_atlas --inspector --frames 1 --screenshot docs/shots/makepad-draw/coverage.png
./atlas_viewer.py data/makepad-draw_atlas --inspect Window --frames 2 --screenshot docs/shots/makepad-draw/inspector.png
./atlas_viewer.py data/makepad-draw_atlas --hover platform/src/os/linux/x11/x11_sys.rs:8:12 --frames 2 --screenshot docs/shots/makepad-draw/hover_symbol.png
./atlas_viewer.py data/makepad-draw_atlas --compare '~100' --history --select platform/src/os/linux/vulkan.rs --frames 1 --screenshot docs/shots/makepad-draw/history_file.png
./atlas_viewer.py data/big-picture_atlas --goto c/stb_image.h:120 --zoom 14 --frames 2 --screenshot docs/shots/big-picture/wrapped.png
./atlas_viewer.py data/big-picture_atlas --goto vt_viewer.py:569 --zoom 120 --frames 3 --screenshot docs/shots/big-picture/text_120_raster.png --no-vector-text
./atlas_viewer.py data/big-picture_atlas --goto vt_viewer.py:569 --zoom 120 --frames 3 --screenshot docs/shots/big-picture/text_120_vector.png
./tests/bench_vt.py --out /tmp/bench && cp /tmp/bench/stack_12.png docs/shots/vt_bench_12.png && cp /tmp/bench/stack_96.png docs/shots/vt_bench_96.png
```

"N px per line" below means N device pixels per line for the file under the view centre (`c/vt_core.c` in big-picture, `platform/src/draw_shader.rs` in makepad-draw); files with a larger or smaller pitch sit on other rungs in the same frame, which is the per-file ladder at work.

## Layout previews

- `big-picture_layout.png`: the layout stage's own render of big-picture (19 files, 15,216 lines): the `c/` directory band, files as 1 px outlines wrapped into up to 12 columns, one grey bar per line from indent to length.
- `makepad-draw_layout.png`: the same for makepad/draw plus makepad/platform (493 files, 280,607 lines, 53 directories): nested directory bands in the top-level hue, big files wrapped into up to 14 columns, tiny files as slivers.

## big-picture (19 files, 15,216 lines, 555,088 chars)

- `big-picture/overview.png`: the map fitted to the window: the `c/` band and label, root-level files as bordered rectangles, `stb_image.h` wrapped into 12 columns; at this size most files sit on the line-bars rung and the smallest ones already show text.
- `big-picture/bars.png`: 2 px per line: the grey line-bars rung, the circuit-board texture of the video.
- `big-picture/tokens.png`: 4.5 px per line: one coloured block per token (keywords blue, types yellow, strings pink, comments green) and item outlines in the item-kind colour.
- `big-picture/text.png`: 16 px per line: legible syntax-coloured JetBrains Mono text from the vector tier (the default from 12 px per line), function outlines still drawn.
- `big-picture/hover.png`: the text view with the cursor at the window centre: the hovered file gets the light fill and the label `c/vt_core.c:26 function smoothstep01` (path, line, enclosing item).
- `big-picture/filter.png`: the fitted map with the filter `Viewer` applied: the one hit file outlined in yellow, every other file dimmed, the results panel listing 1 definition (`class Viewer`, tagged `class`) and 1 reference, the status line reading `1 definitions · 1 references · 1 files · 0/2`.
- `big-picture/result.png`: after stepping to result 1: the fly-to landed on `class Viewer:` in `vt_viewer.py` at the text rung with the hit box filled yellow, the file's 3 px yellow border, the current row highlighted in the panel and the counter at `1/2`.
- `big-picture/wrapped.png`: `--goto c/stb_image.h:120 --zoom 14`: the credits block's long lines wrapped inside their column, continuation rows hanging in by two characters.
- `big-picture/text_120_raster.png`: `vt_viewer.py:569` at 120 px per line with `--no-vector-text`, drawn from the 64 px raster atlas: the magnified glyphs are visibly soft.
- `big-picture/text_120_vector.png`: the same view with the default vector tier: the glyphs are crisp at any magnification.
- `vt_bench_12.png`, `vt_bench_96.png`: `tests/bench_vt.py --out` evidence from phase 6: the same string at 12 and 96 px per line rendered by the Slug tier, the old grid tier (taken from git before the history rewrite removed it), the raster tier and the exact coverage, stacked in that order and enlarged; a rerun today writes three rows, without the grid.

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
- `big-picture/churn.png`, `age.png`, `changes.png`, `history.png`: the same four for big-picture's own short history (`--shots` writes them for any atlas with `history.npz`).
- `makepad-draw/history_file.png`: `vulkan.rs` selected with the Changes lens on: the Inspector's History block under the selection (commits, dates, lines, newest five commits) before the Uses and Used-by lists.
- `makepad-draw/hover_symbol.png`: the cursor held on `Window` in x11_sys.rs at text zoom: the label reads the resolver's view, `type Window · 2 definitions · 102 references`.
- `makepad-draw/result.png`: after stepping to result 1: the fly-to ends on `pub type Window = XID;` at 41 px per line with the hit box, the 3 px file border and the crumb trail `← makepad-draw › platform › src › os › linux › x11`.

## Frame times

`--frames 300 --stats` runs the scripted zoom from the fitted map to 16 px per line at the corpus centre, through all four rungs, on an M4 MacBook at a 2940 by 1658 framebuffer: big-picture 1.15 ms mean, 3.42 ms 99th percentile; makepad-draw 1.63 ms mean, 4.37 ms 99th percentile (acceptance asks for under 16 ms and under 33 ms). The full makepad tree (`./atlas_index.py makepad --out data/makepad_atlas`, 7145 files, 3.67 million lines, 380 MB on the GPU) gives 4.48 ms mean and 23.66 ms 99th percentile, the tail being the fitted view where every line is visible; held at 16 px per line it runs at 1.31 ms mean, 4.93 ms 99th percentile.
