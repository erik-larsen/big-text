// line.glsl: instanced line quads. The vertex shader fetches the line
// (tex_line_f: x, y, file-relative index; tex_line_u: byte offset lo, file,
// indent | len << 16, byte offset hi), the file's rung, flags and sampling
// step (tex_file_u) and its pitch and cap (tex_file_f), and emits a
// degenerate quad for lines outside the view. The fragment shader draws the
// four rungs: 0 every step-th line as a one pixel grey bar (the sampled
// overview texture), 1 a grey bar from indent to len, 2 one block per
// character in the kind colour (token segments), 3 the glyph from the
// atlas times the kind colour.
#version 330 core
layout(location = 0) in vec2 aQuad;
uniform sampler2D uLineF;
uniform usampler2D uLineU;
uniform sampler2D uFileF;
uniform usampler2D uFileU;
uniform vec2 uOffset;
uniform float uScale;
uniform vec2 uViewport;
uniform float uCharAspect;
uniform int uBase;          // first line of this draw (visible files come in runs)
out vec2 vUV;
flat out uvec2 vOff;
flat out ivec4 vMeta;       // indent, clipped len, rung, flags
flat out float vPpl;        // device pixels per line

ivec2 tc(int i) { return ivec2(i & 4095, i >> 12); }

void main() {
    int i = gl_InstanceID + uBase;
    uvec4 lu = texelFetch(uLineU, tc(i), 0);
    int f = int(lu.y);
    uvec4 fu = texelFetch(uFileU, tc(f), 0);
    int rung = int(fu.x);
    int len = int(lu.z >> 16);
    vec4 lf = texelFetch(uLineF, tc(i), 0);
    // rung 0 (under one device pixel per line) keeps every step-th line of
    // the file, step = ceil(1 / ppl) from the CPU, about one bar per pixel
    // row, and draws it exactly one device pixel tall.
    // no early return: Apple's GL driver corrupts the draw when a flat
    // integer output is left unwritten, so every path writes every output
    int step = int(fu.z) | (int(fu.w) << 8);
    int j = int(lf.z);
    bool kill = len == 0 || (rung == 0 && (step < 1 || j % step != 0));
    vec4 meta = texelFetch(uFileF, tc(2 * f + 1), 0);
    float p = meta.x;
    float lenc = min(float(len), meta.z);
    vec2 p0 = lf.xy;
    vec2 p1 = p0 + vec2(lenc * p * uCharAspect, rung == 0 ? 1.0 / uScale : p);
    vec2 v0 = uOffset, v1 = uOffset + uViewport / uScale;
    kill = kill || p1.x < v0.x || p0.x > v1.x || p1.y < v0.y || p0.y > v1.y;
    vec2 s = (mix(p0, p1, aQuad) - uOffset) * uScale;
    gl_Position = kill ? vec4(-2.0, -2.0, 0.0, 1.0)
                       : vec4(s.x / uViewport.x * 2.0 - 1.0, 1.0 - s.y / uViewport.y * 2.0, 0.0, 1.0);
    vUV = aQuad;
    vOff = uvec2(lu.x, lu.w);
    vMeta = ivec4(int(lu.z & 0xFFFFu), int(lenc), rung, int(fu.y));
    vPpl = p * uScale;
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

const vec3 BAR = vec3(0xa4, 0xa8, 0xb0) / 255.0;

ivec2 tcc(uint o) { return ivec2(int(o & 16383u), int(o >> 14)); }

void main() {
    int indent = vMeta.x, lenc = vMeta.y, rung = vMeta.z, flags = vMeta.w;
    float fx = vUV.x * float(lenc);
    // screen derivatives of the unwrapped cell coordinate (column, y),
    // taken before any discard: they pick the glyph mip level correctly at
    // cell boundaries and give the vector tier the fragment's footprint
    vec2 cell_uv = vec2(fx, vUV.y);
    vec2 gdx = dFdx(cell_uv), gdy = dFdy(cell_uv);
    int col = int(floor(fx));
    float dim = (flags & 8) != 0 ? 0.45 : 1.0;
    // bars and token blocks leave the bottom of the cell empty so rows read
    // as rows; never thinner than one device pixel
    float barf = max(0.7, min(1.0, 1.0 / vPpl));
    // bar brightness ramps with pixels per line across the rung 0/1 cut,
    // so only the sampling changes at one pixel per line, not the look
    float bara = clamp(0.5 + 0.15 * vPpl, 0.55, 0.8);
    if (rung == 0) {                        // a sampled line: one pixel row
        if (col < indent) discard;
        frag = vec4(BAR * dim, bara);
        return;
    }
    if (rung == 1) {
        if (col < indent || vUV.y > barf) discard;
        frag = vec4(BAR * dim, bara);
        return;
    }
    uint o = vOff.x + uint(col);            // hi word is zero below 4 GB
    uint kind = texelFetch(uKinds, tcc(o), 0).x;
    if (kind == 0u) discard;
    vec3 kc = uKindColor[int(kind) % 10] * dim;
    if (rung == 2) {
        if (vUV.y > barf) discard;
        frag = vec4(kc, 1.0);
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
        if (g < 0 || g > 94) g = 31;        // '?'
        vec2 cell = vec2(g & 15, g >> 4);
        vec2 scale = uGlyph.xy / uGlyph.zw; // cell pixels -> atlas uv
        cov = textureGrad(uGlyphs, (cell + uv) * scale, gdx * scale, gdy * scale).r;
    }
    frag = vec4(kc, cov);
}
