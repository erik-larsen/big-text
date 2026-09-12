// file.glsl: instanced file rectangles. Per instance: tex_file_f (RGBA32F,
// three texels per file: rect, (pitch, colw, cap, hue), (z base, height,
// 0, 0)) and tex_file_u (RGBA8UI, one texel per file: (rung, flags, step
// lo, step hi); flags bit 0 hovered, bit 1 has hits, bit 2 current result,
// bit 3 dimmed). Invisible files become a degenerate quad. Every rung draws
// the dark file background under the lines (the hue is only in the
// directory bands). The border is in device pixels, computed from the world
// distance to the edge and fwidth so it holds in perspective: 1 grey, 2
// yellow for hit files, 3 yellow for the current result. The ring pass
// (uRingOnly) redraws just the border after the text.
#version 330 core
layout(location = 0) in vec2 aQuad;
uniform sampler2D uFileF;
uniform usampler2D uFileU;
uniform int uRingOnly;      // 1: the border pass drawn after the lines
uniform mat4 uMVP;
uniform vec4 uView;
uniform float uScale;       // device pixels per world unit at the focus
uniform float uZScale;
uniform vec3 uHue[12];
out vec2 vWorld;
flat out vec4 vRect;
flat out ivec2 vRF;         // rung, flags
flat out vec3 vTile;
flat out int vRing;

ivec2 tc(int i) { return ivec2(i & 4095, i >> 12); }

void main() {
    int f = gl_InstanceID;
    vec4 rect = texelFetch(uFileF, tc(3 * f), 0);
    // no early return (see line.glsl): every output is written on every path
    bool kill = !(rect.z > uView.x && rect.x < uView.z && rect.w > uView.y && rect.y < uView.w);
    vec4 meta = texelFetch(uFileF, tc(3 * f + 1), 0);
    vec4 zz = texelFetch(uFileF, tc(3 * f + 2), 0);
    uvec4 fu = texelFetch(uFileU, tc(f), 0);
    // slivers thinner than a device pixel are widened to one
    float wpp = 1.0 / uScale;
    vec4 r = rect;
    if (r.z - r.x < wpp) r.z = r.x + wpp;
    if (r.w - r.y < wpp) r.w = r.y + wpp;
    vec2 w = mix(r.xy, r.zw, aQuad);
    float z = (zz.x + zz.y) * uZScale + (uRingOnly == 1 ? 0.08 : 0.0);
    gl_Position = kill ? vec4(-2.0, -2.0, 0.0, 1.0) : uMVP * vec4(w, z, 1.0);
    vWorld = w;
    vRect = r;
    vRF = ivec2(int(fu.x), int(fu.y));
    vTile = uHue[int(meta.w) % 12];
    vRing = uRingOnly;
}
// ---- fragment ----
#version 330 core
in vec2 vWorld;
flat in vec4 vRect;
flat in ivec2 vRF;
flat in vec3 vTile;
flat in int vRing;
out vec4 frag;

const vec3 BG     = vec3(0x1c, 0x1c, 0x1e) / 255.0;
const vec3 FILEBG = vec3(0x11, 0x11, 0x14) / 255.0;
const vec3 GREY   = vec3(0x8a, 0x8f, 0x9a) / 255.0;
const vec3 YELLOW = vec3(0xe8, 0xd4, 0x4d) / 255.0;
const vec3 HOVER  = vec3(0xdf, 0xe3, 0xec) / 255.0;

void main() {
    int rung = vRF.x, flags = vRF.y;
    bool hovered = (flags & 1) != 0, hit = (flags & 2) != 0;
    bool current = (flags & 4) != 0, dimmed = (flags & 8) != 0;
    // every rung shares the file background: the hue lives in the directory
    // bands only, so the rung 0/1 cut does not flip a file's colour
    vec3 col = FILEBG;
    if (dimmed) col *= 0.5;
    float d = min(min(vWorld.x - vRect.x, vRect.z - vWorld.x),
                  min(vWorld.y - vRect.y, vRect.w - vWorld.y));
    float px = max(fwidth(vWorld.x), fwidth(vWorld.y));   // world units per device pixel
    float bw = (current ? 3.0 : (hit ? 2.0 : (hovered ? 2.0 : 1.0))) * px;
    // the border is drawn again after the lines (uRingOnly), so column-0
    // text never breaks the yellow hit outline; the fill pass skips it
    if (vRing == 1 && d >= bw) discard;
    if (d < bw) {
        if (hit || current) col = YELLOW;
        else if (hovered) col = mix(col, HOVER, 0.55);
        else col = mix(col, GREY, 0.7);
    }
    // the light hover fill: the bright bar of the overview; lighter at the
    // text rung so the glyphs drawn over it stay readable (the ring above
    // keeps the full tint at every rung)
    else if (hovered) col = mix(col, HOVER, rung == 3 ? 0.18 : 0.55);
    frag = vec4(col, 1.0);
}
