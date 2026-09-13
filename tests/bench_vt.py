#!/usr/bin/env python3
"""The vector tier measurement: the Slug tier against the raster tier (the
64 px mipmapped atlas sampled as line.glsl does at the text LOD), on the
default face, at 12, 32, 96 and 300 device pixels per line. The phase 6 run
also measured the Dobbie-style grid tier the Slug tier replaced, taken from
git; that history was rewritten before publication and the port is gone, so
the grid numbers survive only in the README's table. Per contender and size:

  error    mean absolute coverage difference against the exact coverage of
           the same string in the same cells: Pillow's rasterisation at eight
           times the size, box-filtered down, where hinting is negligible (a
           hinted reference at the target size favours the raster tier, which
           is itself a Pillow render); over the whole image and over ink pixels
  time     one full screen (2940 by 1640) of glyph cells at that size drawn
           into an offscreen framebuffer; the best of five batches of fifty
           draws with glFinish, ms per draw (the minimum resists whatever
           else the machine is doing; run it on an idle machine anyway)
  sparkle  the string rendered at a 10 by 10 grid of sub-pixel offsets in
           steps of 0.1 px; for every pair of neighbouring offsets along x,
           the count of pixels whose coverage changes by more than 0.25,
           which a box filter one pixel wide cannot do on a straight edge
           for a 0.1 px shift; the mean over pairs and the maximum

Prints a Markdown table for the README and applies the two rules from
docs/DESIGN.md: VT_MIN_PPL is the smallest tested size at which the Slug
tier's ink error is at or below the raster tier's (64 when none is), and
vector text is on by default when the Slug tier's time per screen at that
size is at most twice the raster tier's.

    ./tests/bench_vt.py [--font F] [--index I] [--out DIR] [--sizes 12,32,96,300]
"""
import argparse
import ctypes
import os
import sys
import time
from pathlib import Path

import glfw
import numpy as np
from OpenGL.GL import *  # noqa: F403
from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))
import atlas_font  # noqa: E402
import vt_glyphs  # noqa: E402
from test_vt_glyphs import VERT, FRAG_HEAD, FRAG_MAIN, compile_program, layout  # noqa: E402
from PIL import ImageDraw, ImageFont  # noqa: E402

SIZES = (12, 32, 96, 300)
TEXTS = ("The quick brown fox 0123 {}[]()", "@&%$#0QOG8B@&%W")
SCREEN = (2940, 1640)
SPARKLE_STEPS = 10
SPARKLE_JUMP = 0.25
SUPERSAMPLE = 8
TIME_REPEATS = 50
TIME_BATCHES = 5


def render_exact(font_path, index, metrics, text, px, ss=SUPERSAMPLE):
    """The exact coverage of the string: Pillow at ss times the size in the
    same cells scaled up, averaged down over ss by ss blocks."""
    w, h, margin, cells = layout(text, px, metrics["char_aspect"])
    em_px = px * ss * metrics["upem"] / (metrics["ascent"] + metrics["descent"])
    font = ImageFont.truetype(font_path, em_px, index=index)
    img = Image.new("L", (w * ss, h * ss), 0)
    draw = ImageDraw.Draw(img)
    baseline = (margin + px * metrics["ascent"] / (metrics["ascent"] + metrics["descent"])) * ss
    for ch, (x0, y0, x1, y1) in zip(text, cells):
        draw.text((x0 * ss, baseline), ch, font=font, fill=255, anchor="ls")
    big = np.asarray(img, np.float64) / 255.0
    return big.reshape(h, ss, w, ss).mean(axis=(1, 3))

RASTER_FRAG = """
uniform sampler2D uGlyphs;
uniform vec4 uGlyph;        // cell_w, cell_h, atlas_w, atlas_h (pixels)
float vt_coverage(int code, vec2 uv, vec2 gdx, vec2 gdy) {
    int g = code - 32;
    if (g < 0 || g > 94) g = 31;
    vec2 cell = vec2(g & 15, g >> 4);
    vec2 scale = uGlyph.xy / uGlyph.zw;
    return textureGrad(uGlyphs, (cell + uv) * scale, gdx * scale, gdy * scale).r;
}
float vt_coverage(int code, vec2 uv) { return vt_coverage(code, uv, dFdx(uv), dFdy(uv)); }
"""


class Contender:
    def __init__(self, name, prog, textures, char_aspect, uniforms=None):
        self.name, self.prog, self.textures, self.char_aspect = name, prog, textures, char_aspect
        glUseProgram(prog)
        for i, (uname, tex) in enumerate(textures):
            glUniform1i(glGetUniformLocation(prog, uname), i)
        for uname, vals in (uniforms or {}).items():
            glUniform4f(glGetUniformLocation(prog, uname), *vals)

    def bind(self):
        glUseProgram(self.prog)
        for i, (_, tex) in enumerate(self.textures):
            glActiveTexture(GL_TEXTURE0 + i)
            glBindTexture(GL_TEXTURE_2D, tex)


class Fbo:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.fbo = glGenFramebuffers(1)
        self.tex = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, self.tex)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
        glBindFramebuffer(GL_FRAMEBUFFER, self.fbo)
        glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, self.tex, 0)
        assert glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE
        glBindFramebuffer(GL_FRAMEBUFFER, 0)

    def read(self):
        buf = glReadPixels(0, 0, self.w, self.h, GL_RGBA, GL_UNSIGNED_BYTE)
        return np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 4)[::-1, :, 0].astype(np.float64) / 255.0

    def delete(self):
        glDeleteFramebuffers(1, [self.fbo])
        glDeleteTextures(1, [self.tex])


def quads(cells, codes):
    verts = []
    for (x0, y0, x1, y1), c in zip(cells, codes):
        quad = [(x0, y0, 0, 0), (x1, y0, 1, 0), (x1, y1, 1, 1), (x0, y1, 0, 1)]
        for k in (0, 1, 2, 0, 2, 3):
            verts.append(quad[k] + (float(c),))
    return np.array(verts, np.float32)


def draw(contender, fbo, vao, vbo, verts, repeat=1):
    """Draw the quads into the FBO `repeat` times; returns the ms per draw."""
    glBindFramebuffer(GL_FRAMEBUFFER, fbo.fbo)
    glViewport(0, 0, fbo.w, fbo.h)
    contender.bind()
    glUniform2f(glGetUniformLocation(contender.prog, "uSize"), fbo.w, fbo.h)
    glBindVertexArray(vao)
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    glBufferData(GL_ARRAY_BUFFER, verts.nbytes, verts, GL_STREAM_DRAW)
    glFinish()
    t0 = time.perf_counter()
    for _ in range(repeat):
        glClearColor(0, 0, 0, 1)
        glClear(GL_COLOR_BUFFER_BIT)
        glDrawArrays(GL_TRIANGLES, 0, len(verts))
    glFinish()
    return (time.perf_counter() - t0) * 1000.0 / repeat


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--font", default=vt_glyphs.DEFAULT_FONT)
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--out", default=None, help="directory for the rendered PNGs")
    ap.add_argument("--min-ppl", type=float, default=12.0, help="the new tier's band widening")
    ap.add_argument("--max-bands", type=int, default=vt_glyphs.MAX_BANDS, help="the new tier's band count ceiling")
    ap.add_argument("--sizes", default=None, help="comma-separated sizes to test (default 12,32,96,300)")
    args = ap.parse_args()
    out = Path(args.out) if args.out else None
    if out:
        out.mkdir(parents=True, exist_ok=True)

    if not glfw.init():
        raise RuntimeError("glfw.init failed")
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    win = glfw.create_window(64, 64, "bench_vt", None, None)
    glfw.make_context_current(win)
    glDisable(GL_BLEND)
    glDisable(GL_DEPTH_TEST)
    vao = glGenVertexArrays(1)
    glBindVertexArray(vao)
    vbo = glGenBuffers(1)
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    stride = 5 * 4
    for loc, n, off in ((0, 2, 0), (1, 2, 8), (2, 1, 16)):
        glEnableVertexAttribArray(loc)
        glVertexAttribPointer(loc, n, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(off))

    box = atlas_font.font_box(args.font, args.index)[:2]

    # the new tier
    curves, bands, metrics = vt_glyphs.build_atlas(args.font, args.index, args.min_ppl, box=box, verbose=True,
                                                   max_bands=args.max_bands)
    sizes = tuple(int(v) for v in args.sizes.split(",")) if args.sizes else SIZES
    tc, tb = vt_glyphs.make_textures(curves, bands)
    new_glsl = (ROOT / "shaders" / "vt_glyph.glsl").read_text()
    contenders = [Contender("slug", compile_program(VERT, FRAG_HEAD + new_glsl + FRAG_MAIN),
                            [("vt_curves", tc), ("vt_bands", tb)], metrics["char_aspect"])]
    # the raster tier
    glyphs, gm = atlas_font.build_atlas(args.font, args.index, 64)
    rt = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, rt)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_R8, glyphs.shape[1], glyphs.shape[0], 0, GL_RED, GL_UNSIGNED_BYTE, glyphs)
    glGenerateMipmap(GL_TEXTURE_2D)
    contenders.append(Contender("raster", compile_program(VERT, FRAG_HEAD + RASTER_FRAG + FRAG_MAIN),
                                [("uGlyphs", rt)], gm["char_aspect"],
                                uniforms={"uGlyph": (gm["cell_w"], gm["cell_h"], glyphs.shape[1], glyphs.shape[0])}))
    A = metrics["char_aspect"]

    rng = np.random.default_rng(1)
    results = {}                      # (name, size) -> dict
    screen = Fbo(*SCREEN)
    for px in sizes:
        text = TEXTS[0] + " " + TEXTS[1]
        w, h, margin, cells = layout(text, px, A)
        ref = render_exact(args.font, args.index, metrics, text, px)
        fbo = Fbo(w, h)
        # a full screen of cells for the timing
        cols, rows = max(1, int(SCREEN[0] / (px * A))), max(1, int(SCREEN[1] / px))
        codes = rng.integers(33, 127, size=cols * rows)
        scells = [(c * px * A, r * px, (c + 1) * px * A, (r + 1) * px) for r in range(rows) for c in range(cols)]
        sverts = quads(scells, codes)
        for ct in contenders:
            verts = quads(cells, [ord(ch) for ch in text])
            draw(ct, fbo, vao, vbo, verts)
            img = fbo.read()
            if out:
                Image.fromarray((img * 255 + 0.5).astype(np.uint8)).save(out / f"{ct.name}_{px}.png")
            diff = np.abs(img - ref)
            ink = (ref > 0) | (img > 0)
            err_all, err_ink = float(diff.mean()), float(diff[ink].mean()) if ink.any() else 0.0
            draw(ct, screen, vao, vbo, sverts, repeat=2)                    # warm up
            ms = min(draw(ct, screen, vao, vbo, sverts, repeat=TIME_REPEATS) for _ in range(TIME_BATCHES))
            # sparkle: a grid of sub-pixel offsets
            counts = []
            for dy in range(SPARKLE_STEPS):
                prev = None
                for dx in range(SPARKLE_STEPS):
                    off = (dx / SPARKLE_STEPS, dy / SPARKLE_STEPS)
                    oc = [(x0 + off[0], y0 + off[1], x1 + off[0], y1 + off[1]) for x0, y0, x1, y1 in cells]
                    draw(ct, fbo, vao, vbo, quads(oc, [ord(ch) for ch in text]))
                    cur = fbo.read()
                    if prev is not None:
                        counts.append(int((np.abs(cur - prev) > SPARKLE_JUMP).sum()))
                    prev = cur
            results[(ct.name, px)] = {"err_all": err_all, "err_ink": err_ink, "ms": ms,
                                      "sparkle_mean": float(np.mean(counts)), "sparkle_max": int(np.max(counts)),
                                      "cells": cols * rows}
            print(f"{ct.name:>6} {px:4d} px: error {err_all:.4f} all, {err_ink:.4f} ink; {ms:7.2f} ms per screen "
                  f"of {cols * rows} cells; sparkle {np.mean(counts):.1f} mean, {np.max(counts)} max")
        fbo.delete()
        if out:
            Image.fromarray((ref * 255 + 0.5).astype(np.uint8)).save(out / f"exact_{px}.png")
            # the renders stacked and enlarged, as evidence
            scale = max(1, 72 // px)
            imgs = [Image.open(out / f"{n}_{px}.png") for n in ("slug", "raster")] + [Image.open(out / f"exact_{px}.png")]
            w0, h0 = imgs[0].size
            w1 = min(w0, 2000 // scale)
            stack = Image.new("L", (w1 * scale, (h0 * 3 + 8) * scale), 40)
            for i, im in enumerate(imgs):
                stack.paste(im.crop((0, 0, w1, h0)).resize((w1 * scale, h0 * scale), Image.NEAREST), (0, (h0 + 4) * i * scale))
            stack.save(out / f"stack_{px}.png")
    screen.delete()
    glfw.terminate()

    print("\n| px per line | tier | error, all | error, ink | ms per screen | cells per screen | sparkle mean | sparkle max |")
    print("|---|---|---|---|---|---|---|---|")
    for px in sizes:
        for name in ("slug", "raster"):
            r = results[(name, px)]
            print(f"| {px} | {name} | {r['err_all']:.4f} | {r['err_ink']:.4f} | {r['ms']:.2f} | {r['cells']} | "
                  f"{r['sparkle_mean']:.1f} | {r['sparkle_max']} |")

    # the rules
    handoff = next((px for px in sizes if results[("slug", px)]["err_ink"] <= results[("raster", px)]["err_ink"]), None)
    vt_min = handoff if handoff is not None else 64
    ref_px = handoff if handoff is not None else sizes[-1]
    default_on = results[("slug", ref_px)]["ms"] <= 2.0 * results[("raster", ref_px)]["ms"]
    print(f"\nVT_MIN_PPL = {vt_min} (the smallest tested size where slug's ink error <= raster's"
          f"{'' if handoff else '; none did, so the raster atlas cell size'})")
    print(f"vector text {'on' if default_on else 'off'} by default (slug {results[('slug', ref_px)]['ms']:.2f} ms vs raster "
          f"{results[('raster', ref_px)]['ms']:.2f} ms per screen at {ref_px} px; the rule is at most twice)")


if __name__ == "__main__":
    main()
