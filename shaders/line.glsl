// line.glsl: instanced line quads. The vertex shader fetches the line
// (tex_line_f: x, y, file-relative index; tex_line_u: byte offset lo, file,
// indent | len << 16, byte offset hi), the file's LOD, flags and sampling
// (tex_file_u), its pitch and cap and its height (tex_file_f, three
// texels per file), and emits a degenerate quad for lines outside the
// view and for the rows LOD 0 leaves out. Positions go through uMVP, at the file's top in 3D. The fragment shader draws the
// four LODs: 0 one line per pixel row as a one pixel grey bar (the sampled
// overview texture), 1 a grey bar from indent to len, 2 one block per
// character in the kind colour (token segments), 3 the glyph from the
// atlas times the kind colour.
#version 330 core
layout(location = 0) in vec2 aQuad;
uniform sampler2D uLineF;
uniform usampler2D uLineU;
uniform sampler2D uFileF;
uniform usampler2D uFileU;
uniform mat4 uMVP;
uniform vec4 uView;         // world rectangle in view (conservative), for culling
uniform float uScale;       // device pixels per world unit at the focus
uniform float uFocusW;      // clip w at the focus point (1 in 2D)
uniform float uZScale;
uniform float uCharAspect;
uniform int uBase;          // first line of this draw (visible files come in runs)
uniform int uProp;          // 1: a proportional face; rows at the tokens and text LODs are glyph.glsl's
out vec2 vUV;
flat out uvec2 vOff;
flat out ivec4 vMeta;       // indent, clipped len, LOD, flags
flat out float vPpl;        // device pixels per line

ivec2 tc(int i) { return ivec2(i & 4095, i >> 12); }

void main() {
    int i = gl_InstanceID + uBase;
    uvec4 lu = texelFetch(uLineU, tc(i), 0);
    int f = int(lu.y);
    uvec4 fu = texelFetch(uFileU, tc(f), 0);
    int lod = int(fu.x);
    int len = int(lu.z >> 16);
    vec4 lf = texelFetch(uLineF, tc(i), 0);
    // no early return: Apple's GL driver corrupts the draw when a flat
    // integer output is left unwritten, so every path writes every output
    int j = int(lf.z);
    vec4 meta = texelFetch(uFileF, tc(3 * f + 1), 0);
    vec4 zz = texelFetch(uFileF, tc(3 * f + 2), 0);
    float p = meta.x;
    float lenc = min(float(len), meta.z);
    vec2 p0 = lf.xy;
    float z = (zz.x + zz.y) * uZScale + 0.04;
    // device pixels per line at this row's depth: the focus scale, foreshortened
    float w0 = max((uMVP * vec4(p0, z, 1.0)).w, 1e-6);
    float ppl = p * uScale * uFocusW / w0;
    // LOD 0 (under one device pixel per line) keeps the first row that
    // starts in each new pixel row: exactly one bar per pixel row at any
    // pitch, meeting LOD 1's full rows at one pixel per line with no step
    // (an integer stride halved the density just under it)
    bool sampled_out = lod == 0 && j > 0 && floor(float(j) * ppl) == floor(float(j - 1) * ppl);
    bool kill = len == 0 || sampled_out || (uProp == 1 && lod >= 2);
    // the row's width in line heights (lf.w) from the layout: the clipped
    // character count times the aspect for a monospace face, the sum of the
    // advances for a proportional one; a sampled bar is one device pixel
    // tall where it is, p / ppl world units, so it never thins to nothing
    // in the distance and falls between pixel rows
    vec2 p1 = p0 + vec2(lf.w * p, lod == 0 ? p / ppl : p);
    kill = kill || p1.x < uView.x || p0.x > uView.z || p1.y < uView.y || p0.y > uView.w;
    gl_Position = kill ? vec4(-2.0, -2.0, 0.0, 1.0) : uMVP * vec4(mix(p0, p1, aQuad), z, 1.0);
    vUV = aQuad;
    vOff = uvec2(lu.x, lu.w);
    vMeta = ivec4(int(lu.z & 0xFFFFu), int(lenc), lod, int(fu.y));
    vPpl = ppl;
}
// ---- fragment ----
#version 330 core
// With --vector-text the viewer pastes "#define VT_TEXT" and the whole of
// shaders/vt_glyph.glsl right after the line above; from uVtMin device
// pixels per line the glyph coverage then comes from the vector tier.
uniform usampler2D uChars;
uniform usampler2D uKinds;
uniform sampler2D uGlyphs;
uniform vec4 uGlyph;        // cell_w, cell_h, atlas_w, atlas_h (pixels)
uniform vec3 uKindColor[10];
uniform float uVtMin;       // vector tier from this many device px per line
in vec2 vUV;
flat in uvec2 vOff;
flat in ivec4 vMeta;
flat in float vPpl;
out vec4 frag;

uniform vec3 uBar;          // the line bar colour, from the scheme
uniform vec3 uInk;          // the scheme's ink: the bars' alpha far and near, and the word blocks'

ivec2 tcc(uint o) { return ivec2(int(o & 16383u), int(o >> 14)); }

void main() {
    int indent = vMeta.x, lenc = vMeta.y, lod = vMeta.z, flags = vMeta.w;
    float fx = vUV.x * float(lenc);
    // screen derivatives of the unwrapped cell coordinate (column, y),
    // taken before any discard: they pick the glyph mip level correctly at
    // cell boundaries and give the vector tier the fragment's footprint
    vec2 cell_uv = vec2(fx, vUV.y);
    vec2 gdx = dFdx(cell_uv), gdy = dFdy(cell_uv);
    int col = int(floor(fx));
    float dim = (flags & 8) != 0 ? 0.45 : 1.0;
    // bars and token blocks leave the bottom of the cell empty so rows read
    // as rows, but only once that gap is a pixel and a half: thinner, it
    // would land on a pixel row or not from frame to frame and the columns
    // would shimmer, so the bar fills the row and carries the gap in its
    // alpha instead, the same mean ink either way
    float barf = max(0.7, min(1.0, 1.0 / vPpl));
    bool carve = (1.0 - barf) * vPpl >= 1.5;
    float fill = carve ? 1.0 : barf;
    // bar alpha eases from the scheme's far ink to its near ink between one
    // and three pixels per line, so only the sampling changes at the LOD
    // 0/1 cut and the bars meet the blocks' ink at the LOD 1/2 cut
    float bara = mix(uInk.x, uInk.y, clamp((vPpl - 1.0) / 2.0, 0.0, 1.0));
    // the indent as a fraction of the row: indent columns over the clipped
    // length (a proportional row's indent is spaces, uniform enough)
    float indf = lenc > 0.0 ? float(indent) / lenc : 0.0;
    if (lod == 0) {                        // a sampled line: one pixel row
        if (vUV.x < indf) discard;
        frag = vec4(uBar * dim, bara);
        return;
    }
    if (lod == 1) {
        if (vUV.x < indf || (carve && vUV.y > barf)) discard;
        frag = vec4(uBar * dim, bara * fill);
        return;
    }
    uint o = vOff.x + uint(col);            // hi word is zero below 4 GB
    uint kind = texelFetch(uKinds, tcc(o), 0).x;
    if (kind == 0u) discard;
    vec3 kc = uKindColor[int(kind) % 10] * dim;
    if (lod == 2) {
        if (carve && vUV.y > barf) discard;
        frag = vec4(kc, uInk.z * fill);
        return;
    }
    uint ch = texelFetch(uChars, tcc(o), 0).x;
    vec2 uv = vec2(fract(fx), vUV.y);       // position in the glyph's cell box
    float cov;
#ifdef VT_TEXT
    if (vPpl >= uVtMin) {
        cov = vt_coverage(int(ch), uv, gdx, gdy);
    } else
#endif
    {
        int g = int(ch) - 32;
        if (g < 0 || g > 223) g = 31;       // '?'
        vec2 cell = vec2(g & 15, g >> 4);
        vec2 scale = uGlyph.xy / uGlyph.zw; // cell pixels -> atlas uv
        cov = textureGrad(uGlyphs, (cell + uv) * scale, gdx * scale, gdy * scale).r;
    }
    frag = vec4(kc, cov);
}
