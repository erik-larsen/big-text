#!/usr/bin/env python3
"""Self-check for the vector-texture glyph tier (vt_glyphs.py + shaders/vt_glyph.glsl).

Opens a hidden glfw window, renders a test string at 12, 32, 96 and 300 device
pixels per line into an offscreen framebuffer through vt_coverage(), saves the
renderings as PNGs, and compares the 96 pixel one against Pillow's FreeType
rasterisation of the same string, font and cell size. Prints the mean
absolute coverage difference; passes (exit 0) when it is below 0.06.

  ./tests/test_vt_glyphs.py [--font Menlo.ttc] [--index 0] [--grid 12] [--out data/vt_test]
"""
import argparse
import ctypes
import sys
import time
from pathlib import Path

import glfw
import numpy as np
from OpenGL.GL import *  # noqa: F403
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import vt_glyphs  # noqa: E402

TEXT = "The quick brown fox 0123 {}[]()"
SIZES = (12, 32, 96, 300)
THRESHOLD = 0.06

VERT = """#version 330 core
layout(location=0) in vec2 aPos;     // device pixels, y down
layout(location=1) in vec2 aUV;
layout(location=2) in float aCode;
uniform vec2 uSize;
out vec2 vUV;
flat out int vCode;
void main() {
    vUV = aUV;
    vCode = int(aCode + 0.5);
    gl_Position = vec4(aPos.x / uSize.x * 2.0 - 1.0, 1.0 - aPos.y / uSize.y * 2.0, 0.0, 1.0);
}
"""

FRAG_HEAD = "#version 330 core\n"
FRAG_MAIN = """
in vec2 vUV;
flat in int vCode;
out vec4 frag;
void main() {
    frag = vec4(vec3(vt_coverage(vCode, vUV)), 1.0);
}
"""


def compile_program(vs_src, fs_src):
    prog = glCreateProgram()
    for kind, src in ((GL_VERTEX_SHADER, vs_src), (GL_FRAGMENT_SHADER, fs_src)):
        sh = glCreateShader(kind)
        glShaderSource(sh, src)
        glCompileShader(sh)
        if not glGetShaderiv(sh, GL_COMPILE_STATUS):
            raise RuntimeError(glGetShaderInfoLog(sh).decode())
        glAttachShader(prog, sh)
    glLinkProgram(prog)
    if not glGetProgramiv(prog, GL_LINK_STATUS):
        raise RuntimeError(glGetProgramInfoLog(prog).decode())
    return prog


def layout(text, pitch, char_aspect):
    """Image size, margin and per-character cell rectangles for one line."""
    cell_w = pitch * char_aspect
    margin = round(pitch * 0.25) + 2
    w = int(np.ceil(len(text) * cell_w)) + 2 * margin
    h = pitch + 2 * margin
    cells = [(margin + i * cell_w, margin, margin + (i + 1) * cell_w, margin + pitch) for i in range(len(text))]
    return w, h, margin, cells


def render_gl(prog, vao, vbo, atlas_tex, text, pitch, char_aspect):
    """Coverage image [h, w] float in 0..1 from the vector shader."""
    w, h, margin, cells = layout(text, pitch, char_aspect)
    verts = []
    for ch, (x0, y0, x1, y1) in zip(text, cells):
        c = float(ord(ch))
        quad = [(x0, y0, 0, 0), (x1, y0, 1, 0), (x1, y1, 1, 1), (x0, y1, 0, 1)]
        for k in (0, 1, 2, 0, 2, 3):
            verts.append(quad[k] + (c,))
    verts = np.array(verts, np.float32)

    fbo = glGenFramebuffers(1)
    tex = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, tex)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA8, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, None)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST)
    glBindFramebuffer(GL_FRAMEBUFFER, fbo)
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0)
    assert glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE
    glViewport(0, 0, w, h)
    glClearColor(0, 0, 0, 1)
    glClear(GL_COLOR_BUFFER_BIT)

    glUseProgram(prog)
    glActiveTexture(GL_TEXTURE0)
    glBindTexture(GL_TEXTURE_2D, atlas_tex)      # the FBO texture above took unit 0
    glUniform2f(glGetUniformLocation(prog, "uSize"), w, h)
    glBindVertexArray(vao)
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    glBufferData(GL_ARRAY_BUFFER, verts.nbytes, verts, GL_STREAM_DRAW)
    t0 = time.time()
    glDrawArrays(GL_TRIANGLES, 0, len(verts))
    glFinish()
    ms = (time.time() - t0) * 1000
    buf = glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE)
    img = np.frombuffer(buf, np.uint8).reshape(h, w, 4)[::-1, :, 0].copy()
    glBindFramebuffer(GL_FRAMEBUFFER, 0)
    glDeleteFramebuffers(1, [fbo])
    glDeleteTextures(1, [tex])
    return img.astype(np.float64) / 255.0, ms


def render_pillow(font_path, index, metrics, text, pitch):
    """Coverage image of the same string from Pillow (FreeType), one glyph at
    a time at the exact cell position, baseline at ascent from the cell top."""
    w, h, margin, cells = layout(text, pitch, metrics["char_aspect"])
    em_px = pitch * metrics["upem"] / (metrics["ascent"] + metrics["descent"])
    font = ImageFont.truetype(font_path, em_px, index=index)
    img = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(img)
    baseline = margin + pitch * metrics["ascent"] / (metrics["ascent"] + metrics["descent"])
    for ch, (x0, y0, x1, y1) in zip(text, cells):
        draw.text((x0, baseline), ch, font=font, fill=255, anchor="ls")
    return np.asarray(img, np.float64) / 255.0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--font", default="/System/Library/Fonts/Menlo.ttc")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--grid", type=int, default=12)
    ap.add_argument("--out", default="data/vt_test", help="directory for the PNGs")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    atlas, table, metrics = vt_glyphs.build_atlas(args.font, args.index, args.grid, verbose=True)

    if not glfw.init():
        raise RuntimeError("glfw.init failed")
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, glfw.TRUE)
    glfw.window_hint(glfw.VISIBLE, glfw.FALSE)
    win = glfw.create_window(64, 64, "vt_glyphs test", None, None)
    if not win:
        raise RuntimeError("window creation failed")
    glfw.make_context_current(win)

    glsl = (Path(__file__).resolve().parent.parent / "shaders" / "vt_glyph.glsl").read_text()
    prog = compile_program(VERT, FRAG_HEAD + glsl + FRAG_MAIN)
    tex = vt_glyphs.make_texture(atlas)
    glUseProgram(prog)
    glActiveTexture(GL_TEXTURE0)
    glBindTexture(GL_TEXTURE_2D, tex)
    glUniform1i(glGetUniformLocation(prog, "vt_atlas"), 0)

    vao = glGenVertexArrays(1)
    glBindVertexArray(vao)
    vbo = glGenBuffers(1)
    glBindBuffer(GL_ARRAY_BUFFER, vbo)
    stride = 5 * 4
    glEnableVertexAttribArray(0)
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(0))
    glEnableVertexAttribArray(1)
    glVertexAttribPointer(1, 2, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(8))
    glEnableVertexAttribArray(2)
    glVertexAttribPointer(2, 1, GL_FLOAT, GL_FALSE, stride, ctypes.c_void_p(16))
    glDisable(GL_BLEND)
    glDisable(GL_DEPTH_TEST)

    renders = {}
    for px in SIZES:
        img, ms = render_gl(prog, vao, vbo, tex, TEXT, px, metrics["char_aspect"])
        renders[px] = img
        path = out / f"vt_{px}.png"
        Image.fromarray((img * 255 + 0.5).astype(np.uint8)).save(path)
        print(f"{px:4d} px per line: {img.shape[1]}x{img.shape[0]}, draw {ms:.1f} ms -> {path}")

    means = {}
    for px in SIZES:
        ref = render_pillow(args.font, args.index, metrics, TEXT, px)
        diff = np.abs(renders[px] - ref)
        means[px] = diff.mean()
        ink = (ref > 0) | (renders[px] > 0)
        note = "  <- the test criterion" if px == 96 else ""
        if px == 96:
            Image.fromarray((ref * 255 + 0.5).astype(np.uint8)).save(out / "vt_96_pillow.png")
            Image.fromarray((np.clip(diff * 4, 0, 1) * 255 + 0.5).astype(np.uint8)).save(out / "vt_96_diff.png")
        print(f"mean absolute coverage difference vs Pillow at {px:3d} px: {means[px]:.4f} "
              f"(over ink pixels {diff[ink].mean():.4f}, max {diff.max():.3f}, "
              f"vector ink {renders[px].sum():.0f} vs pillow {ref.sum():.0f}){note}")
    mean = means[96]
    print(f"mean absolute coverage difference at 96 px: {mean:.4f} (threshold {THRESHOLD})")

    glfw.terminate()
    ok = mean < THRESHOLD
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
