// glyph.glsl: one instanced quad per character, for a proportional face at
// the tokens and text LODs (the line shader keeps the bars, and every LOD
// of a monospace face). Instance i is character uBase + i of the corpus:
// tex_char_f (RG32F: its row, its x within the row in line heights) gives
// the row, tex_line_f the row's position, tex_line_u the file, tex_file_u
// the file's LOD and flags, tex_file_f its pitch and height; uAdv holds
// the face's advances in line heights. Spaces, rows outside the view and
// files below the tokens LOD become a degenerate quad. The fragment
// shader draws a block in the kind colour at LOD 2 and the glyph at LOD
// 3, from the raster atlas (the glyph drawn from the left of a cell as
// wide as the widest advance) or, from uVtMin, the vector tier, whose
// glyph space is the glyph's own advance box.
#version 330 core
layout(location = 0) in vec2 aQuad;
uniform sampler2D uCharF;
uniform sampler2D uLineF;
uniform usampler2D uLineU;
uniform usampler2D uChars;
uniform usampler2D uKinds;
uniform sampler2D uFileF;
uniform usampler2D uFileU;
uniform mat4 uMVP;
uniform vec4 uView;
uniform float uScale;
uniform float uFocusW;
uniform float uZScale;
uniform float uAdv[96];
uniform int uBase;
out vec2 vUV;
flat out int vCode;
flat out int vKind;
flat out ivec2 vRF;         // LOD, flags
flat out float vPpl;

ivec2 tc(int i) { return ivec2(i & 4095, i >> 12); }
ivec2 tcc(int o) { return ivec2(o & 16383, o >> 14); }

void main() {
    int o = gl_InstanceID + uBase;
    int ch = int(texelFetch(uChars, tcc(o), 0).x);
    int kind = int(texelFetch(uKinds, tcc(o), 0).x);
    vec2 cf = texelFetch(uCharF, tcc(o), 0).xy;
    int row = int(cf.x + 0.5);
    vec4 lf = texelFetch(uLineF, tc(row), 0);
    uvec4 lu = texelFetch(uLineU, tc(row), 0);
    int f = int(lu.y);
    uvec4 fu = texelFetch(uFileU, tc(f), 0);
    int lod = int(fu.x);
    vec4 meta = texelFetch(uFileF, tc(3 * f + 1), 0);
    vec4 zz = texelFetch(uFileF, tc(3 * f + 2), 0);
    float p = meta.x;
    int code = clamp(ch, 32, 127) - 32;
    float adv = uAdv[code];
    vec2 p0 = lf.xy + vec2(cf.y * p, 0.0);
    vec2 p1 = p0 + vec2(adv * p, p);
    // no early return: every output is written on every path
    bool kill = kind == 0 || lod < 2 || p1.x < uView.x || p0.x > uView.z || p1.y < uView.y || p0.y > uView.w;
    float z = (zz.x + zz.y) * uZScale + 0.04;
    gl_Position = kill ? vec4(-2.0, -2.0, 0.0, 1.0) : uMVP * vec4(mix(p0, p1, aQuad), z, 1.0);
    vUV = aQuad;
    vCode = code;
    vKind = kind;
    vRF = ivec2(lod, int(fu.y));
    vPpl = p * uScale * uFocusW / max(gl_Position.w, 1e-6);
}
// ---- fragment ----
#version 330 core
// With the vector tier the viewer pastes "#define VT_TEXT" and the whole of
// shaders/vt_glyph.glsl right after the line above.
uniform sampler2D uGlyphs;
uniform vec4 uGlyph;        // cell_w, cell_h, atlas_w, atlas_h (pixels)
uniform vec3 uKindColor[10];
uniform float uVtMin;
uniform float uAdv[96];
uniform float uAdvMax;      // the atlas cell's advance in line heights
uniform vec3 uInk;          // the scheme's ink: z is the word blocks' alpha
in vec2 vUV;
flat in int vCode;
flat in int vKind;
flat in ivec2 vRF;
flat in float vPpl;
out vec4 frag;

void main() {
    int lod = vRF.x, flags = vRF.y;
    float dim = (flags & 8) != 0 ? 0.45 : 1.0;
    vec3 kc = uKindColor[vKind % 10] * dim;
    float barf = max(0.7, min(1.0, 1.0 / vPpl));
    if (lod == 2) {
        if (vUV.y > barf) discard;
        frag = vec4(kc, uInk.z);
        return;
    }
    vec2 gdx = dFdx(vUV), gdy = dFdy(vUV);
    float cov;
#ifdef VT_TEXT
    if (vPpl >= uVtMin) {
        cov = vt_coverage(vCode + 32, vUV, gdx, gdy);
    } else
#endif
    {
        vec2 cell = vec2(vCode & 15, vCode >> 4);
        vec2 scale = uGlyph.xy / uGlyph.zw;
        float fx = uAdv[vCode] / uAdvMax;      // the glyph's advance inside the widest cell
        vec2 uv = vec2(vUV.x * fx, vUV.y);
        cov = textureGrad(uGlyphs, (cell + uv) * scale, gdx * vec2(fx, 1.0) * scale, gdy * vec2(fx, 1.0) * scale).r;
    }
    frag = vec4(kc, cov);
}
