# Will Dobbie's GPU text demos

Two WebGL demos by Will Dobbie, copied from his site and verified byte for byte. He never published a repository; the demos are served unminified, so the page source is the source.

| Post | Date | Demo | Here |
|---|---|---|---|
| [GPU text rendering with vector textures](https://wdobbie.com/post/gpu-text-rendering-with-vector-textures/) | 2016-01-02 | [wdobbie.com/pdf/](https://wdobbie.com/pdf/) (Agner Fog's optimizing_cpp.pdf, 164 pages) | `pdf/` |
| [Implementation notes](https://wdobbie.com/post/gpu-text-rendering-with-vector-textures-details/) | 2016 | same demo | |
| [War and Peace and WebGL](https://wdobbie.com/post/war-and-peace-and-webgl/) | 2016-01-21 | [wdobbie.com/warandpeace/](https://wdobbie.com/warandpeace/) (1273 pages, 2.7 million glyphs) | `warandpeace/` |

A third-party mirror of the War and Peace demo exists at github.com/amilajack/webgl-sdf-demo (2021). Its files are byte-identical to the live site except for whitespace in index.html; its package.json says MIT, but that is the mirror author's claim, not Dobbie's. The copies here were taken from wdobbie.com directly on 2026-09-12.

## Files

| File | Bytes | MD5 | In git |
|---|---|---|---|
| warandpeace/index.html | 44444 | | yes |
| warandpeace/atlas.bmp (vector atlas: curves and grid cells) | 131126 | b304e18f300400a0168d24dfc409316c | yes |
| warandpeace/atlasverts.bmp (one quad per glyph, to prerender the raster atlas) | 14454 | | yes |
| warandpeace/glyphs.bmp (every glyph of the book, one static vertex buffer) | 53507434 | 1387b2d27aba95d3ffd9f1fce49fa25e | no, run fetch.sh |
| warandpeace/imageverts.bmp | 54 | | yes |
| warandpeace/pages.json (page rectangles and vertex ranges) | 191716 | | yes |
| pdf/index.html | 26775 | | yes |
| pdf/web.vert, pdf/font.frag (the shaders, external files in this older demo) | 1547, 6862 | | yes |
| pdf/atlas.bmp | 131126 | 961e515f0ea83bf713f2667b7c1fa8a1 | yes |
| pdf/vertices.bmp | 18539094 | b5c611b92e8d52799351f4331b974b9b | no, run fetch.sh |
| pdf/pages.json | 24221 | | yes |

The .bmp files are not images: Dobbie uses BMP as a container so browsers of 2016 would download binary data without fuss. `unpackBmp` in index.html strips the header. The PDF demo also requests `imagevertices.bmp`, which returns 404 on the live site too; the demo tolerates it.

## Running

```bash
cd dobbie && python3 -m http.server 8000
```

Then open http://localhost:8000/warandpeace/ or http://localhost:8000/pdf/. Drag pans, right-drag or alt-drag zooms, and the toolbar has a Grids checkbox that shows the vector atlas cells and a Vector only checkbox that disables the raster fallback so you can see the small-text shimmer it fixes.

## How it works, briefly

- The atlas texture holds every distinct glyph once: its quadratic bezier control points as 16-bit coordinates split across RGBA channels, and a grid over the glyph where each cell lists the indices of the curves crossing it (four one-byte indices per texel, a second grid when a cell needs more).
- Each glyph on the page is one quad. Its vertices carry a position, a colour and the atlas coordinates of its glyph; the grid position and size are read from the atlas in the vertex shader.
- The fragment shader finds the grid cell under the pixel, fetches its curves, and casts rays along four rotated directions, solving each quadratic analytically and integrating a parabolic filter window along the ray. The result is exact coverage at any magnification with no precomputed distance field.
- War and Peace adds the small-text tier: at load, every glyph is drawn once through the vector shader into a framebuffer, which is mipmapped. In the fragment shader, when the glyph is minified past two texels per pixel, it samples that raster atlas with five taps instead of ray casting. This removed the shimmer of the first demo.
- The whole book is pre-laid out: no text layout happens in the browser. A page is one draw call over a vertex range, and pages outside the view are skipped.

What is not here: the C++ preprocessor that turned a PDF into atlas, vertex buffer and JSON. Dobbie describes it but did not release it. big-text's own is `vt_glyphs.py` at the repo root, which builds the same kind of atlas from a font with fontTools, with the ray-cast ported to GLSL 330 in `shaders/vt_glyph.glsl`.

## Licence

None published. These files are included for study and as a reference implementation. Do not redistribute this directory without asking Will Dobbie.
