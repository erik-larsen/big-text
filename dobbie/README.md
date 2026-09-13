# Will Dobbie's GPU text demos

Two WebGL demos by Will Dobbie, copied from his site and verified byte for byte. He never published a repository; the demos are served unminified, so the page source is the source.

| Post | Date | Demo | Here |
|---|---|---|---|
| [GPU text rendering with vector textures](https://wdobbie.com/post/gpu-text-rendering-with-vector-textures/) | 2016-01-02 | [wdobbie.com/pdf/](https://wdobbie.com/pdf/) (Agner Fog's optimizing_cpp.pdf, 164 pages) | `pdf/` |
| [Implementation notes](https://wdobbie.com/post/gpu-text-rendering-with-vector-textures-details/) | 2016 | same demo | |
| [War and Peace and WebGL](https://wdobbie.com/post/war-and-peace-and-webgl/) | 2016-01-21 | [wdobbie.com/warandpeace/](https://wdobbie.com/warandpeace/) (1273 pages, 2.7 million glyphs) | `warandpeace/` |

A third-party mirror of the War and Peace demo exists at github.com/amilajack/webgl-sdf-demo (2021). Its files are byte-identical to the live site except for whitespace in index.html; its package.json says MIT, but that is the mirror author's claim, not Dobbie's. The copies here were taken from wdobbie.com directly on 2026-09-12.

## Files

Nothing here is committed: `fetch.sh` downloads every file from wdobbie.com into `pdf/` and `warandpeace/` (both ignored by git) and verifies it by MD5; `fetch.sh --check` verifies what is present. The repository keeps this README and the script.

| File | Bytes | MD5 |
|---|---|---|
| pdf/index.html | 26775 | 9109460ef4a90aa3fce67bb08b6e4ae2 |
| pdf/web.vert, pdf/font.frag (the shaders, external files in this older demo) | 1547, 6862 | 7c7da6be7e3795b3e6303e403d6cbd71, 82d00c49e53fc59d99e9ab18d730a865 |
| pdf/atlas.bmp (vector atlas: curves and grid cells) | 131126 | 961e515f0ea83bf713f2667b7c1fa8a1 |
| pdf/pages.json (page rectangles and vertex ranges) | 24221 | 8acc99caa96d5f1f4b7286ca11f929f6 |
| pdf/vertices.bmp | 18539094 | b5c611b92e8d52799351f4331b974b9b |
| warandpeace/index.html | 44444 | ec7be2093ce4a9e4bf687340a07d8e48 |
| warandpeace/atlas.bmp | 131126 | b304e18f300400a0168d24dfc409316c |
| warandpeace/atlasverts.bmp (one quad per glyph, to prerender the raster atlas) | 14454 | 26aeda3a3c027836d4ed83da2d73ebb5 |
| warandpeace/glyphs.bmp (every glyph of the book, one static vertex buffer) | 53507434 | 1387b2d27aba95d3ffd9f1fce49fa25e |
| warandpeace/imageverts.bmp | 54 | 993342c3683d8fe03993fd0d8f237e15 |
| warandpeace/pages.json | 191716 | 7def81687e71d65b7f0849814658c788 |

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

What is not here: the C++ preprocessor that turned a PDF into atlas, vertex buffer and JSON. Dobbie describes it but did not release it. big-text's own is `vt_glyphs.py` at the repo root, which builds Slug-style band data from a font with fontTools for the shader in `shaders/vt_glyph.glsl`.

## Licence

None published. The technique, the formats and the tier design are described in his posts and are free to reimplement, which big-text does; the files are his copyright, so they are fetched for study and parity testing and never redistributed. big-text's own vector tier (`vt_glyphs.py`, `shaders/vt_glyph.glsl`) is written from his posts and Eric Lengyel's MIT reference shaders, not from these files.
