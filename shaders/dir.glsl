// dir.glsl: instanced directory rectangles. One instance per directory,
// data from tex_dir_f (RGBA32F, two texels per directory: the outer rect,
// then (pad, depth, hue, 0)). The fragment shader draws the padding ring in
// the hue at 55 percent over a darker inner tint; the ring is at least one
// device pixel wide (fwidth gives world units per pixel).
#version 330 core
layout(location = 0) in vec2 aQuad;
uniform sampler2D uDirF;
uniform vec2 uOffset;      // world coordinate of the top-left device pixel
uniform float uScale;      // device pixels per world unit
uniform vec2 uViewport;    // framebuffer size in device pixels
uniform vec3 uHue[12];
out vec2 vWorld;
flat out vec4 vRect;
flat out float vPad;
flat out vec3 vColor;

ivec2 tc(int i) { return ivec2(i & 4095, i >> 12); }

void main() {
    int d = gl_InstanceID;
    vec4 rect = texelFetch(uDirF, tc(2 * d), 0);
    vec4 meta = texelFetch(uDirF, tc(2 * d + 1), 0);
    vec2 v0 = uOffset, v1 = uOffset + uViewport / uScale;
    // no early return (see line.glsl): every output is written on every path
    bool vis = meta.y >= 1.0 && rect.z > v0.x && rect.x < v1.x
               && rect.w > v0.y && rect.y < v1.y;
    vec2 w = mix(rect.xy, rect.zw, aQuad);
    vec2 s = (w - uOffset) * uScale;
    gl_Position = vis ? vec4(s.x / uViewport.x * 2.0 - 1.0, 1.0 - s.y / uViewport.y * 2.0, 0.0, 1.0)
                      : vec4(-2.0, -2.0, 0.0, 1.0);
    vWorld = w;
    vRect = rect;
    vPad = meta.x;
    vColor = uHue[int(meta.z) % 12];
}
// ---- fragment ----
#version 330 core
in vec2 vWorld;
flat in vec4 vRect;
flat in float vPad;
flat in vec3 vColor;
out vec4 frag;

void main() {
    float d = min(min(vWorld.x - vRect.x, vRect.z - vWorld.x),
                  min(vWorld.y - vRect.y, vRect.w - vWorld.y));
    float px = fwidth(vWorld.x);           // world units per device pixel
    float ring = max(vPad, px);            // never thinner than one pixel
    if (d < ring) frag = vec4(vColor, 0.55);
    else          frag = vec4(vColor * 0.6, 0.16);
}
