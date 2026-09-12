# big-text

Draft. This README is the design document; nothing here is settled until we have gone through it together.

big-text is the text-scale sibling of [big-picture](https://github.com/erik-larsen/big-picture): view millions of lines of source code, or a whole book, as one continuous surface. Zoom out and a codebase looks like a circuit board; zoom in and every glyph is crisp. No page turns, no mode switches, one camera.

It combines three lineages, each vendored or pinned in this repo:

| Lineage | What it contributes | Where |
|---|---|---|
| big-picture (Erik Larsen, 2026) | Tile pyramid, virtual texturing, feedback pass, streaming budget, mosaic layout from a folder tree, hover and click on the manifest, the globe | `big-picture/` (submodule, pinned) |
| Vector textures and War and Peace (Will Dobbie, 2016) | Glyph outlines stored as bezier curves in a texture and ray-cast per pixel, so text is crisp at any magnification; a prerendered mipmapped atlas takes over when minified; 2.7 million glyphs of War and Peace as one static vertex buffer | `dobbie/` (verified copies of the live demos) |
| Studio code atlas (Rik Arends, makepad, Sep 2026) | Semantic level of detail: one ladder from file tile to kind bands to line bars to tokens to text, chosen by pixels per line; a retained GPU working set with a device-derived budget; label layout on worker threads | `makepad/` (submodule, pinned) plus `notes/makepad-code-atlas.md` |
| dagcmp (Erik Larsen, 2026) | The filesystem as a corpus: a whole NTFS volume scanned from the Master File Table in seconds, a tree model with per-directory aggregates, and a compare engine that gives every node a status | `dagcmp/` (submodule, pinned) |

The code atlas's own crates (code_graph, code_atlas, code_view) are in a private makepad repo. What we have is the public engine underneath it and the commit messages that describe the design. big-text reimplements the ladder; it does not port their code.

## The idea in one paragraph

big-picture draws a gigapixel image with a fixed GPU budget by streaming tiles from a pyramid. Text breaks the pyramid: a downsampled page of code is grey mush, and a magnified tile is blurry. So big-text keeps the streaming and the camera but replaces the pyramid with a ladder of representations. Far away a file is a coloured tile. Closer it is one bar per line, coloured by token kind, which is what makes a codebase look like a die shot. Closer still it is real glyphs, drawn from a raster atlas while they are small and from bezier curves once they are large. Each file picks its rung from its size in pixels, and the viewer streams only the rungs it can see.

## Questions to settle first

1. Corpus and layout. Source trees, books, or whole filesystems? A source tree lays out like big-picture's photo mosaic: files are the pictures, directories the grouping, and a manifest gives hover and click. A book is 1273 pages in a grid. Both fit one layout stage if a "document" is just a rectangle with lines. A filesystem is the third shape: millions of nodes where most files have no text to show, so the ladder bottoms out at the treemap rung (makepad's mpfiles disk map does exactly this: pixel-bounded layout, siblings too small to see collapse into one block). dagcmp already scans and models such trees, and its compare statuses would colour a two-volume map the way token kinds colour code. Proposal: one layout stage where a document is a rectangle with lines, and a directory is a rectangle of documents; test with makepad's own repo (about 3 million lines), War and Peace, and a dagcmp scan of a full volume.
2. The glyph tier. Three candidates: Dobbie's curve ray-casting (exact, any magnification, heavy fragment shader), signed-distance atlases (cheap, soft corners when very large), or Lengyel's Slug as makepad uses (curves plus bands, the modern form of Dobbie's idea). Proposal: start with Dobbie's, because his shaders exist, run in WebGL 1, and his fallback to a mipmapped raster atlas is exactly the small-text tier we need anyway.
3. Atlas generation. Dobbie's preprocessor (a C++ program that reads a PDF and writes a vector atlas, a vertex buffer and a JSON file) was never published. We need our own: read a font with fontTools, convert outlines to quadratic beziers, dice each glyph into the grid cells his shader expects, and write the texture. This is the first real code in the repo and it decides whether the rest is possible.
4. Platform. big-picture is Python plus a C port; Dobbie is WebGL; makepad is Rust on Metal. Proposal: Python with OpenGL for the prototype, reusing big-picture's viewer skeleton, with every shader written to GLSL ES 1.0 so the same code runs in the C port and in a browser. The browser is where a code browser gets shared.
5. Do we keep a pyramid at all? For the two far rungs, yes, possibly: a tile pyramid whose coarse levels are generated from the source (bands, bars) rather than downsampled from the fine ones. That would let big-picture's viewer stream the ladder unchanged. Whether that beats drawing bars as instanced quads is an open question.
6. Budget and eviction. big-picture streams 256 by 256 pixel pages with an LRU. big-text streams per-file instance buffers whose size depends on the rung. makepad measures the machine at startup to size its upload budget. Which unit do we budget in: bytes uploaded per frame, glyphs resident, or both?

## Provenance and licences

- big-picture, dagcmp: MIT.
- makepad: MIT or Apache-2.0. Only the public repo is included, pinned at commit a2fdeb325 (dev, 2026-09-11).
- Dobbie's demos: no licence has been published, on the blog or with the files. The copies under `dobbie/` are here for study and as a reference implementation to test against. Until Will Dobbie says otherwise, this repo should stay private, or `dobbie/` should be excluded before it is published. Details, sources and checksums are in `dobbie/README.md`.

## Layout

```
big-text/
  README.md                    this file
  notes/makepad-code-atlas.md  what the public makepad history says about the code atlas
  dobbie/                      the two live demos, verified against wdobbie.com
  big-picture/                 submodule, pinned
  dagcmp/                      submodule, pinned
  makepad/                     submodule, pinned
```

Two large binaries (the War and Peace glyph buffer, 53 MB, and the PDF demo's vertex buffer, 18 MB) are ignored by git. `dobbie/fetch.sh` downloads them from wdobbie.com and checks their checksums.
