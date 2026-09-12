// file.glsl: instanced file rectangles. Per instance: tex_file_f (RGBA32F,
// two texels per file: rect, then (pitch, colw, cap, hue)) and tex_file_u
// (RGBA8UI, one texel per file: (rung, flags, 0, 0); flags bit 0 hovered,
// bit 1 has hits, bit 2 current result, bit 3 dimmed). Invisible files
// become a degenerate quad. Rung 0 is the tile fill in a muted hue; rungs
// 1..3 draw the dark file background under the lines. The border is in
// device pixels: 1 grey, 2 yellow for hit files, 3 yellow for the current
// result. Files thinner than a pixel are widened to one so slivers show.
#version 330 core
layout(location = 0) in vec2 aQuad;
uniform sampler2D uFileF;
uniform usampler2D uFileU;
uniform int uRingOnly;      // 1: the border pass drawn after the lines
uniform vec2 uOffset;
uniform float uScale;
uniform vec2 uViewport;
uniform vec3 uHue[12];
out vec2 vScreen;
flat out vec4 vSRect;
flat out ivec2 vRF;         // rung, flags
flat out vec3 vTile;
flat out int vRing;

ivec2 tc(int i) { return ivec2(i & 4095, i >> 12); }

void main() {
    int f = gl_InstanceID;
    vec4 rect = texelFetch(uFileF, tc(2 * f), 0);
    vec2 v0 = uOffset, v1 = uOffset + uViewport / uScale;
    // no early return (see line.glsl): every output is written on every path
    bool kill = !(rect.z > v0.x && rect.x < v1.x && rect.w > v0.y && rect.y < v1.y);
    vec4 meta = texelFetch(uFileF, tc(2 * f + 1), 0);
    uvec4 fu = texelFetch(uFileU, tc(f), 0);
    vec2 s0 = (rect.xy - uOffset) * uScale;
    vec2 s1 = (rect.zw - uOffset) * uScale;
    s1 = max(s1, s0 + 1.0);                 // at least one device pixel
    vec2 s = mix(s0, s1, aQuad);
    gl_Position = kill ? vec4(-2.0, -2.0, 0.0, 1.0)
                       : vec4(s.x / uViewport.x * 2.0 - 1.0, 1.0 - s.y / uViewport.y * 2.0, 0.0, 1.0);
    vScreen = s;
    vSRect = vec4(s0, s1);
    vRF = ivec2(int(fu.x), int(fu.y));
    vTile = uHue[int(meta.w) % 12];
    vRing = uRingOnly;
}
// ---- fragment ----
#version 330 core
in vec2 vScreen;
flat in vec4 vSRect;
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
    // rung 0: the tile, darkened to the file background with a trace of the
    // hue so the sampled one pixel bars drawn over it read
    vec3 col = rung == 0 ? mix(FILEBG, vTile, 0.15) : FILEBG;
    if (dimmed) col *= 0.5;
    float d = min(min(vScreen.x - vSRect.x, vSRect.z - vScreen.x),
                  min(vScreen.y - vSRect.y, vSRect.w - vScreen.y));
    float bw = current ? 3.0 : (hit ? 2.0 : (hovered ? 2.0 : 1.0));
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
