// Vector glyph coverage on the Slug algorithm, GLSL 330 core (also valid
// GLSL ES 3.00). A translation of SlugRender and its helpers from the
// reference pixel shader at https://github.com/EricLengyel/Slug:
//
//     Copyright 2017, by Eric Lengyel.
//     SPDX-License-Identifier: MIT OR Apache-2.0
//     "The code in this repository may be freely used by anyone for any
//     purpose. The patent has been dedicated to the public domain. If you
//     do use this code in software that gets distributed in any way, then
//     you are required to give credit."
//
// The fragment finds the horizontal and vertical band of its glyph that
// contain it, walks each band's curves (sorted so the loop can stop at the
// first curve wholly behind the ray), decides for each curve which of its
// two roots count from the sign bits of the control points alone (the root
// code, which is what makes the method robust: no root is ever range-checked
// against t), accumulates a box-filtered coverage along a horizontal and a
// vertical ray, and combines the two by how close their crossings fall to
// the pixel.
//
// Data from vt_glyphs.py: the sampler2D `vt_curves` (RGBA32F, one texel per
// quadratic bezier: p1 and p2, the next texel's xy being p3) and the
// usampler2D `vt_bands` (RG16UI: row 0 a directory of four texels per code,
// then per-glyph blocks of band headers and curve lists in one linear
// address space wrapping at 4096). Glyph space is the cell box with y up;
// the cell's uv is y down, so the shader flips it.
//
// Concatenate this file into a fragment shader after its #version line.
// Provides
//   float vt_coverage(int code, vec2 uv)
//   float vt_coverage(int code, vec2 uv, vec2 duvdx, vec2 duvdy)
// code is the byte of Windows-1252 (32..255; others draw as space), uv is the position
// in the glyph's cell box, (0,0) top left, (1,1) bottom right, the same uv
// the raster atlas cell is sampled with. The two-argument form takes the
// screen derivatives of uv itself; when uv is a fract() of a longer varying,
// pass the derivatives of the unwrapped value to the four-argument form.

uniform sampler2D vt_curves;
uniform usampler2D vt_bands;

const int VT_LOG_W = 12;                 // both textures are 4096 wide
const float VT_COORD_SCALE = 2.0;        // the directory's bbox fixed point covers [-0.5, 1.5]
const float VT_COORD_OFFSET = -0.5;

ivec2 vt_addr(int a) {
    return ivec2(a & ((1 << VT_LOG_W) - 1), a >> VT_LOG_W);
}

// The root eligibility code of a curve relative to the sample, from the
// signs of the three control point coordinates across the ray: bit 0 says
// the first root contributes, bit 8 the second. Eight equivalence classes
// of sign patterns, one table lookup, no comparison of t against [0, 1).
uint vt_root_code(float y1, float y2, float y3) {
    uint i1 = floatBitsToUint(y1) >> 31u;
    uint i2 = floatBitsToUint(y2) >> 30u;
    uint i3 = floatBitsToUint(y3) >> 29u;
    uint shift = (i2 & 2u) | (i1 & ~2u);
    shift = (i3 & 4u) | (shift & ~4u);
    return (0x2E74u >> shift) & 0x0101u;
}

// The x coordinates where the curve crosses y = 0. The polynomial in t is
// a t^2 - 2 b t + c with a = y1 - 2 y2 + y3, b = y1 - y2, c = y1; the
// discriminant is clamped to zero (imaginary roots become a double root at
// the minimum) and a nearly linear polynomial is solved as -2 b t + c = 0.
vec2 vt_solve_h(vec4 p12, vec2 p3) {
    vec2 a = p12.xy - p12.zw * 2.0 + p3;
    vec2 b = p12.xy - p12.zw;
    float ra = 1.0 / a.y;
    float rb = 0.5 / b.y;
    float d = sqrt(max(b.y * b.y - a.y * p12.y, 0.0));
    float t1 = (b.y - d) * ra;
    float t2 = (b.y + d) * ra;
    if (abs(a.y) < 1.0 / 65536.0) {
        t1 = p12.y * rb;
        t2 = t1;
    }
    return vec2((a.x * t1 - b.x * 2.0) * t1 + p12.x, (a.x * t2 - b.x * 2.0) * t2 + p12.x);
}

// The y coordinates where the curve crosses x = 0.
vec2 vt_solve_v(vec4 p12, vec2 p3) {
    vec2 a = p12.xy - p12.zw * 2.0 + p3;
    vec2 b = p12.xy - p12.zw;
    float ra = 1.0 / a.x;
    float rb = 0.5 / b.x;
    float d = sqrt(max(b.x * b.x - a.x * p12.x, 0.0));
    float t1 = (b.x - d) * ra;
    float t2 = (b.x + d) * ra;
    if (abs(a.x) < 1.0 / 65536.0) {
        t1 = p12.x * rb;
        t2 = t1;
    }
    return vec2((a.y * t1 - b.y * 2.0) * t1 + p12.y, (a.y * t2 - b.y * 2.0) * t2 + p12.y);
}

// The two rays' coverages combined by their weights (how close a crossing
// fell to the pixel centre); the absolute values let either winding
// convention render. Nonzero fill rule.
float vt_combine(float xcov, float ycov, float xwgt, float ywgt) {
    float c = max(abs(xcov * xwgt + ycov * ywgt) / max(xwgt + ywgt, 1.0 / 65536.0), min(abs(xcov), abs(ycov)));
    return clamp(c, 0.0, 1.0);
}

float vt_coverage(int code, vec2 uv, vec2 duvdx, vec2 duvdy) {
    code = clamp(code, 0, 255);
    uvec2 d0 = texelFetch(vt_bands, ivec2(4 * code, 0), 0).xy;
    uvec2 d1 = texelFetch(vt_bands, ivec2(4 * code + 1, 0), 0).xy;
    uvec2 d2 = texelFetch(vt_bands, ivec2(4 * code + 2, 0), 0).xy;
    uvec2 d3 = texelFetch(vt_bands, ivec2(4 * code + 3, 0), 0).xy;
    int base = int(d0.x) + (int(d0.y) << VT_LOG_W);
    ivec2 bandMax = ivec2(d1);
    vec2 b0 = vec2(d2) * (VT_COORD_SCALE / 65535.0) + VT_COORD_OFFSET;
    vec2 b1 = vec2(d3) * (VT_COORD_SCALE / 65535.0) + VT_COORD_OFFSET;

    // glyph space is y up: the cell's top edge is v = 1
    vec2 p = vec2(uv.x, 1.0 - uv.y);
    // the pixel's footprint in glyph units, per axis (the reference's fwidth)
    vec2 emsPerPixel = max(abs(duvdx) + abs(duvdy), vec2(1e-8));
    vec2 pixelsPerEm = 1.0 / emsPerPixel;

    vec2 scale = vec2(bandMax + 1) / max(b1 - b0, vec2(1e-6));
    ivec2 bandIndex = clamp(ivec2(floor((p - b0) * scale)), ivec2(0), bandMax);

    // the horizontal band: a ray along +x
    float xcov = 0.0, xwgt = 0.0;
    uvec2 hband = texelFetch(vt_bands, vt_addr(base + bandIndex.y), 0).xy;
    int hloc = base + int(hband.y);
    for (int i = 0; i < int(hband.x); i++) {
        ivec2 cl = ivec2(texelFetch(vt_bands, vt_addr(hloc + i), 0).xy);
        vec4 p12 = texelFetch(vt_curves, cl, 0) - vec4(p, p);
        vec2 p3 = texelFetch(vt_curves, cl + ivec2(1, 0), 0).xy - p;
        // sorted by maximum x descending: everything from here on is behind the ray
        if (max(max(p12.x, p12.z), p3.x) * pixelsPerEm.x < -0.5) break;
        uint rc = vt_root_code(p12.y, p12.w, p3.y);
        if (rc != 0u) {
            vec2 r = vt_solve_h(p12, p3) * pixelsPerEm.x;
            if ((rc & 1u) != 0u) {
                xcov += clamp(r.x + 0.5, 0.0, 1.0);
                xwgt = max(xwgt, clamp(1.0 - abs(r.x) * 2.0, 0.0, 1.0));
            }
            if (rc > 1u) {
                xcov -= clamp(r.y + 0.5, 0.0, 1.0);
                xwgt = max(xwgt, clamp(1.0 - abs(r.y) * 2.0, 0.0, 1.0));
            }
        }
    }

    // the vertical band: a ray along +y; its headers follow the horizontal ones
    float ycov = 0.0, ywgt = 0.0;
    uvec2 vband = texelFetch(vt_bands, vt_addr(base + bandMax.y + 1 + bandIndex.x), 0).xy;
    int vloc = base + int(vband.y);
    for (int i = 0; i < int(vband.x); i++) {
        ivec2 cl = ivec2(texelFetch(vt_bands, vt_addr(vloc + i), 0).xy);
        vec4 p12 = texelFetch(vt_curves, cl, 0) - vec4(p, p);
        vec2 p3 = texelFetch(vt_curves, cl + ivec2(1, 0), 0).xy - p;
        if (max(max(p12.y, p12.w), p3.y) * pixelsPerEm.y < -0.5) break;
        uint rc = vt_root_code(p12.x, p12.z, p3.x);
        if (rc != 0u) {
            vec2 r = vt_solve_v(p12, p3) * pixelsPerEm.y;
            if ((rc & 1u) != 0u) {
                ycov -= clamp(r.x + 0.5, 0.0, 1.0);
                ywgt = max(ywgt, clamp(1.0 - abs(r.x) * 2.0, 0.0, 1.0));
            }
            if (rc > 1u) {
                ycov += clamp(r.y + 0.5, 0.0, 1.0);
                ywgt = max(ywgt, clamp(1.0 - abs(r.y) * 2.0, 0.0, 1.0));
            }
        }
    }
    return vt_combine(xcov, ycov, xwgt, ywgt);
}

float vt_coverage(int code, vec2 uv) {
    return vt_coverage(code, uv, dFdx(uv), dFdy(uv));
}
