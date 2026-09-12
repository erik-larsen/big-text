# Screenshots

Acceptance evidence for the two reference corpora, produced by the viewer's `--shots` mode on the development machine (M4 MacBook, 1600 by 1000 point window, 2940 by 1658 device pixel framebuffer). The two `*_layout.png` files are `atlas_layout.py --preview` renders, everything else is a framebuffer grab from `atlas_viewer.py`.

Regenerate everything with:

```
./atlas_index.py big-picture --out data/big-picture_atlas
./atlas_index.py makepad/draw makepad/platform --out data/makepad-draw_atlas
./atlas_resolve.py data/big-picture_atlas
./atlas_resolve.py data/makepad-draw_atlas
./atlas_layout.py data/big-picture_atlas --preview docs/shots/big-picture_layout.png
./atlas_layout.py data/makepad-draw_atlas --preview docs/shots/makepad-draw_layout.png
./atlas_viewer.py data/big-picture_atlas --shots docs/shots/big-picture --filter Viewer --stats
./atlas_viewer.py data/makepad-draw_atlas --shots docs/shots/makepad-draw --filter Window --stats
./atlas_viewer.py data/makepad-draw_atlas --inspector --frames 1 --screenshot docs/shots/makepad-draw/coverage.png
./atlas_viewer.py data/makepad-draw_atlas --inspect Window --frames 2 --screenshot docs/shots/makepad-draw/inspector.png
./atlas_viewer.py data/makepad-draw_atlas --hover platform/src/os/linux/x11/x11_sys.rs:8:12 --frames 2 --screenshot docs/shots/makepad-draw/hover_symbol.png
./atlas_viewer.py data/big-picture_atlas --goto vt_viewer.py:569 --zoom 120 --frames 3 --screenshot docs/shots/big-picture/text_120_raster.png
./atlas_viewer.py data/big-picture_atlas --goto vt_viewer.py:569 --zoom 120 --frames 3 --screenshot docs/shots/big-picture/text_120_vector.png --vector-text
```

"N px per line" below means N device pixels per line for the file under the view centre (`c/vt_core.c` in big-picture, `platform/src/draw_shader.rs` in makepad-draw); files with a larger or smaller pitch sit on other rungs in the same frame, which is the per-file ladder at work.

## Layout previews

- `big-picture_layout.png`: the layout stage's own render of big-picture (19 files, 15,216 lines): the `c/` directory band, files as 1 px outlines wrapped into up to 12 columns, one grey bar per line from indent to length.
- `makepad-draw_layout.png`: the same for makepad/draw plus makepad/platform (493 files, 280,607 lines, 53 directories): nested directory bands in the top-level hue, big files wrapped into up to 14 columns, tiny files as slivers.

## big-picture (19 files, 15,216 lines, 555,088 chars)

- `big-picture/overview.png`: the map fitted to the window: the `c/` band and label, root-level files as bordered rectangles, `stb_image.h` wrapped into 12 columns; at this size most files sit on the line-bars rung and the smallest ones already show text.
- `big-picture/bars.png`: 2 px per line: the grey line-bars rung, the circuit-board texture of the video.
- `big-picture/tokens.png`: 4.5 px per line: one coloured block per token (keywords blue, types yellow, strings pink, comments green) and item outlines in the item-kind colour.
- `big-picture/text.png`: 16 px per line: legible syntax-coloured Menlo text from the raster glyph atlas, function outlines still drawn.
- `big-picture/hover.png`: the text view with the cursor at the window centre: the hovered file gets the light fill and the label `c/vt_core.c:26 function smoothstep01` (path, line, enclosing item).
- `big-picture/filter.png`: the fitted map with the filter `Viewer` applied: the one hit file outlined in yellow, every other file dimmed, the results panel listing 1 definition (`class Viewer`, tagged `class`) and 1 reference, the status line reading `1 definitions · 1 references · 1 files · 0/2`.
- `big-picture/result.png`: after stepping to result 1: the fly-to landed on `class Viewer:` in `vt_viewer.py` at the text rung with the hit box filled yellow, the file's 3 px yellow border, the current row highlighted in the panel and the counter at `1/2`.
- `big-picture/text_120_raster.png`: `vt_viewer.py:569` at 120 px per line drawn from the 64 px raster atlas: the magnified glyphs are visibly soft.
- `big-picture/text_120_vector.png`: the same view with `--vector-text`: above 40 px per line the glyphs come from the Dobbie vector-texture tier and are crisp at any magnification.

## makepad-draw (493 files, 280,607 lines, 10,182,050 chars)

- `makepad-draw/overview.png`: the fitted map: `platform/` in green and `draw/` in teal, nested directory bands with stacked labels (`platform/ src/ os/ linux/ android/`), files as bordered tiles at this size, tiny files as slivers.
- `makepad-draw/bars.png`: 2 px per line inside `platform/src`: the line-bars rung with the directory padding drawn as wide green bands.
- `makepad-draw/tokens.png`: 4.5 px per line: the tokens rung with item outlines; neighbouring files with a smaller pitch are still on the bars rung, one with a larger pitch is already text.
- `makepad-draw/text.png`: 16 px per line: legible Rust with struct, enum and impl outlines.
- `makepad-draw/hover.png`: the same view hovered at the centre: label `platform/src/draw_shader.rs:232 enum DrawShaderInputPacking`.
- `makepad-draw/filter.png`: the fitted map with the filter `Window`: 31 hit files outlined in yellow at the overview, the rest dimmed, the panel populated with 1 definition (`pub type Window = XID`) and 147 references grouped by file, the status line `1 definitions · 147 references · 31 files · 0/148`.
- `makepad-draw/coverage.png`: the Inspector with nothing selected (`--inspector`): the resolver's coverage block, one line per resolution status with its count.
- `makepad-draw/inspector.png`: the Inspector on the enum variant `Window` (`--inspect Window`): kind and name, path and line, the enclosing enum, the definition and reference counts, and the references grouped by file, each a click away.
- `makepad-draw/hover_symbol.png`: the cursor held on `Window` in x11_sys.rs at text zoom: the label reads the resolver's view, `type Window · 2 definitions · 102 references`.
- `makepad-draw/result.png`: after stepping to result 1: the fly-to ends on `pub type Window = XID;` at 41 px per line with the hit box, the 3 px file border and the crumb trail `← makepad-draw › platform › src › os › linux › x11`.

## Frame times

`--frames 300 --stats` runs the scripted zoom from the fitted map to 16 px per line at the corpus centre, through all four rungs, on an M4 MacBook at a 2940 by 1658 framebuffer: big-picture 1.15 ms mean, 3.42 ms 99th percentile; makepad-draw 1.63 ms mean, 4.37 ms 99th percentile (acceptance asks for under 16 ms and under 33 ms). The full makepad tree (`./atlas_index.py makepad --out data/makepad_atlas`, 7145 files, 3.67 million lines, 380 MB on the GPU) gives 4.48 ms mean and 23.66 ms 99th percentile, the tail being the fitted view where every line is visible; held at 16 px per line it runs at 1.31 ms mean, 4.93 ms 99th percentile.
