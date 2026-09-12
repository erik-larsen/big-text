# Parity with the Studio code atlas

The plan for reaching everything the video and the makepad commit messages show, as a checklist. Each row is one observation from `notes/makepad-code-atlas.md`, its state in the current build (commit 3634cc1, 2026-09-12), and the phase that closes it. A phase is done when every row it owns is checked against a screenshot or a scripted test, not when the code exists. The phases are ordered so each unlocks the next; sizes are single-session, sequential work.

## Checklist

| # | Observation (video or commit) | Now | Phase |
|---|---|---|---|
| 1 | Treemap of the repo: nested directory rectangles, label tags at corners, hue per top-level directory on borders | done | |
| 2 | Files wrapped into columns so every file stays near square with one line pitch | done | |
| 3 | Long lines wrapped inside the column (visible in the video's text views), not clipped | done (continuation rows hang in by two characters) | |
| 4 | Crates of thousands of tiny files show as one striped block | done (emerges) | |
| 5 | Directory bands widen with zoom | done | |
| 6 | Single-child chains collapse into one tag ("git · tests") | done | |
| 7 | Ladder rung: tile under about 1 px per line | done (sampled bars) | |
| 8 | Ladder rung: kind bands, items as filled bands by kind at a distance ("fills far, outlines near") | done (bands below 3 px, outlines above) | |
| 9 | Ladder rung: grey line bars 1 to 3 px | done | |
| 10 | Ladder rung: token-coloured segments with item outlines 3 to 6 px | done | |
| 11 | Ladder rung: text from 6 px, hard cuts, per-file rung within one frame | done | |
| 12 | Vector-crisp text at large magnification | done, behind --vector-text | |
| 13 | Wheel zoom glides about the cursor | done | |
| 14 | Left or right drag pans | done | |
| 15 | Hover: light fill on the file at every distance | done | |
| 16 | Hover label: path, then line and enclosing item at the tokens rung and closer | done | |
| 17 | Hover label at text rung: symbol with "N definitions · M references" | done | |
| 18 | Filter box: typing outlines every hit file in yellow at any zoom, dims the rest | done | |
| 19 | Results: Definitions with line, text and kind; References grouped by file with context | done: entities and resolved references from the resolver, statuses on the rest | |
| 20 | Search summary "D definitions · R references · F files · i/N" | done | |
| 21 | Step to a result: van Wijk zoom-out-and-in, hit outlined and filled, row highlighted | done | |
| 22 | Inspector tab: entity details on click, coverage statistics of the index | done | |
| 23 | Click selects, Shift-click toggles, Shift-drag marquees, selection lights its neighbourhood | done (neighbourhood from the file graph, both directions; Inspector follows) | |
| 24 | Toolbar: lens group, projection group, metric buttons (Tokens, References), palette, legend toggle | text buttons for lens, projection (2D, 3D) and metric, drawn over the map; no palette or legend | later (palette, legend) |
| 25 | Area metric switchable: Tokens (default), References | done: Tokens, References, Lines, a hard cut between precomputed layouts | |
| 26 | Size lens in 2D, 2.5D and 3D; hard lens switch | done: 2D and 3D, hard switch; 2.5D (perspective without heights) not separate | |
| 27 | Alt-drag tilts; 3D extrudes files and directories by the height metric (References) with shaded walls in the hue; labels as billboards; the ladder still draws on slab tops | done (heights from whichever metric is selected; directories as depth terraces) | |
| 28 | Layers lens: dependency ranks with SCC aggregates and rank labels | done (rank rows, cycles grouped, rank labels with file counts) | |
| 29 | History lens: revision scrub rail, spatial diff, change lighting | missing | 5 |
| 30 | Crumb trail follows the view centre | done | |
| 31 | Two retained draw lists moved by the camera alone, no regeneration on pan or zoom | done (resident textures, per-run draws) | |
| 32 | Streaming working set with a device-derived budget for corpora beyond resident size | missing; 3.7 M lines resident is fine | 6 |
| 33 | Parallel deterministic index with progress | done (4 s for 3.7 M lines) | |
| 34 | Search index served to agents over MCP with byte-admitted pages | missing | 7 |
| 35 | Disk mode (a filesystem as the corpus) | missing | 7 |
| 36 | 120 Hz | 60 Hz vsync, 4.5 ms per frame on full makepad | not pursued |

## Phases

**Phase 0, quick wins (half a session, done in commit after 41ed298).** Close rows 8 and the first half of 19 without new dependencies: a kind-bands rung that draws item rectangles filled at low alpha in their kind colour from 1 px per line (outlines take over from 3 px, which is the "fills far, outlines near" of the commit message); references exclude hits whose character kind is comment or string, using the kinds texture already built. Add a Tokens metric to atlas_layout.py (runs of non-space kinds) so the default area metric matches the video's. Verify: makepad-draw at 2 px per line shows coloured bands; the `Window` filter drops from 148 to 130 references.

**Phase 1, resolver and Inspector (done).** Rows 17, 19, 22, 25. Parse with tree-sitter (one pip dependency plus the Rust, Python and C grammars); per file collect items with kinds and scopes, then resolve identifier uses: same scope, then the file, then `use` paths within the crate, then a unique global name; the rest are ambiguous or not found and counted as such. Output per-file definition and reference tables into the atlas and a per-file reference count (fan-in) as the References metric. The Inspector tab shows the selected or hovered entity with its definitions and references, and the index coverage (files, entities, references resolved, ambiguous, not found) in the style of Rik's panel. Parity means the same categories, not his numbers: his analyser is bigger and private. Verify: `Window` on makepad-draw lists the `pub type Window = XID` definition and code references only, grouped by file with kinds; hovering a symbol at text zoom shows its counts.

**Phase 2, metrics, lenses, wrapping (done).** Rows 3, 24, 25, 26 in 2D. A toolbar of text buttons; atlas_layout.py runs once per metric and the viewer swaps layouts with a hard cut; long lines wrap inside their column (the layout emits visual rows, the viewer maps hover back to logical lines). Verify: switching Tokens to References re-lays the map; a 200-column line reads as two rows at text zoom.

**Phase 3, 3D projection (done).** Rows 26 and 27. A perspective camera with tilt and orbit on Alt-drag, a per-file and per-directory height from the current metric, wall quads shaded in the hue, a depth buffer, labels as billboards, and the existing ladder drawn on the slab tops with the rung still chosen per file from its pixels per line (so text at the near edge, bars at the far edge, as in the video). Verify against frames 25 to 35 of the video.

**Phase 4, Layers lens and selection (done).** Rows 23 and 28. The module graph from the resolver's `use` and `mod` edges, strongly connected components condensed, longest-path ranks as rows; click, Shift-click and marquee selection with the neighbourhood lit and the Inspector following.

**Phase 5, History lens (1 session).** Row 29. `git log --numstat` per file for churn, a scrub rail that re-indexes at a chosen commit (4 s for all of makepad, so live enough), change lighting between two revisions, churn as a colour lens. This is also the recency heatmap from the lenses discussion.

**Phase 6, scale (1 to 2 sessions).** Row 32. Per-file instance ranges with a byte budget, retirement of files off screen, so a corpus past ten million lines works. Only if a corpus that needs it exists.

**Phase 7, optional (1 session each).** Rows 34 and 35: an MCP server that serves the index at the ladder's rungs under byte budgets; a filesystem indexer over a dagcmp scan.

## Ground rules

- One phase per session. Each phase starts with its DESIGN.md contract and README paragraph, agreed before code, and ends with screenshots checked against the video frames and a commit.
- Python and OpenGL throughout. Nothing above needs Rust for parity at makepad's size.
- tree-sitter is the only new dependency, added in phase 1 with explicit approval.
- Lenses are precomputed layouts swapped with a hard cut, which is also what Rik settled on after removing his morph.
- No parallel agents unless a phase has a separable module and the spend is agreed first.

## Where things stand (2026-09-12, after phase 4)

Done: phases 0 to 4, rows 1 to 28 except the palette and legend buttons of row 24; 30 of the 36 rows are checked. Next: phase 5 (History), then 6 (streaming) and 7 (MCP, disk mode). Not pursued: 120 Hz, 2.5D as a mode of its own.

To resume in a fresh clone (the generated data is not in git):

```bash
pip install numpy Pillow PyOpenGL glfw fonttools tree-sitter tree-sitter-rust tree-sitter-python tree-sitter-c
git submodule update --init big-picture makepad
./atlas_index.py big-picture --out data/big-picture_atlas
./atlas_index.py makepad/draw makepad/platform --out data/makepad-draw_atlas
./atlas_resolve.py data/big-picture_atlas
./atlas_resolve.py data/makepad-draw_atlas
./atlas_layout.py data/big-picture_atlas
./atlas_layout.py data/makepad-draw_atlas
./tests/gen_synthetic_atlas.py
./tests/check_layout.py data/makepad-draw_atlas && ./tests/test_viewer_input.py && ./tests/test_vt_glyphs.py
./atlas_viewer.py data/makepad-draw_atlas --shots /tmp/shots --filter Window --stats
```

Each phase so far: its contract was written into `docs/DESIGN.md` first ("as built" sections at the end), the code was patched in the main session with anchored edits, every behaviour was verified by a screenshot taken with the viewer's flags (`--goto`, `--zoom`, `--filter`, `--step`, `--inspect`, `--inspector`, `--hover`, `--select`, `--lens`, `--metric`, `--proj --tilt --yaw`, `--shots`) and read back, then `docs/shots/README.md`, the README and this file were updated and the phase was committed and pushed. Keep that shape.

Known rough edges, none blocking: the toolbar, status line and filter box overlay the map's corners rather than sitting in their own strip; in 3D the fitted view is a fixed tilt and yaw (55, 12 in the shots) and the labels of far directories can crowd; the Layers lens has no crossing minimisation within a row; the resolver's `ambiguous` and `not found` counts are large because there is no type inference (by design); the vector glyph tier is off unless `--vector-text` is given and switches at a fixed 40 px per line; screenshots differ by two pixels of framebuffer height between runs on this Mac.

Phase 5 starts with a DESIGN.md contract for: `atlas_history.py` (git log --numstat per file into churn and recency arrays, and a list of revisions), a churn colour lens (a per-file colour texture, the same mechanism as the hues), the revision rail (re-index at a commit into a separate atlas directory and switch like a layout), and change lighting between two revisions (files added, removed, changed).
