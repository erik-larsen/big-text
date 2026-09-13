// rect.glsl: instanced rectangles from a per-frame VBO, used for item
// bands and outlines, hit boxes and UI boxes. Per instance: rect (x0, y0,
// x1, y1), colour, params (border in device pixels, fill alpha), z. With
// uWorld = 0 the rect is in device pixels (UI) and the border is measured in
// screen space; with uWorld = 1 it is in world units and goes through uMVP
// at height z (scaled by uZScale), and the border is measured in world
// units against the world size of a device pixel from the derivatives, so
// it holds in perspective. (A screen-space box from two projected corners
// does not cover a perspective trapezoid, and painted whole triangles of
// every item outline near the camera.)
#version 330 core
layout(location = 0) in vec2 aQuad;
layout(location = 1) in vec4 aRect;
layout(location = 2) in vec4 aColor;
layout(location = 3) in vec2 aParam;
layout(location = 4) in float aZ;
uniform int uWorld;
uniform mat4 uMVP;
uniform float uZScale;
uniform vec2 uViewport;
out vec2 vPos;              // world units (uWorld = 1) or device pixels (uWorld = 0)
flat out vec4 vRect;
flat out vec4 vColor;
flat out vec2 vParam;

void main() {
    if (uWorld == 1) {
        float z = aZ * uZScale;
        vec2 w = mix(aRect.xy, aRect.zw, aQuad);
        gl_Position = uMVP * vec4(w, z, 1.0);
        vPos = w;
        vRect = aRect;
    } else {
        vec2 s0 = aRect.xy, s1 = max(aRect.zw, aRect.xy + 1.0);
        vec2 s = mix(s0, s1, aQuad);
        gl_Position = vec4(s.x / uViewport.x * 2.0 - 1.0, 1.0 - s.y / uViewport.y * 2.0, 0.0, 1.0);
        vPos = s;
        vRect = vec4(s0, s1);
    }
    vColor = aColor;
    vParam = aParam;
}
// ---- fragment ----
#version 330 core
uniform int uWorld;
in vec2 vPos;
flat in vec4 vRect;
flat in vec4 vColor;
flat in vec2 vParam;
out vec4 frag;

void main() {
    float d = min(min(vPos.x - vRect.x, vRect.z - vPos.x),
                  min(vPos.y - vRect.y, vRect.w - vPos.y));
    // the border width in the units of vPos: device pixels for UI, else
    // the world size of a device pixel here
    float px = uWorld == 1 ? max(fwidth(vPos.x), fwidth(vPos.y)) : 1.0;
    if (vParam.x > 0.0 && d < vParam.x * px) { frag = vColor; return; }
    if (vParam.y <= 0.0) discard;
    frag = vec4(vColor.rgb, vParam.y);
}
