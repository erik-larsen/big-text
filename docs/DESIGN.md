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
