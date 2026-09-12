# What the public makepad history says about the Studio code atlas

Rik Arends' post of 2026-09-12 ("my full 2.5m line codebase explorer, 120hz") shows the Architecture view of makepad Studio, called the code atlas in commits. This note collects what can be learned from the public repo. The submodule at `makepad/` is pinned at a2fdeb325 (dev, 2026-09-11), and the commit hashes below are in its history.

## Timeline

| Commit | Date | Subject |
|---|---|---|
| 2233cbcdb | 2026-09-06 | Replace legacy Studio with docked and canvas agent workspace |
| b61845f99 | 2026-09-08 | studio: Architecture view, the third workspace mode |
| 313265d20 | 2026-09-08 | Studio code atlas: geometry code views, live filter, 3D size lens, lanes as terminals |
| 9c4e0cc39 | 2026-09-09 | Studio code atlas: performance round: retained uploads, worker labels, exact search, no forks |
| 7235d7e52 | 2026-09-10 | Studio code atlas: stall fix, GPU working set, lens hard switch, filter masks, parallel index |
| 524142a92 | 2026-09-10 | Split Studio into makepad director (public) and makepad scope (private) |

The last commit moved "the code map, its lenses, code views, search and the code_graph/code_atlas/code_view crates" to a closed repo (github.com/makepad/scope, gitignored at apps/scope). Those crates were never in the public tree: the workspace manifest referenced them, but no commit contains their sources. The apps/studio sources that were public are the agent-lane, terminal and iteration tooling, not the map.

## Design statements from the commit messages

From b61845f99: "a zoomable map of the code drawn from the own analyser: Folders as the home projection (repository directories as coloured regions, crates with their module tree inside, size by a Tokens/References metric), Size, Layers (dependency ranks with SCC aggregates) and Landscape (relief prisms by a height metric), all morphing into each other by identity. Continuous zoom with no stage switch: deeper levels fade in inside their parents, code text appears at one line pitch per file in the monospace face from six pixels per line. Pan and zoom are camera transforms over two retained draw lists (tiles, labels) with no geometry regeneration." Also: "Built but not yet wired: the code-splat renderer, the texture baker, ... the gauss ladder backdrops."

From 313265d20: "one geometry CodeView ladder (tile → kind bands → line bars → tokens → text) with wrapped columns, retained instances and machine-derived budgets; live full-text filter with yellow hits at every stage".

From 9c4e0cc39: "per-frame renderer upload budget with liveness counters, worker-shaped labels, camera-only band weights (fills far, outlines near), line-bar LOD, async Metal pipeline creation, ... thread pool sized from core topology with QoS". Index: ".gitignore-exact source inventory, search item budget from the machine".

From 7235d7e52: "retained GPU working set with retirement and device-derived soft bound, morph machinery removed (hard lens switch), label layer and billboards, indexed filter membership and mask pages, parallel deterministic index phases, line-LOD bands".

Reading: the far view in the video is not tiny text. It is the ladder's lower rungs, bars per line coloured by token kind, retained on the GPU as instance buffers and moved only by the camera. Text proper starts at six device pixels per line. The morph between lenses was tried and removed in favour of a hard switch.

## Public engine code that the atlas relies on

All under the submodule, at the pinned commit unless noted.

- `draw/src/text/`: the text stack. Three glyph tiers by device pixels per em: a raster atlas, SDF or MSDF atlases (`MsdfResolutionSettings` in layouter.rs: 32 to 128 dpx/em), and Slug for large text (`slug_atlas.rs`: curves and bands in float textures, evaluated per fragment; Lengyel 2017). `fonts.rs` `should_use_slug_glyph` picks Slug at any size on macOS (threshold 0) and only above the MSDF maximum on Linux and Windows.
- `draw/src/text/async_labels.rs` (added in 9c4e0cc39, 586 lines; removed again by the split): worker-owned label layout and raster preparation with bounded channels, so the UI thread never blocks on text. Read it with `git show 9c4e0cc39:draw/src/text/async_labels.rs`.
- `platform/src/retained_instances.rs` (9c4e0cc39, grown in 7235d7e52 to 844 lines; removed by the split): immutable instance publications with a per-frame retained-upload allowance of 4 MB, scaled down by timing a memcpy at startup. `git show 7235d7e52:platform/src/retained_instances.rs`.
- `platform/src/recording_buffer.rs` (7235d7e52): CPU recording capacity shared between retained publications and worker completions.
- `platform/src/draw_list.rs`: retained draw lists, the two-list camera transform.
- `apps/mpfiles/src/treemap.rs`: the disk map, not the code map, but the same layout philosophy: "pixel-bounded, not depth-bounded", siblings too small to see collapse into one rectangle.
- `libs/code_arch/`: the arch plan format and its guard, public.

## What big-text takes from this

The ladder and the budget, not the code. Rungs chosen by pixels per line, a hard switch between rungs, instance buffers retained on the GPU and moved by the camera alone, an upload budget measured on the machine, and text layout off the UI thread.

## Observed behaviour, from the video

Source: the 43 second clip attached to the tweet, saved as `~/Downloads/code-browser.mp4` (3178 by 1916, 60 fps, so a 2x Retina capture of a roughly 1590 by 958 point window; the 120 Hz claim cannot be checked from a 60 fps file). Frames were sampled at 1 fps for the whole clip and at 5 to 8 fps around the zoom, the search stepping and the tilt. Observations are keyed by time.

### Screen and panels

- Left toolbar, two groups of icons: three lens icons (folders, grid, layers) and four projection icons (flat 2D, cube 3D, tilted landscape, hash grid), then the size metric as a text button ("Tokens") and a palette icon. Once the 3D projection is on, a second metric button appears ("References"), presumably the height metric.
- Right panel, fixed width: a Filter text box, then Inspector / Results / History tabs. The Inspector with nothing selected shows the analyser's coverage for "revision 1 · aarch64-apple-darwin": 620463 known relations, 705150 lexical references recognised, 1782258 unresolved paths, 721803 entities and 1336912 edges, 5947 files indexed, 1953 unattached, 159 boundaries, 15208 opaque macro bodies, 8837 key collisions, 1157839 unresolved in unattached files. The indexing is a real resolver, not a grep.
- Bottom left, a crumb trail ("← makepad › libs › rapier › src › dynamics › solver") that follows the camera: it names the deepest directory under the view centre, not the hovered file.
- Top right, next to the filter box, the current search summary: "119 definitions · 1472 references · 238 files · 4/1591".

### Layout

- A treemap of the repository. Directories are nested rectangles with a small label tag in their top left corner ("rapier/", "git · tests", "model · examples"). Files are rounded rectangles with a one pixel border.
- Wide files do not become wide rectangles. A large file is wrapped into several columns side by side inside its rectangle (parallel_velocity_solver.rs shows as four columns, a windows-rs binding as about twelve), so a file's aspect stays near square and its line pitch stays uniform. This is the "wrapped columns" of the commit message and it is what makes the far view a texture rather than a bar chart.
- Crates with thousands of tiny files (windows-rs bindings, the vulkan headers) show as one pale block of fine vertical stripes: siblings too small to draw collapse into a striped fill. Same rule as the disk map in mpfiles.
- Colour by top-level directory: apps blue, libs teal and green, platform orange and yellow, widgets purple, tools brown, xr pink. The hue is on directory borders and, in 3D, on the walls. Directory borders get thicker with zoom: at file level a directory boundary is a wide lavender or teal band.

### The ladder, as seen

The rung is chosen per file from its size on screen. Approximate switch points, read from the frames at the capture's 2x scale:

| Rung | What is drawn | Seen when a line is about |
|---|---|---|
| tile | flat fill in the directory hue, striped if the file is tiny | under 1 px |
| line bars | one grey bar per line, length proportional to line length; from a distance this is the circuit-board texture | 1 to 3 px |
| tokens | each line is a row of coloured segments, one per token, pastel on dark (keywords blue, types yellow, strings pink, comments green); item boxes appear as outlines (yellow, blue, teal by kind) | 3 to 6 px |
| text | syntax-highlighted monospace text; item outlines remain; the hovered symbol's box is drawn brighter | 6 px and up |

The switch between rungs is a hard cut, not a fade: at 3.0 s a file is coloured token bars and at 3.2 s it is text, with no intermediate frame showing both. The cut is not noticed in motion because the tokens rung is already a colour-accurate low-frequency version of the text. In the tilted 3D view the rung varies across one frame: text at the near edge, bars at the far edge, so the choice is per file (or per column), not per frame.

### Interaction

- Wheel zoom glides. The opening zoom goes from the whole of libs to readable text in about 2.4 s of continuous motion (1.6 s to 4.0 s), and the zoom out takes about 1 s. Zoom is about the cursor.
- Hover shows a label under the cursor with the repo path and, once the file is at the tokens rung or closer, the line and enclosing item: "libs/rapier/src/dynamics/solver/parallel_velocity_solver.rs:205 fn solve". At text level the label becomes symbol-level with counts: "libs/fab/src/model/model.rs:203 struct TextureData · 1 definition · 1 reference". The hovered file gets a light fill, visible from any distance as the bright bar in the overview.
- Search. Typing "Window" in the Filter box populates Results with two lists, Definitions (119, each row: line number, the definition text, its kind such as "struct Window" or "enum ShelfHit", grouped by file) and References (1472, "main_window := Window{ macro script_mod"). On the map every file with a hit gets a yellow outline immediately, at every zoom level, so the overview shows where the symbol lives before you move.
- Stepping through results (the 1/1591 counter) flies the camera: it zooms out to an intermediate height and back in to the hit in about 0.5 to 0.75 s, the van Wijk style zoom-out-and-in rather than a straight line. Each hit is centred at text level with the matching item outlined in yellow. The current result row is highlighted in the panel.
- Tilt. Alt-drag tilts the flat map into perspective over about a second (24.4 s to 25.6 s), and the 3D projection then extrudes files and directories into slabs with shaded walls in the directory hue. Labels stay upright as billboards. Zooming continues to work in 3D down to readable text on the slab tops, with perspective foreshortening pushing the far rows down the ladder. Returning to flat is the same motion reversed.
- Click selects (the Inspector then describes the entity). Not exercised in the clip beyond hover.

### What to copy into big-text, and what to skip

Copy: the treemap with wrapped columns and collapsed siblings; four rungs with hard switches at roughly 1, 3 and 6 px per line; hit outlines on files at every zoom; the crumb trail from the view centre; hover label with path, line and item; the fly-to-result zoom-out-and-in; a tilt that is just a camera change over the same geometry.

Skip for now: the resolver (definitions and references need a real language index; a tokenizer is enough for the tokens rung), the 3D extrusion, the History and lens system.
