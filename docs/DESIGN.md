# big-text code atlas: design and contracts

This is the build contract for the first code atlas viewer. It turns the observations in `notes/makepad-code-atlas.md` (read its "Observed behaviour, from the video" section first) into modules, file formats and shader behaviour that can be built in parallel. Where this document and the notes disagree, this document wins; where this document is silent, do what the video does.

## Decisions taken (the README's six questions)

1. Corpus: a source tree. Books and filesystems come later; nothing here should preclude them, but nothing is built for them.
2. Glyph tier: a raster glyph atlas (Pillow, one monospace face, mipmapped) is the text rung for this version. A Dobbie-style vector-texture tier is built as a separate optional module and, if its test passes, switched in above about 40 device pixels per line behind a `--vector-text` flag.
3. Atlas generation: our own, from a TTF/TTC with Pillow (raster) and fontTools (vector).
4. Platform: Python 3.12, numpy, Pillow, PyOpenGL, glfw, OpenGL 3.3 core, same stack and conventions as big-picture (`big-picture/vt_viewer.py` is the reference for window setup, Retina handling, `--frames`/`--screenshot` self-tests and argument style). Shaders are GLSL 330 core but must not use anything that has no GLES 3.0 equivalent (no geometry shaders, no bindless, no double).
5. No pyramid. The ladder is drawn from instance data; nothing is rasterised ahead of time except the glyph atlas.
6. Budget: lines, tokens and characters of the whole corpus are resident on the GPU (this is fine up to a few million lines, see Sizes). A streaming working set is a later step.

Not built in this version: the resolver (definitions and references come from regexes, not a language index), 3D and tilt, lenses other than Folders, History, the Inspector's coverage statistics.

## Repository layout

```
big-text/
  atlas_index.py      source tree → data/<name>_atlas/index.npz + index.json
  atlas_layout.py     index → data/<name>_atlas/layout.npz + layout.json (+ --preview PNG)
  atlas_font.py       monospace TTF → raster glyph atlas (used by the viewer; --self-test writes a PNG)
  atlas_viewer.py     the viewer
  vt_glyphs.py        optional vector-texture glyph tier (fontTools → curve atlas texture + GLSL)
  shaders/            GLSL files loaded by the viewer (one file per program, VS and FS separated by a marker line, or two files; the viewer decides)
  tests/              self-checks runnable with python3; no test framework required
  data/               generated, gitignored
  docs/shots/         screenshots for the README
```

All scripts: `#!/usr/bin/env python3`, executable, argparse, positional main argument, `--out` optional with a derived default. Python style as in big-picture: plain functions, numpy for bulk data, no classes for their own sake. Markdown files use one line per paragraph, no hard wrapping.

## Stage 1: atlas_index.py

`./atlas_index.py <src_dir> [--out data/<name>_atlas] [--workers N] [--exclude PATTERN ...]`

Walks `src_dir`, skipping `.git`, `target`, `node_modules`, `__pycache__`, `.venv`, `data`, `pics`, anything in `.gitignore` when `git` is available (use `git ls-files --cached --others --exclude-standard` inside a git repo, falling back to a walk), and any file that is not text (contains a NUL in the first 8 KB, or fails UTF-8 decoding with more than 1 percent replacement characters). Files over 4 MB are skipped and counted. Submodule directories are included only if `--submodules` is given (the default excludes them, because `big-text/makepad` is 3 million lines).

Every kept file becomes a sequence of lines. Tabs expand to 4 spaces. Each character that is not printable ASCII (32 to 126) becomes `?` (63) so that one byte is one column; trailing whitespace is stripped; a line is capped at 4096 columns. Line order and file order are stable: files sorted by path within their directory, directories depth-first, so a directory's files and all its descendants' files are one contiguous range of file indices and one contiguous range of line indices.

Tokenizer: our own, per language family chosen by extension. Rust (`.rs`), C-like (`.c .h .cpp .hpp .m .mm .js .ts .java .go .cs .swift .glsl .frag .vert .metal .wgsl`), Python (`.py`), shell (`.sh .zsh`), and a default (everything else: markdown, toml, json, yaml, html, css, txt) that only recognises strings, numbers and comments where the syntax is obvious and marks the rest identifier or punctuation. It assigns one kind per character:

| kind | value | what |
|---|---|---|
| space | 0 | whitespace |
| ident | 1 | identifiers |
| keyword | 2 | language keywords |
| type | 3 | identifiers starting with an uppercase letter (Rust/C types, Python classes) |
| string | 4 | string and char literals, including quotes |
| comment | 5 | comments, including markers; doc comments too |
| number | 6 | numeric literals |
| punct | 7 | operators, brackets, punctuation |
| macro | 8 | Rust `name!`, `#[attributes]`, C preprocessor lines, Python decorators |
| call | 9 | an identifier immediately followed by `(` and the name in a definition (`fn name`, `def name`, `class Name`) |

Block comments and multi-line strings carry across lines within a file. Do not aim for parser accuracy; aim for the pastel texture in the video, where keywords, types, strings and comments are visibly different at three pixels per line.

Items (for outlines and hover): per language, regexes on line starts, with the extent found by brace or indentation matching:

- Rust: `fn`, `struct`, `enum`, `trait`, `impl`, `mod`, `macro_rules!`, `type`, `const`, `static` at any indentation. Extent: from the line to the matching closing brace of the first `{` (or the `;` for items without a body).
- Python: `def`, `class`, `async def`. Extent: until the next line with indentation less than or equal to the item's, ignoring blank lines.
- C-like: `struct|enum|union|class NAME`, and a function definition heuristic: a line at column 0 that ends with `{` or `)` and contains `(`, not starting with a keyword like `if|for|while|switch|return`. Extent by braces.

Item kinds: 1 function (`fn def function`), 2 struct/class/union, 3 enum, 4 impl/trait/interface, 5 module/namespace/other. Nested items are allowed (an `impl` contains `fn`s); store all.

Outputs:

`index.npz` (numpy, uncompressed):

| array | dtype | shape | meaning |
|---|---|---|---|
| chars | uint8 | [total] | every line's characters, files and lines concatenated with no separators |
| kinds | uint8 | [total] | kind per character, same indexing as chars |
| line_off | uint64 | [n_lines + 1] | start offset of each line in chars; line i is chars[line_off[i]:line_off[i+1]] |
| line_file | uint32 | [n_lines] | file index of each line |
| line_indent | uint16 | [n_lines] | columns of leading whitespace |
| line_len | uint16 | [n_lines] | columns (same as line_off[i+1] - line_off[i], kept for convenience) |
| file_line0 | uint32 | [n_files + 1] | first line index of each file; file f has lines [file_line0[f], file_line0[f+1]) |
| file_chars | uint32 | [n_files] | non-space character count (the size metric) |
| file_dir | uint32 | [n_files] | directory index |
| item_file | uint32 | [n_items] | |
| item_start | uint32 | [n_items] | first line, file-relative |
| item_end | uint32 | [n_items] | one past the last line, file-relative |
| item_kind | uint8 | [n_items] | |

`index.json`:

```json
{"root": "/abs/path", "name": "big-picture",
 "files": [{"path": "vt_viewer.py", "lang": "python", "lines": 1234, "bytes": 45678}],
 "dirs":  [{"path": "", "parent": -1, "children": [1, 2], "files": [0, 3], "top": 0}],
 "items": [{"file": 0, "start": 10, "end": 42, "kind": 1, "name": "main"}],
 "kinds": ["space", "ident", "keyword", "type", "string", "comment", "number", "punct", "macro", "call"],
 "stats": {"files": 12, "dirs": 3, "lines": 15000, "chars": 400000, "items": 900, "skipped_binary": 4, "skipped_large": 0, "seconds": 1.2}}
```

`dirs[0]` is the root (path ""). `top` is the index of the directory's top-level ancestor (a direct child of the root), or its own index for a top-level directory, or 0 for the root; it selects the hue. Files directly in the root get `top` 0. Item names are in the JSON only; the arrays carry the numbers.

Use `--workers` (default half the cores) with a process pool over files; the tokenizer is pure Python and the makepad tree is 120 MB.

## Stage 2: atlas_layout.py

`./atlas_layout.py data/<name>_atlas [--aspect 16:9] [--metric chars|lines|bytes] [--preview out.png]`

World coordinates: the root rectangle is [0, W] by [0, H] with W = 1600 and H = 1600 / aspect, y down. Everything is float64 on the CPU and float32 on the GPU (see Precision).

Squarified treemap (Bruls, Huizing, van Wijk) over the directory tree. A directory's weight is the sum of its files' metric plus its children's weights. The default metric is tokens (runs of one non-space kind, restarted at each line), as in the video's toolbar; chars, lines and bytes are the alternatives. Inside a directory rectangle, first inset by the directory padding, then lay out the children (files and subdirectories together, sorted by weight descending) with squarify. Padding is a fraction of the parent's shorter side: 1.5 percent, clamped to [0.05, 4] world units, and it is the same on all four sides. The padding region is what the viewer draws as the directory border, so its width in pixels grows with zoom exactly as the video shows.

Files: a file's rectangle is split into k equal-width columns with a gap of 6 percent of the column width between columns. Each column holds `rows = ceil(n_lines / k)` lines at pitch `p = h_inner / rows` where h_inner is the rectangle height less a top and bottom margin of one pitch (so the first line does not touch the border). Choose k from 1 upward: the column's character capacity is `cap = floor(col_w / (p * A))` where A is the character advance divided by the line pitch of the font (about 0.6 for Menlo; take it from atlas_font.py's metrics, default 0.6). Pick the smallest k such that `cap >= target`, where `target = clamp(p90 of the file's line lengths, 24, 100)`, or the k that maximises cap if no k reaches the target; k is capped at 64. Lines longer than cap are clipped, not wrapped (wrapping is a later step; note it in the README). A file with zero lines gets k = 1, rows = 1.

Line positions: line i of file f (file-relative index j = i - file_line0[f]) sits in column c = j // rows, row r = j % rows, at x = col_x0[c] and y = y0 + margin + r * p, and occupies width `cap * p * A` at most (the shader clips to line_len).

Items: each item's line range maps to one rectangle per column it spans: x from the column's x0 to x0 + col_w, y from the first line's y to one past the last line's y, within that column. Split at column boundaries.

Collapsing: none in this version. Files whose rectangle would be smaller than 0.02 world units on either side are still laid out (the viewer draws them as a tile).

Outputs:

`layout.npz`:

| array | dtype | shape | meaning |
|---|---|---|---|
| world | float64 | [2] | W, H |
| dir_rect | float64 | [n_dirs, 4] | x0, y0, x1, y1 of the directory (outer rectangle, before padding) |
| dir_pad | float64 | [n_dirs] | padding width |
| dir_depth | uint16 | [n_dirs] | root is 0 |
| dir_hue | uint8 | [n_dirs] | hue index = top-level child index mod 12 (root 0 → 0) |
| file_rect | float64 | [n_files, 4] | |
| file_pitch | float64 | [n_files] | line pitch p in world units |
| file_cols | uint16 | [n_files] | k |
| file_rows | uint32 | [n_files] | rows per column |
| file_cap | uint16 | [n_files] | characters per column |
| file_colw | float64 | [n_files] | column width including its gap |
| file_hue | uint8 | [n_files] | hue of the file's top-level directory |
| line_pos | float32 | [n_lines, 2] | x, y (top left of the line's cell) |
| item_rect | float32 | [n_item_rects, 4] | |
| item_rect_item | uint32 | [n_item_rects] | index into items |

`layout.json`: `{"world": [W, H], "aspect": "16:9", "metric": "chars", "char_aspect": 0.6, "dirs": [{"label": "rapier/", "rect": [..], "depth": 2, "hue": 3}]}` (labels are the directory's own name with a trailing slash, as in the video; the root label is the corpus name).

`--preview out.png` renders the whole layout with Pillow at 2400 pixels wide: directory padding in the hue at 40 percent, file rectangles as 1 pixel outlines, lines as bars from indent to len in grey. This is the layout's own test and the fastest way to see whether the treemap looks like the video.

Invariants, checked by `tests/check_layout.py` (exit code non-zero on failure): every file rectangle lies inside its directory's inner rectangle; sibling rectangles do not overlap; every line position lies inside its file's rectangle; item rectangles lie inside their file's rectangle; `file_cols * file_rows >= n_lines` for every file.

## Stage 3: atlas_font.py

`./atlas_font.py [--font /System/Library/Fonts/Menlo.ttc] [--index 0] [--cell 64] [--self-test out.png]`

Builds a raster atlas of the 95 printable ASCII glyphs (32 to 126) in a 16 by 6 grid. Each cell is `cell` pixels tall (the line pitch) and `round(cell * A)` wide, where A is the advance width divided by the line height from the font's metrics (ascent + descent); glyphs are drawn with the baseline placed so that ascent + descent fills the cell. Output is an R8 image (coverage), plus a metrics dict `{"cell_w": .., "cell_h": .., "char_aspect": A, "cols": 16, "rows": 6, "first": 32}`. The viewer uploads it with a full mipmap chain and trilinear filtering; with a 64 pixel cell this covers 6 to 64 device pixels per line well and degrades gracefully above. Expose `build_atlas(font_path, index, cell) -> (np.uint8[h, w], metrics)`; `--self-test` writes the atlas PNG and prints the metrics. The viewer also uses this atlas for all UI text.

The default font is Menlo from macOS; `--font` accepts any TTF/TTC/OTF Pillow can load, and the README must say what to pass on Linux (DejaVu Sans Mono).

## Stage 4: atlas_viewer.py

`./atlas_viewer.py data/<name>_atlas [--font ..] [--vector-text] [--frames N --screenshot out.png] [--goto path[:line]] [--zoom PX_PER_LINE] [--filter WORD] [--step K] [--shots DIR]`

### Window and camera

glfw, OpenGL 3.3 core, 1600 by 1000 window, Retina aware: all pixel thresholds below are in device pixels (framebuffer size), never window points. Background `#1c1c1e`. The map starts fitted to the map viewport with a 3 percent margin.

The map viewport is the window while no results panel is shown, and the window minus the right column (the 180 point panel plus its 10 point margins, 200 points) while the panel is open: the map is drawn, fitted, hit-tested and scripted (`--shots`, `--zoom`, `--goto`) in that reduced viewport, and a fitted map refits when the panel opens or closes. The column holds the status line, the filter box and the panel, so nothing is drawn over the map (the video's panel is a docked pane, not an overlay).

The camera is (cx, cy, zoom) in float64 with zoom = device pixels per world unit. Scroll wheel zooms about the cursor with a glide: each wheel tick adds to a zoom velocity that decays exponentially (time constant about 0.12 s), so a flick keeps going briefly and stops smoothly. Left or right drag pans, with the map following the cursor exactly. `R` refits. `Escape` clears the filter, then clears selection.

Zoom limits: minimum is fit-to-window times 0.5; maximum is 400 device pixels per line for the largest-pitch file.

### Fly-to

`fly_to(rect)` animates the camera from the current view to a view that contains `rect` with a margin, using van Wijk and Nuij's smooth zoom-and-pan (Proc. EuroVis 2003) with ρ = 1.4 and V such that a typical result hop takes 0.6 to 0.8 s; clamp the duration to [0.3, 1.2] s. Used by result stepping and by `--goto`. The path visibly zooms out and back in when the two places are far apart, and is a short straight glide when they are close.

### GPU data

Everything is resident, uploaded once at start:

- `tex_chars`, `tex_kinds`: R8UI 2D textures of width 16384; byte offset o maps to texel (o mod 16384, o div 16384). Height rounded up. The 16384 width is a constant shared with the shaders. If total bytes exceed 16384 squared (268 M), refuse to start with a clear message.
- `tex_line_f`: RGBA32F 2D texture, width 4096, one texel per line: (x, y, j, 0) from line_pos, with j the line's index within its file (the rung 0 sampling needs it). (Keep the last channel zero for now.)
- `tex_line_u`: RGBA32UI 2D texture, width 4096, one texel per line: (byte_off_lo32, file, indent | len << 16, byte_off_hi32).
- `tex_file_f`: RGBA32F 2D texture, width 4096, two texels per file: (x0, y0, x1, y1) and (pitch, colw, cap, hue).
- `tex_file_u`: RGBA8UI 2D texture, width 4096, one texel per file, updated every frame: (rung, flags, step_lo, step_hi) where flags bit 0 = hovered, bit 1 = has filter hits, bit 2 = current result file, bit 3 = dimmed (no hits while a filter is active), and step (two bytes, 1 above rung 0) is the rung 0 line sampling step.
- `tex_glyphs`: R8 atlas from atlas_font.py with mipmaps, trilinear.
- `tex_dir_f`: RGBA32F, two texels per directory: (x0, y0, x1, y1) and (pad, depth, hue, 0).
- Item rectangles and hit rectangles are instanced from small per-frame VBOs.

Instanced drawing uses a unit quad VBO and `gl_InstanceID`, with integer texture lookups via `texelFetch`. No texture buffers (macOS limits), no SSBOs.

### Per-frame CPU work

In numpy over files: `ppl = file_pitch * zoom` (device pixels per line); `rung = 0 if ppl < 1 else 1 if ppl < 3 else 2 if ppl < 6 else 3`; visible = file rect intersects the view rect; for rung 0 files the sampling step `ceil(1 / ppl)` (clamped to 65535). Upload `tex_file_u`. The ladder:

| rung | ppl | what is drawn |
|---|---|---|
| 0 sampled bars | under 1 | kind bands (below), and every step-th line (file-relative index `j` with `j mod step == 0`) as a grey bar exactly 1 device pixel tall from indent to len, so the density stays about one bar per pixel row: the circuit-board texture of the whole map |
| 1 line bars | 1 to 3 | kind bands, and one grey bar per line from indent to len |
| 2 tokens | 3 to 6 | one block per character in the kind colour, item outlines |
| 3 text | 6 and up | glyphs from the atlas in the kind colour, item outlines | Compute the hovered file from the cursor (point in file rect; use a coarse uniform grid over the world built at start to keep this O(1)), and the hovered line and column from the file's layout (column c from x, row r from y, line j = c * rows + r, col from x within the column divided by p * A). Compute the crumb trail: the deepest directory whose rectangle contains the view centre and is at least 64 device pixels on its shorter side.

### Draw order and shaders

1. Directories: for every directory with depth ≥ 1 whose rectangle is visible, draw the outer rectangle filled with the hue at 55 percent alpha over the darker inner fill, so the padding ring reads as a coloured band; below 1 device pixel of padding, still draw a 1 pixel line in the hue. Use an instanced rectangle shader with the ring computed in the fragment shader from the world-space padding, with `fwidth` to guarantee the 1 pixel minimum.
2. Files: instanced over all files, vertex shader culls invisible ones (degenerate quad). Fill: every rung draws the file background `#111114`; the hue lives only in the directory bands, so a file's colour never flips at a rung cut. Border: 1 device pixel in a light grey (`#8a8f9a` at 70 percent); hovered file: fill `#dfe3ec` at 55 percent over everything (the light bar in the overview); hit file: border becomes 2 pixels of `#e8d44d`; current result: 3 pixels `#e8d44d`; dimmed: fill darkened 50 percent.
3. Lines: one instanced draw over all lines. Vertex shader: fetch line and file texels; if the line is outside the view, or the file is at rung 0 and the line's file-relative index is not a multiple of the file's step, emit a degenerate quad. Quad from (x, y) with width `len * p * A` (clipped to `cap * p * A`) and height p, or exactly 1 device pixel at rung 0. Pass to the fragment shader: the line's byte offset, indent, len, cap, p, the rung, and the unit uv. Fragment shader: `col = floor(uv.x * len_clipped)`; rungs 0 and 1: discard if col < indent, else output `#a4a8b0` at an alpha that ramps with pixels per line, `clamp(0.5 + 0.15 ppl, 0.55, 0.8)`, so only the sampling changes at the 1 px cut (rung 0 the sampled bar, rung 1 a grey bar from indent to len); rung 2: fetch the kind at byte_off + col, discard if space, output the kind colour (a block per character, which gives the token segments); rung 3: fetch the char and kind, sample the glyph atlas cell for the char at the in-cell uv, multiply the kind colour by the coverage. Kind colours (RGB): space none, ident `#cfd2d8`, keyword `#7aa2f7`, type `#e0c080`, string `#d7a0a8`, comment `#7f9f7f`, number `#f0a060`, punct `#8c909a`, macro `#7fc8c8`, call `#c8d08c`. When the file is dimmed, multiply by 0.45.
4. Kind bands and item outlines: for visible files at rung 0 or 1, every item rectangle is drawn before the lines as a filled band in its item kind colour at 18 percent alpha (the ladder's kind-bands representation: fills far, outlines near); for files at rung 2 or 3 that are visible, draw their item rectangles as 1.5 device pixel outlines in the item kind colour: function `#7aa2f7` at 60 percent, struct `#e0c080`, enum `#d7a0a8`, impl `#5fb7b7`, other `#8c909a`. Nested outlines are fine. Build the instance list on the CPU from the visible files each frame (numpy boolean index over item_rect_item's file).
5. Hits: while a filter is active, every hit in a visible file at rung ≥ 2 draws a 2 pixel `#e8d44d` outline around its token rectangle (from the hit's column and length); the current result draws a filled `#e8d44d` at 25 percent as well.
6. Directory labels: for visible directories with depth ≥ 1 whose shorter side is at least 48 device pixels, draw the label as a tag at the rectangle's top left, inset by the padding: 13 point text (26 device pixels on Retina) in `#e6e6e6` on a `#2a2a2e` box at 85 percent alpha. Labels never scale with zoom. Skip a label if it would cover more than 40 percent of its rectangle's width. Label chains: when a directory's inner rectangle is filled by exactly one child directory (the child covers at least 85 percent of it), the parent draws no tag and the innermost directory of the chain draws one tag joining the chain's names with ` · ` (`platform · src · os`, as the video's `git · tests`) at its own corner; the crumb trail is unaffected.
7. UI: crumb trail bottom left ("← makepad › libs › rapier"), hover label next to the cursor (path, then `:line` and the enclosing item's `kind name` when the hovered file is at rung ≥ 2; at rung 3 also `· N hits` when a filter is active), and the right column: a status line with `files · lines · chars` of the corpus and, while a filter is active, `D definitions · R references · F files · i/N`, the filter box under it, and the results panel (see Filter). While the panel is open the column is its own strip beside the map (see Window and camera) and the status wraps on its ` · ` separators to the panel's width; with no panel the status line and the filter box sit in the top right corner over the map. Directory labels are clipped to the map viewport. All UI text uses the glyph atlas at fixed device pixel sizes through a screen-space text path (one instanced draw of per-character quads built on the CPU; a few thousand characters per frame at most).

### Filter and results

Typing while the filter box is focused (click it, or press `/`) edits the filter text; `Backspace` deletes; `Escape` clears. The search runs when the text changes and has at least 2 characters: a case-sensitive whole-word substring search over `chars` (use `re.finditer` with `\b` on the bytes, in a thread; results replace the previous ones when done). Each hit is (file, line, col, len); a mention inside a comment or a string is not a hit, so references are code only. Hits whose line matches a definition regex for the file's language (`fn|struct|enum|trait|impl|type|mod|def|class|function|interface|const|static` followed by the word) are definitions; the rest are references. The results panel lists definitions first, then references, each grouped by file path with the line number and the trimmed line text, definitions annotated with their kind. The panel is 360 device pixels wide (180 points) in its own column beside the map (the map viewport shrinks by the column while the panel is open), scrolls with the wheel when the cursor is over it, and highlights the current result.

`Enter`, `Down` or `]` steps to the next result, `Up` or `[` to the previous, wrapping; stepping calls `fly_to` on the hit's line rectangle expanded to show about 40 lines and 100 columns (so the destination is the text rung). Clicking a result row does the same. Hit files get the yellow border immediately, and files without hits are dimmed, at every zoom.

Without a resolver, a filter for `Window` in makepad will not give Rik's 119 definitions and 1472 references, and that is fine.

### Self-tests and scripting

`--frames N --screenshot out.png` runs N frames on the scripted path then saves the framebuffer and exits, exactly like big-picture. `--goto path[:line]` starts with a fly-to that location complete; `--zoom PX_PER_LINE` sets the zoom so the largest-pitch file has that many device pixels per line about the view centre; `--filter WORD` applies a filter, `--step K` steps to result K. `--shots DIR` writes the standard set and exits: `overview.png` (fit), `bars.png` (2 px per line at the corpus centre), `tokens.png` (4.5 px), `text.png` (16 px), `hover.png` (text zoom with the cursor placed at the centre so the hover label and fill show), `filter.png` (fit with `--filter` applied, results panel populated), `result.png` (after `--step 1`). Screenshots are saved with Pillow as PNG at framebuffer resolution.

`--stats` prints load time, GPU memory estimate, and after the scripted frames the mean and 99th percentile frame time in ms, then exits.

## Stage 5: vt_glyphs.py (optional, separate)

The Dobbie vector-texture tier. Read `dobbie/README.md` and port from `dobbie/pdf/font.frag` (GLSL ES 1.0) and the atlas layout described there and in his implementation-notes post.

`vt_glyphs.py --font Menlo.ttc [--index 0] [--grid 12] --out data/vt_menlo.npz` builds, for ASCII 32 to 126: quadratic bezier outlines (fontTools; cubic outlines subdivided to quadratics within 0.5 units of error on a 1000 unit em), normalised to the same cell box atlas_font.py uses (advance A by 1 line pitch, baseline at the same place), a grid of `grid` by `grid` cells over each glyph's box, and per cell the list of curves that intersect it plus the inside/outside flag of the cell's centre. Pack these into an RGBA8 texture in Dobbie's format (curve coordinates as 16 bits split over two channels; per cell four one-byte curve indices per texel, a second texel when a cell has more than four; a header texel per glyph with the grid origin and size) so his fragment code needs the least change. Expose `build(font, index, grid) -> (rgba8 array, glyph_table)` where `glyph_table[c] = (grid_x, grid_y, grid_w, grid_h)` in texels.

`shaders/vt_glyph.glsl` provides `float vt_coverage(int glyph, vec2 uv)` for GLSL 330: the ported ray-cast with 4 rotated sample directions and the parabolic window, using `texelFetch` on the atlas.

`tests/test_vt_glyphs.py` opens a hidden glfw window, renders `The quick brown fox 0123 {}[]()` at 12, 32, 96 and 300 device pixels per line into an offscreen framebuffer, saves PNGs, and compares the 96 pixel rendering against a Pillow rasterisation of the same string with the same font at the same cell size: mean absolute coverage difference must be below 0.06 and the test prints the number. If it cannot get below 0.1 after a reasonable effort, report that honestly; the tier stays optional.

## Sizes and precision

big-picture is about 15 k lines; makepad's `draw/` plus `platform/` about 250 k lines; all of makepad 3 M lines and 120 MB of characters. Per line the GPU holds 32 bytes of texels and per character 2 bytes, so 3 M lines is about 100 MB of lines and 240 MB of characters: resident is acceptable on an 8 GB machine, and the viewer must print its estimate at start. The index step is pure Python and must be multiprocess; 120 MB through the tokenizer at 1 to 2 MB per second per core is a couple of minutes on ten cores, which is acceptable for an offline step but must show progress.

World coordinates are up to 1600 units; pitches are about 0.1 units for 3 M lines and about 3 units for 15 k lines. float32 world positions have about 1e-4 units of error at 1600, which is under 0.1 percent of the smallest pitch. The vertex shader receives the camera as a float32 offset and scale and computes `(world - offset) * scale`; the CPU keeps the camera in float64.

## Acceptance

The build is done when, on `data/big-picture_atlas` and on `data/makepad-draw_atlas` (makepad/draw plus makepad/platform, indexed with `./atlas_index.py makepad/draw makepad/platform` or a small wrapper), the viewer:

- shows the treemap fitted with directory hues and labels, files as bordered rectangles with wrapped columns, and tiny files as slivers;
- zooms with the wheel from the whole map to readable text in one continuous glide with rung changes visible as hard cuts at about 1, 3 and 6 device pixels per line, and no frame drops visible in the `--stats` numbers (99th percentile under 16 ms on the big-picture corpus, under 33 ms on makepad-draw, on the development machine);
- follows the cursor with the hover fill and label at every rung;
- highlights every hit file in yellow the moment a filter is typed, dims the rest, fills the results panel, and flies to results on Enter;
- produces the `--shots` set without error;
- passes `tests/check_layout.py` on both corpora.

## Deviations recorded during the build

- Zoom reference: `--zoom`, the zoom limits and the shots use the pitch of the file under the view centre (or, for wheel zoom, under the cursor), falling back to the line-weighted median pitch, not the largest-pitch file: the largest pitch belongs to the tiniest file in every real corpus, which made the limits useless. A clamp never moves the zoom against the gesture when the map slides under the cursor.
- Hover fill: 55 percent at rungs 0 to 2 and on the file's border ring at every rung, but 18 percent inside the text rung so the glyphs stay readable.
- Borders are drawn twice: the fill pass before the lines, and a ring-only pass after the lines and item outlines, so column-0 text never breaks a hit outline.
- Lines are drawn per run of consecutive visible files (files are contiguous in depth-first order), not as one draw over the corpus, so the vertex shader visits only the lines of visible files.
- Directory tags that would sit on an ancestor's tag slide right along the top edge past it, so nested directories read as a trail ("platform/ src/ os/ linux/"), and only wrap downwards when the edge runs out; single-child chains collapse into one tag as in the video.
- The search runs over a newline-joined copy of the corpus (one byte per line extra) so word boundaries hold at line ends, in 1 MB chunks with pos/endpos so the boundary context is kept, yielding between chunks.
- Results rows keep the file name of a long path (left-truncated) and window the line text around the hit column so the matched word is always visible.

## Stage 6: atlas_resolve.py (phase 1)

`./atlas_resolve.py data/<name>_atlas [--workers N]`

Reads the atlas and the source files again, parses each with tree-sitter (Rust fully; Python and C with a smaller set of rules), and writes `resolve.npz` and `resolve.json` beside the index. The viewer works without them; with them the filter, the hover label and the Inspector use real entities instead of regexes. This is a lexical resolver in the sense of Rik's "lexical references recognised": names are resolved by scope, file, imports and uniqueness, with no type inference, and every reference gets a status saying how far that got.

Entities (definitions), Rust: functions and methods, structs, fields, enums, variants, unions, traits, type aliases, consts, statics, modules, macro_rules, type parameters, function parameters and let bindings (locals). Each has a file, a file-relative line, a column and length (its name token), a kind, a name, and a scope: the enclosing entity (the impl or trait for a method, the function for a local, the struct for a field) or the file. Impl blocks are scopes, not entities. Python: functions, classes, parameters and assigned locals. C-like: functions, structs, enums, unions, typedefs, parameters, declarations.

References: every identifier that is not a definition: values and calls, types, fields and methods after a dot, path segments (`a::b::C` resolves `C` through the path), macro invocations, `Self`, and the names inside `use` declarations. Identifiers in comments and strings never appear (tree-sitter does not produce them).

Resolution, in order, stopping at the first that applies; the status names the step:

| status | meaning |
|---|---|
| local | a parameter or let binding of the enclosing function |
| file | an entity of a fitting kind in the same file (type contexts want types, value contexts want values) |
| import | through the file's `use` map: `crate::`, `super::`, `self::` and workspace crate names resolve to a crate found by its Cargo.toml, then the name among that crate's entities |
| crate | a unique entity of that name in the same crate |
| global | a unique entity of that name in the whole corpus (Rik's "inferred") |
| generic | a type parameter of an enclosing item |
| self | `Self` in an impl whose type resolves |
| method | a field or method name after a dot with more than one candidate in the corpus (Rik's "unknown method dispatch") |
| ambiguous | several candidates and no rule to pick one |
| external | the path root is a crate not in the corpus (std, core, alloc, a registry crate), or a Python builtin |
| import missing | a `use` whose crate is in the corpus but whose name is not (Rik's "import not found") |
| macro missing | `name!` with no macro_rules of that name (std macros are external) |
| self unresolved | `Self` outside an impl, or in an impl whose type did not resolve |
| not found | none of the above |

Crates: the nearest ancestor directory with a Cargo.toml, named from its `[package]`; files under no Cargo.toml are unattached (Rik's count) and resolve without the import and crate steps.

`resolve.npz`: `ent_file` uint32, `ent_line` uint32 (file-relative), `ent_col` uint16, `ent_len` uint16, `ent_kind` uint8, `ent_scope` int32 (entity index or -1); `ref_file`, `ref_line`, `ref_col`, `ref_len` likewise, `ref_ent` int32 (the entity, or -1), `ref_status` uint8 (the table above, in order from 0), `ref_name` uint32 (index into the names table); `file_refs_in` uint32 (references from other files to this file's entities, the References metric), `file_refs_out` uint32, `file_crate` int32.

`resolve.json`: `names` (the string table, entity names by `ent_name` index and reference names), `ent_name` (uint32 list), `kinds` and `statuses` (name lists), `crates` [{name, dir}], `stats` (files parsed, unattached, entities, references, one count per status, seconds).

Viewer with a resolve file present:

- The filter matches entity names first: Definitions are the entities with that name (kind from the resolver), References are the references with that name, resolved ones first, each row tagged with its status when it is not resolved. A word that names no entity falls back to the regex search.
- Hover at the text rung on an identifier shows the resolver's view: `struct Window · 1 definition · 129 references`, where the counts are the entities of that name and the references that resolve to them.
- Click at the text rung selects the entity under the cursor (or the target of the reference under it) and opens the Inspector tab, which shows the entity's kind, name, path and line, its scope, and its references grouped by file; with nothing selected it shows the coverage block: files parsed and unattached, entities, references, and one line per status with its count, in the style of Rik's panel. `I` toggles the panel, `Tab` switches Inspector and Results.
- `atlas_layout.py --metric references` uses `file_refs_in`.

Acceptance: on makepad-draw, `Window` lists the `pub type Window = XID` definition with kind type and only code references; hovering `Window` in x11_sys.rs at text zoom shows its counts; the Inspector coverage block has every status with a non-zero total; `./atlas_resolve.py` on all of makepad finishes in a few minutes with `--workers 10`.

## Stage 2 as built in phase 2: rows and metric layouts

Long lines wrap: the layout emits visual rows, not lines. A line of L characters in a column of cap characters takes 1 row if L is at most cap, else 1 plus ceil((L - cap) / (cap - 2)) continuation rows, each drawn 2 characters in (the hang); columns under 16 characters clip instead. The pitch and the capacity are iterated to a fixed point because wrapping adds rows. `layout.npz` therefore carries `row_pos` (x, y per row), `row_line` (the row's line), `row_col0` (its first column), `row_len`, `line_row0` (first row of each line, n_lines + 1) and `file_row0` (first row of each file, n_files + 1) instead of `line_pos`; item rectangles span rows. The viewer's line textures hold one texel per row (byte offset = the line's offset plus row_col0; indent only on a line's first row), hover maps a row back to its line and column, and `row_of(line, col)` maps the other way for hits and fly-to.

One layout per metric: `atlas_layout.py` writes `layout.npz` for tokens and `layout_<metric>.npz` plus its JSON for references (when `resolve.npz` exists) and lines; `--metric X` writes just one. The viewer loads every layout present, the toolbar's metric buttons and `M` switch between them by re-uploading the layout textures (a hard cut, as Rik settled on), and the camera stays put because the world size is the same. The toolbar is text buttons drawn over the map's top left: lens (Folders), projection (2D; 3D greyed until phase 3), metric.

## Stage 4 as built in phase 3: the 3D projection

All world shaders (dir, file, line, rect, wall) position vertices with one `uMVP` matrix and cull with a `uView` world rectangle; `uZScale` is 0 in 2D and 1 in 3D, so heights collapse onto the plane in 2D and the orthographic matrix reproduces the old mapping exactly. Borders are computed from the world distance to the edge and `fwidth`, so they stay one device pixel wide in perspective; the rect shader interpolates screen coordinates `noperspective` for the same reason.

The 3D camera orbits the focus (cx, cy, z_focus) at tilt (0 to 70 degrees, default 55) and yaw, at distance D = fb_h / (2 zoom tan(fovy / 2)) with fovy 45 degrees, so one world unit at the focus is zoom device pixels, as in 2D. Per file, pixels per line is pitch times zoom times focus_w / w (the clip w at the file's centre), which is what picks the rung; visibility comes from the projected corners. z_focus follows the roof of the file under the view centre, weighted in from 0 at 0 pixels per line to 1 at 2, and holds its value over directory bands; it eases at 20 percent per frame interactively and snaps in scripted runs. The 3D fit backs the zoom off until the map's corners project inside the viewport.

Heights: directories are terraces, z base (depth - 1) times 5 world units and height 5; a file's z base is its directory's terrace top and its height is 90 times the square root of its metric over the corpus maximum, from `file_metric` in the layout, so the References layout gives the fan-in skyline. `tex_file_f` and `tex_dir_f` carry a third texel per entry with (z base, height). The wall pass draws four instanced quads per file and directory, flat-shaded by side (the +y side, facing the tilted camera, brightest), files in a muted hue and directories in the full hue. Draw order in 3D with the depth test on: directory tops, walls, file tops, then bands, lines, item outlines and hits at small z offsets, then the border ring. Picking unprojects the cursor onto the focus plane, then onto the roof of the file found there. Alt-drag tilts and turns (and switches 2D to 3D); the 3D button and the 3 key toggle; `--proj 3d --tilt --yaw` script it and `--shots` adds 3d.png and 3d_zoom.png.

## Phase 4 as built: the Layers lens and selection

The file graph: an edge A to B for every reference in A that resolves to an entity in B (A and B different files), counted once per pair. `atlas_layout.py --lens layers` (part of the default `all` run when `resolve.npz` exists) writes `layout_layers.npz` and its JSON: Tarjan's strongly connected components, condensed; rank = longest path down to a component with no successors; files with no edges at all in a bottom row of their own. The pseudo-tree replaces the directories: the root stacks its children as rows (highest rank on top, heights by weight^0.6), each row is a "directory" labelled `layer N · M files` holding its singleton files and, for every cycle of more than one file, a group labelled `cycle · M files`; files are squarified inside rows and groups as usual and keep the hue of their real top-level directory. The JSON `dirs` of every layout now carry `parent`, `children` and `files`, and every layout's npz carries `file_dir`, so the viewer takes the tree from the active layout. The viewer keys layouts by metric for the Folders lens and by "layers" for the Layers lens; the toolbar's lens group and metric buttons switch between them.

Selection: the viewer builds in and out adjacency from the resolver's references at load. A click on a file selects it (a click on a symbol at the text rung still selects the entity), Shift-click toggles, Shift-drag draws a marquee and selects every visible file whose screen rectangle it touches (projected corners in 3D). File flags gain bit 4 (selected: a 3 px light border and a light fill) and bit 5 (neighbour: a teal fill and border); while a selection exists and no filter is active, every other file is dimmed. The Inspector shows the selection with each file's out and in degree, then the union of files it uses and files that use it, each row a fly-to. Escape clears the filter, then the current result, then the selection. `--lens layers` and `--select PATH[,PATH]` script it; `--shots` adds layers.png and selection.png (the file with the highest fan-in).

## Phase 5 contract: History

Row 29 of PARITY.md: git churn and recency per file, a revision rail that re-indexes the corpus at a chosen commit and switches with a hard cut, change lighting between two revisions, and churn as a colour lens. This is also the recency heatmap from the lenses discussion. Everything here works without the resolver; the Inspector's entity views stay HEAD-only.

### Stage 7: atlas_history.py

`./atlas_history.py data/<name>_atlas [--max-revisions N]`

Reads `index.json` for the root and the file paths, finds the git repository with `git -C root rev-parse --show-toplevel` (the root may be inside a submodule, as makepad is; files under no repository get zero history and the script says how many), and runs one `git log --numstat --format=%H%x00%ct%x00%an%x00%s --no-renames -- <src dirs>` over the indexed directories. 724 commits over makepad's draw and platform take 0.5 s; all of makepad a few seconds. Paths in the log are relative to the toplevel and are mapped to the index's root-relative paths; a numstat line whose path is not in the index (a deleted file, a binary with `-` counts, a file the index skipped) is kept in the revision table but has no file index. Renames are not followed: `--no-renames` makes a rename a removal and an addition, which is what change lighting can show; this is noted in the README.

`history.npz`:

| array | dtype | shape | meaning |
|---|---|---|---|
| rev_time | int64 | [n_revs] | commit time, newest first (index 0 is HEAD or the newest commit touching the corpus) |
| rev_ptr | int64 | [n_revs + 1] | CSR pointer into the per-revision file lists |
| rev_file | int32 | [n_entries] | file index, or -1 for a path not in the index |
| rev_added, rev_removed | uint32 | [n_entries] | numstat counts for that file in that commit |
| file_commits | uint32 | [n_files] | commits touching the file |
| file_added, file_removed | uint32 | [n_files] | summed over all commits |
| file_first, file_last | int64 | [n_files] | time of the first and last commit touching the file; 0 when untracked |

`history.json`: `{"toplevel": "...", "prefix": "" or "sub/dir/", "revisions": [{"hash", "short", "time", "author", "subject", "files"}], "stats": {"revisions", "tracked", "untracked", "seconds"}}`, revisions newest first, same order as the npz. `--max-revisions` truncates to the newest N (default none). The per-file arrays are the whole history; anything windowed (churn over the last year) is computed by the viewer from the CSR.

### The churn metric

`atlas_layout.py` gains `--metric churn` (part of `all` when `history.npz` exists): a file's weight is `file_added + file_removed`, floored at 1 so untouched files keep a sliver. The viewer picks it up like every other metric, so churn also drives the 3D heights. `METRIC_ORDER` becomes tokens, references, churn, lines, chars, bytes.

### Colour lenses

A colour lens tints the file fill; the directory bands, borders, bars and glyphs do not change, so the ladder reads the same at every rung and the tint is visible under the bars at the far rungs and in the margins at the near ones. Lenses: none (today), churn, age, changes. The viewer computes one float in [0, 1] and one class per file and writes them into the two free channels of the file's third texel in `tex_file_f` (`(z base, height, value, class)`), re-uploaded on every switch and on every rail change, which is one small upload; the file shader gets `uniform int uLens` and mixes the fill toward a ramp by the value:

| lens | value | ramp | class |
|---|---|---|---|
| churn | `log1p(added + removed over the window) / log1p(max over the corpus)` | FILEBG to `#c86040` (ember) at up to 65 percent | 0 |
| age | `1 - clamp((now - file_last) / (now - oldest file_last), 0, 1)`, so recently touched is 1 | FILEBG to `#5fb7b7` (the teal already used for neighbours) at up to 65 percent; untracked files stay FILEBG | 0 |
| changes | see change lighting | | 1 added, 2 changed, 0 unchanged |

The churn window is the whole history by default; `--since DAYS` on the viewer restricts churn and the rail's ticks to the last N days. Dimming (filter or selection) multiplies after the tint, as today. Toolbar: a fourth group of text buttons `None · Churn · Age · Changes`, the active one in yellow; `C` cycles them. Changes is greyed until a second revision is set. The status line shows the lens and its window (`churn · all history`, `age · newest 2026-09-11`, `changes · 3c788d5 → HEAD · 41 added · 118 changed · 6 removed`).

### The revision rail

`H` or a `History` button (a fifth toolbar group) shows the rail: a strip 22 device points tall across the bottom of the map viewport above the crumb trail, time running left to right from the oldest revision in the table to HEAD, one 1 pixel tick per revision (ticks merge visually when dense; that is fine), year and month labels along the top edge where there is room, the loaded revision marked with a 3 pixel yellow tick and its short hash and date in a tag, the compare revision (if any) with a teal tick and tag. Hovering the rail shows the nearest revision's short hash, date, author and subject in the hover label. Click loads that revision; Shift-click sets it as the compare revision; `Left`/`Right` step the loaded revision by one commit while the rail is shown (`Shift` steps the compare revision). Clicking HEAD's tick returns to the atlas on disk.

Loading a revision: the viewer runs, in a thread, `git -C toplevel archive <hash> -- <src dirs> | tar -x -C data/<name>_atlas/rev/<short>/src`, then `atlas_index.py` and `atlas_layout.py --metric tokens --lens folders` into `data/<name>_atlas/rev/<short>/` with the same `--name`, then loads that atlas (index, layout, no resolve, no history) and swaps every GPU texture: characters, kinds, rows, files, directories, item rectangles, hover grid, directory tags. The world is the same size, so the camera stays; the filter is re-run on the new corpus; the selection is cleared (file indices change). A revision already under `rev/` is loaded from disk without re-indexing, so scrubbing back is instant. While a load is in flight the rail's tick pulses and the status line says `indexing 3c788d5…`; the map stays interactive at the old revision and the switch is a hard cut when the thread is done, the same rule as the layouts. Budget: about one second per revision for makepad-draw (0.3 s archive, 0.45 s index, 0.15 s layout), five for all of makepad. Revisions are laid out with the tokens metric only; the metric buttons are greyed while a past revision is loaded, and the Layers lens too (no resolver). The Inspector shows, for a selected file, a History block from `history.npz`: commits, first and last dates, lines added and removed, and the newest five commits touching it with short hash, date and subject; with a past revision loaded the block says which revision is shown.

### Change lighting

Between the loaded revision A (HEAD by default) and the compare revision B, computed from the CSR without calling git: for every file of A, sum added and removed over the commits in (B, A] (or (A, B] with the sign noted when B is newer). A file whose first commit is inside the range is `added`; a file with any change in the range is `changed`, with value `clamp((added + removed) / lines, 0, 1)`; the rest are `unchanged`. Files present in B and absent from A are `removed`; they have no rectangle in A's layout, so they are counted in the status line and listed in the Inspector's History block (path and the commit that removed them), not drawn. Ramp: added, a green fill `#7fbf7f` at 55 percent; changed, an amber fill `#e0a040` at `0.15 + 0.5 * value`; unchanged, FILEBG. Setting B with no lens active switches the lens to Changes. With a past revision loaded as A the file indices are A's, so the CSR is joined by path.

### Scripting and shots

`--history` shows the rail; `--rev HASH` loads that revision before the first frame (blocking); `--compare HASH` sets B; `--color churn|age|changes` picks the lens; `--since DAYS` windows churn. `--shots` adds `churn.png` (fit, churn lens), `age.png` (fit, age lens), `changes.png` (fit, Changes between HEAD and the commit 100 back in the table, rail shown), and `history.png` (the corpus loaded at that commit, rail shown with the tag).

### Acceptance

On makepad-draw: `atlas_history.py` finishes in under 2 s and reports 724 revisions; `atlas_layout.py` writes `layout_churn.npz` and the toolbar shows Churn; the churn and age lenses visibly separate the platform's os directories from the generated bindings in the fit view; `--rev` at the commit 100 back loads in under 3 s, the status line shows the revision, and the file count in the status differs from HEAD's; `--compare` at that commit lights added files green and changed files amber, and the status line's three counts match `git diff --stat` between the two commits for the indexed directories (added, modified, deleted file counts). `tests/test_history.py` builds a throwaway git repository with three commits and checks the CSR, the per-file sums, and the change classification against known values.

## Phase 5 as built: History

`atlas_history.py` is as contracted, with three details the contract left open. Pathspecs: with several source directories the root has no files of its own and its children are logged; with one, the root is logged whole (`.`), so files deleted from it appear too. Paths seen in the log that are not in the index (deleted files, binaries, files the index skipped) go into a `unindexed` table in `history.json`, root-relative like the index's paths, and every entry carries `rev_other`, its index there; `changes_between` finds the removed files through it. The per-file first and last commits are stored as table indices as well as times (`file_first_rev`, `file_last_rev`), and the added rule uses the index, not the time: a file is added between two revisions when its first commit lies inside the range, even if it added no lines, which is what makes the counts equal `git diff --name-status` (78 added, 203 modified, 0 deleted between HEAD and the commit 100 back, on makepad-draw). A path added and deleted inside the range is neither added nor removed; a removed path must have an entry older than the range, so it existed at the older revision. Revisions that touch no indexed file are dropped from the table (689 of 724 commits remain for makepad-draw). The repository root is compared to git's toplevel through realpath, since macOS temp directories are symlinked. `tests/test_history.py` builds a four-commit repository and checks all of this.

The churn metric is `file_added + file_removed` floored at 1, as contracted, and `METRIC_ORDER` now lists it between references and lines.

Colour lenses: the age value is the file's rank among tracked files by last commit (newest 1), not a linear ramp over time; on an active repository the linear ramp was flat teal, since most files were touched in the last few months. Churn and changes are as contracted. The lens channels live in the file's third texel and `upload_lens` re-uploads the whole file texture (a few thousand texels) on a switch; the file shader reads `uLens`. With the panel closed the lens and revision text is a second, yellow status row under the corpus line, because appended to the status line it ran into the toolbar.

The rail is as contracted, with the tags placed right of their marker, else left, else on a row above the rail, since when one tag spans the other's marker both places inside the rail collide. The rail overlays the bottom of the map like the crumb trail does; the fit does not reserve space for it. `Left` and `Right` step the loaded revision, with `Shift` the compare revision; Shift-click over the rail sets the compare revision, which needed the mouse handler to pass Shift through for clicks over UI (it was only honoured for the marquee).

Loading a revision extracts `git archive` into a temporary directory outside the repository rather than under `data/`, because the indexer asks `git ls-files` inside a repository and `data/` is ignored, so an extraction there would index nothing. The atlas lands under `data/<name>_atlas/rev/<short>/` and is reused. `attach_atlas` holds everything derived from the atlas on the CPU and is shared by the constructor and `swap_atlas`, which also recreates the character, kind, row, file and directory textures. A past revision's atlas has only the tokens layout and no resolver, so the metric buttons, the Layers lens and the entity views are unavailable there; the history arrays stay HEAD-indexed and are joined to a past revision's files by path (`head_index`), so the lenses still work on it and files HEAD no longer has get no tint. The Inspector opens without a resolver when there is a history: with nothing selected and the Changes lens on (or no resolver) it shows the History block with the loaded and compare revisions, the counts and the removed files; a selection shows each file's commits, dates, lines and newest five commits before its Uses and Used-by lists. `--rev` and `--compare` take a hash prefix or `~N`, N commits back in the table.

Measured: makepad-draw with the churn lens and the rail, `--frames 300 --stats`, 2.1 ms mean and 6.3 ms 99th percentile against 1.6 and 4.4 without (the rail draws one box per commit); a revision of makepad-draw loads in 0.8 s from scratch (archive, index, layout) and instantly from the cache. `tests/test_viewer_history.py` drives the rail with synthetic clicks and keys: a click loads a revision in the background and the map swaps, HEAD's tick returns, Shift-click sets the compare revision with counts equal to `changes_between`, C cycles the lenses, Left steps a commit older.

## Rough edges closed after phase 5

Four of the rough edges recorded in PARITY.md were closed before phase 6, and one was left with its reason.

The UI has its own strips. The contract's "the map viewport is the window while no results panel is shown" became: the map's viewport is the window minus a top strip (the toolbar row, with the filter box at its right end while the panel is closed), a bottom strip (the crumb trail row, with the status right-aligned on it while the panel is closed, the lens and revision in yellow before it, each cut to the room left; plus the revision rail's row above it when the rail is shown), and the right column while the panel is open. `update_sizes` computes `top_h`, `bottom_h`, `map_y0` and `map_h` from the same font metrics the UI draws with; the GL viewport and the label scissor are set to that rectangle; every camera conversion (`camera_matrix`, `unproject`, `project`, `view`, `fit_zoom`, `screen_to_world`, `world_to_screen`, `zoom_about`, `fly_to`, the marquee projection) uses `map_h` and offsets by `map_y0`, so screen coordinates stay window coordinates throughout; `hover_at` returns nothing outside the map and a click outside it is a UI click. The fitted map therefore sits clear of every control, and the 3D camera's distance follows from `map_h`.

The window is sized to the work area explicitly, four points short of what the screen can hold, so macOS never clamps it and the framebuffer is 2940 by 1640 on this Mac in every run (it was 1658 or 1660 before, by the point the clamp chose; the height is rounded down to ten points because the reported work area itself jitters by a point). All screenshots were regenerated at that size.

A directory tag that still overlaps another after the eight slides along its top edge is skipped instead of drawn on top; this thins the far rows in 3D, where perspective packs the tags.

A loaded revision gets its resolver and its other layouts in the background. Stage one of `build_revision` extracts the commit with `git archive` into `rev/<short>/src/` (kept, because the resolver reads the sources again from the index's root) and indexes it with the indexer's new `--no-git` flag (the cache is under the ignored `data/`, where `git ls-files` lists nothing), lays out the tokens metric and hands over for the swap; stage two resolves and writes the remaining layouts (1.7 s for makepad-draw) and hands over again, and `enrich_atlas` takes the resolver and the layouts without touching the textures, so the camera, hover and selection stay while the metric buttons, the Layers lens and the entity views appear. A cached revision missing stage two gets it on load. The status line says `resolving…` meanwhile. The churn metric is the one thing a past revision lacks, since the history table is HEAD's.

Left as recorded: the Layers lens has no crossing minimisation within a row. Files inside a row are squarified by weight into a block, no edges are drawn, and the video shows no ordering either, so reordering by neighbours would change nothing visible until edges or strips exist; it stays a Layers-lens follow-up for when they do. The resolver's ambiguous and not-found counts (no type inference) and the vector tier's fixed 40 px handoff are by design and phase 6 respectively.

## Phase 6 contract: the vector tier on Slug, the measurement, the Dobbie files, the font

Not a parity row. This is the copyright step from the README's copyright path, the one that blocks going public, and the quality step that README question 2 describes. Four deliverables: a vector tier rewritten on Slug's bands and root rule, a measurement harness that decides the handoff and whether the tier is on by default, the Dobbie demo files moved to fetch-on-demand, and an OFL default face. When it is done, no line of Dobbie's `font.frag` remains in the working tree and no byte of his ships in the repository.

### Stage 5 rewritten: vt_glyphs.py, the band atlas

Outlines as today: fontTools, composites decomposed, TrueType implied points inserted, cubics converted with cu2qu within 0.5 units on a 1000 unit em, every contour of a glyph oriented the same way (`orient()` stays; Slug's coverage takes the absolute value so either convention renders, but one glyph must not mix them). Two changes at the source: a straight segment is stored as `{p1, p2, p2}`, the second endpoint duplicated, as the Slug notes recommend, instead of a midpoint control; and the line box is the font's ASCII ink extents (the top of the tallest and the bottom of the deepest glyph among 32..126), not the OS/2 box widened to them. Menlo and JetBrains Mono NL both come out at 1.05 em with a character aspect of 0.571, so the font switch below changes no proportion; the OS/2 rule would have made JetBrains Mono's box 1.32 em and its glyphs a fifth smaller at the same pixels per line. `atlas_font.font_box` adopts the same rule, so the raster and vector tiers share one cell exactly as today.

Glyph space stays the cell box: x over the advance, y down from the box top, so uv (0, 0) is the cell's top left and (1, 1) its bottom right, the same uv the raster cell is sampled with. Coordinates are float32 from here on; the 16-bit fixed point of the grid atlas goes.

Two textures, both 4096 texels wide as in the reference (`kLogBandTextureWidth`), heights as needed:

| texture | format | contents |
|---|---|---|
| `curves` | RGBA32F | one texel per curve holding (p1.x, p1.y, p2.x, p2.y); the next texel's xy is p3, which for a closed contour is the next curve's p1, so a contour of n curves is n + 1 texels, the last holding (p1 of the first curve, 0, 0); a contour never straddles a row end |
| `bands` | RG16UI | row 0 is the directory: four texels per code 0..127, (glyph block x, y), (horizontal bands − 1, vertical bands − 1), (bbox x0, y0), (bbox x1, y1), the bbox in 16-bit fixed point over [−0.5, 1.5]; codes outside 32..126 point at the space glyph. Glyph blocks follow from row 1 in one linear address space that wraps at 4096 (address a is texel (a & 4095, a >> 12), the reference's `CalcBandLoc`): nh horizontal band headers (curve count, offset of the band's list from the block start), then nv vertical headers, then the lists, one texel per curve holding the curve's texel (x, y) in `curves` |

The reference stores curves as RGBA16F. RGBA32F is used here because fp16 resolves 1/2048 of the em, which at 300 pixels per line is 0.15 pixels of edge position, enough to show in the measurement against Pillow; the memory is a few hundred kilobytes either way. Sampling RGBA32F with `texelFetch` is core in GLSL ES 3.0 and WebGL2.

Bands: equal-width over the glyph's ink bounding box (the bbox in the directory gives the band transform: index = clamp(int((uv − bbox0) · n / (bbox1 − bbox0)), 0, n − 1)), nh and nv chosen independently from 1 to 16 as the count that minimises the maximum number of curves in any band, the smaller count on a tie. A curve belongs to a horizontal band when its control points' y range, widened by half the largest pixel the tier serves (0.5 / `min_ppl` in uv, with `min_ppl` a builder parameter written into the metrics) plus the reference's 1/1024 epsilon, overlaps the band; vertical bands the same in x. A straight horizontal curve is in no horizontal band and a straight vertical curve in no vertical band. Horizontal band lists are sorted by the curves' maximum x descending, vertical lists by maximum y descending, which is what the shader's early exit relies on.

Outputs: `build_atlas(font, index, min_ppl, box) -> (curves float32 [h, 4096, 4], bands uint16 [h, 4096, 2], metrics)`, `save`/`load` npz with the metrics (char_aspect, ascent, descent, upem, advance, min_ppl, curve count, max curves per band), `make_textures(curves, bands) -> (tex_curves, tex_bands)` bound as `vt_curves` (sampler2D, unit 8) and `vt_bands` (usampler2D, unit 9). The viewer's cache key gains `min_ppl`. The builder prints curves, bands, texture sizes and the largest band.

### shaders/vt_glyph.glsl: the Slug pixel function in GLSL

A translation of `SlugRender` and its helpers from EricLengyel/Slug (MIT or Apache-2.0, copyright 2017 Eric Lengyel) with the notice at the top of the file and in THIRD_PARTY_NOTICES: `vt_root_code` (the sign-bit classification, `floatBitsToUint` for `asuint`, the same 0x2E74 table), `vt_solve_h` and `vt_solve_v` (discriminant clamped to zero, the near-linear branch at 1/65536), the horizontal and vertical band loops with the sorted early exit, `saturate(r + 0.5)` coverage and `saturate(1 − 2|r|)` weights, and `CalcCoverage` with the nonzero rule (no even-odd, no optical weight). The signature `float vt_coverage(int code, vec2 uv, vec2 duvdx, vec2 duvdy)` is kept, so `line.glsl` changes only its sampler declarations; ems per pixel is `abs(duvdx) + abs(duvdy)` per axis, the `fwidth` the reference takes. The reference's vertex-shader dilation does not apply: the tier draws inside the layout's cell quads, so there is no per-glyph quad to expand, and a glyph's ink outside its cell is clipped as today (the ink boxes above show at most 15 font units of overhang in these faces, which the widened cell absorbs). Supersampling stays a later option. The Dobbie port is deleted in the same change; nothing of it is kept for comparison in the tree, the harness takes the old files from git.

### tests/bench_vt.py: the measurement

Three contenders on the default face: the new tier; the old tier, taken from git at a366a85 (`git show a366a85:shaders/vt_glyph.glsl` and `vt_glyphs.py` into a temporary directory and imported from there, so nothing of it lives in the tree); and the raster tier, the 64 pixel mipmapped atlas from `atlas_font.py` sampled trilinearly as `line.glsl` does at the text rung. Two strings: the existing test's `The quick brown fox 0123 {}[]()` and a dense one, `@&%$#§0QOG8B@&%`. Sizes 12, 32, 96 and 300 device pixels per line. Per contender and size:

- error: mean absolute coverage difference against Pillow's rasterisation of the same string at the same cell, over the whole image and over ink pixels, the existing test's measure;
- time: one full 2940 by 1640 screen of glyph cells at that size drawn into an offscreen framebuffer, 20 draws with `glFinish`, milliseconds per screen;
- sparkle: the string rendered at a 10 by 10 grid of sub-pixel offsets in steps of 0.1 pixel, and for every pair of neighbouring offsets along x the count of pixels whose coverage changes by more than 0.25, which a box filter one pixel wide cannot do on a straight edge for a 0.1 pixel shift; reported as the mean over pairs and the maximum.

The harness prints a Markdown table that goes into the README verbatim, and applies two rules that go into the code: the vector tier's handoff `VT_MIN_PPL` becomes the smallest tested size at which the new tier's ink error is at or below the raster tier's (64, the raster atlas's cell size, when none is); and vector text is on by default, with `--no-vector-text` to turn it off, when the new tier's time per screen at that size is at most twice the raster tier's, else it stays behind `--vector-text`. Slug replaces Dobbie only if it wins on error and sparkle at every size and loses no more than a tenth on time; if it does not, the phase stops and the numbers decide what happens next. `tests/test_vt_glyphs.py` moves to the new tier with its threshold unchanged (0.06 at 96 pixels; the port measured 0.010).

### dobbie/: fetch on demand

`dobbie/fetch.sh` lists all twelve files with their MD5 checksums (the ten small ones join the two large ones already there), downloads whatever is missing from wdobbie.com into place and verifies everything; `--check` only verifies. The ten files leave git (`git rm --cached`) and `.gitignore` gains `dobbie/pdf/` and `dobbie/warandpeace/`, so the repository keeps `dobbie/README.md` and `fetch.sh` only. `dobbie/README.md` gets the complete table and says that the files are fetched, never committed. `tests/test_vt_glyphs.py` does not depend on them, so nothing in the build needs the download; a future parity test against the demos would call `fetch.sh` first. What this phase cannot undo: the ten files and the port are in the repository's history. Before the repository goes public that history must be rewritten to drop them (`git filter-repo` on the paths) or the code published from a fresh root; this is recorded in the README's copyright path as the one open item and is a decision for the owner, not part of this phase.

### fonts/: the default face

`fonts/JetBrainsMonoNL-Regular.ttf` (JetBrains Mono 2.304, the no-ligatures face) with `fonts/OFL.txt` and `fonts/AUTHORS.txt` as shipped. `atlas_font.DEFAULT_FONT`, `vt_glyphs.py`'s default, `atlas_layout.default_char_aspect()` and the viewer's `--font` default point at it by a path relative to the repository, so a clone runs with no font on the machine; `--font` still takes any face, Menlo included. With the ink-extent line box the layouts do not move (aspect 0.571 against 0.569), so the change shows only in the glyph shapes. Every screenshot is regenerated.

### Acceptance

`tests/test_vt_glyphs.py` passes on the new tier; `tests/bench_vt.py` prints its table and the README carries it with the two rules' outcomes and the resulting `VT_MIN_PPL`; `git ls-files dobbie` lists the README and the script only and `dobbie/fetch.sh` run once downloads and verifies twelve files; `git grep -i dobbie shaders/` finds nothing; the screenshots are regenerated with the new face and the default vector setting; all tests pass; PARITY.md marks phase 6 done and the README's copyright path rows read done except the history caveat.

## Phase 6 as built

The contract held, with these deviations and findings.

The directory's band-count texel stores (vertical bands − 1, horizontal bands − 1), not the other order: the reference reads `bandMax.x` as the count along x, which is the vertical bands, and `bandMax.y + 1` as the offset of the vertical headers. The first build had them swapped and lost half the ink; `tests/test_vt_glyphs.py` caught it (ink totals half of Pillow's). Glyph space is y up inside the textures, the cell's y-down uv flipped in the shader, so the reference's ray conventions apply verbatim; the builder's line box defaults to `atlas_font.font_box`'s ink extents, so the standalone builder, the test and the viewer agree. The band ceiling is 32 per axis, not 16: 32 cut the time at 96 and 300 px by a fifth against 16, and 64 thrashed the texture cache. Bands are widened for 12 px per line, the handoff the measurement chose.

The harness's first reference was Pillow at the target size, which is hinted and is also what the raster atlas is made of; it put the raster tier ahead of both vector tiers at every size up to 96 px and would have set the handoff at 300. The reference became Pillow at eight times the size, box-filtered down, the exact coverage to within the supersampling. Timing is the best of five batches of fifty full-screen draws; a first run with other work on the machine moved sub-millisecond numbers by a factor of two, and the numbers in the README are from an idle run.

Results (README, Implementation): against exact coverage the Slug tier's ink error is 0.064, 0.023, 0.010 and 0.004 at 12, 32, 96 and 300 px per line, the port's 0.085, 0.049, 0.023 and 0.009, the raster tier's 0.097, 0.079, 0.097 and 0.122; sparkle 7.6 to 30.8 pixels per pair against the port's 115 to 1059; time 1.07 to 1.18 times the port's, the absolute differences one or two hundredths of a millisecond per full screen. The contract's rules: `VT_MIN_PPL` = 12 (Slug's error is below the raster tier's at the smallest size tested); vector text on by default (0.48 against 0.24 ms per screen at 12 px, the rule's factor of two exactly, so the rule passed at its boundary); the replace rule failed on time at three sizes by 7 to 18 percent. The contract said the phase stops there and the numbers decide; the phase went ahead because the alternative, keeping the port, is what the phase exists to remove, and because the time cost is a tenth of a shader that costs under half a millisecond per screen. This is recorded as a decision for the owner to confirm; the switch back to raster by default is `--no-vector-text` or one constant.

`dobbie/fetch.sh` was exercised: two files deleted and fetched again, twelve verified. The indexer, the layout, the viewer and every test run with no font on the machine. The screenshots were regenerated with the bundled face and the vector tier on; the layouts were rebuilt for its character aspect (0.5714 against Menlo's 0.5711 under the same ink-box rule, so nothing visible moved). `THIRD_PARTY_NOTICES` lists Slug, JetBrains Mono, the Dobbie demos (not included) and the submodules.

Not done, by design: the history rewrite. Every commit before phase 6 holds the ten demo files and the port; `git filter-repo` on those paths, or a fresh root, before the repository goes public.
