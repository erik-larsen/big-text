// dir.glsl: instanced directory rectangles. One instance per directory,
// data from tex_dir_f (RGBA32F, three texels per directory: the outer rect,
// (pad, depth, hue, 0), then (z base, height, 0, 0)). The fragment shader
// draws the padding ring in the hue at 55 percent over a darker inner tint
// (a solid terrace top in 3D); the ring is at least one device pixel wide
// and at most uBandPx (fwidth gives world units per pixel), so the band
// stays thin at every zoom and the rest of the padding reads as the inner
// tint. Positions go through uMVP, which is
// orthographic in 2D and perspective in 3D; uZScale is 0 in 2D so every
// height collapses onto the plane.
#version 330 core
layout(location = 0) in vec2 aQuad;
uniform sampler2D uDirF;
uniform mat4 uMVP;
uniform vec4 uView;        // world rectangle in view (conservative), for culling
uniform float uZScale;
uniform vec3 uHue[12];
uniform int uBandFlat;     // 1: every band in uBandColor, no inner tint (a scheme's "band")
uniform vec3 uBandColor;
out vec2 vWorld;
flat out vec4 vRect;
flat out float vPad;
flat out vec3 vColor;

ivec2 tc(int i) { return ivec2(i & 4095, i >> 12); }

void main() {
    int d = gl_InstanceID;
    vec4 rect = texelFetch(uDirF, tc(3 * d), 0);
    vec4 meta = texelFetch(uDirF, tc(3 * d + 1), 0);
    vec4 zz = texelFetch(uDirF, tc(3 * d + 2), 0);
    // no early return (see line.glsl): every output is written on every path
    bool vis = meta.y >= 1.0 && rect.z > uView.x && rect.x < uView.z
               && rect.w > uView.y && rect.y < uView.w;
    vec2 w = mix(rect.xy, rect.zw, aQuad);
    float z = (zz.x + zz.y) * uZScale;
    gl_Position = vis ? uMVP * vec4(w, z, 1.0) : vec4(-2.0, -2.0, 0.0, 1.0);
    vWorld = w;
    vRect = rect;
    vPad = meta.x;
    vColor = uBandFlat == 1 ? uBandColor : uHue[int(meta.z) % 12];
}
// ---- fragment ----
#version 330 core
uniform float uZScale;
uniform float uBandPx;     // the band's greatest width in device pixels
uniform vec3 uGround;      // the scheme's ground colour, under the terrace tint
uniform int uBandFlat;
in vec2 vWorld;
flat in vec4 vRect;
flat in float vPad;
flat in vec3 vColor;
out vec4 frag;

void main() {
    float d = min(min(vWorld.x - vRect.x, vRect.z - vWorld.x),
                  min(vWorld.y - vRect.y, vRect.w - vWorld.y));
    float px = fwidth(vWorld.x);           // world units per device pixel
    float ring = min(max(vPad, px), uBandPx * px);   // one pixel at least, uBandPx at most
    if (uBandFlat == 1) { if (d >= ring) discard; frag = vec4(vColor, 1.0); }
    else if (d < ring) frag = vec4(vColor, uZScale > 0.0 ? 1.0 : 0.55);
    else               frag = uZScale > 0.0 ? vec4(mix(uGround, vColor, 0.16), 1.0)
                                            : vec4(vColor * 0.6, 0.16);
}
