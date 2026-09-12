// rect.glsl: instanced rectangles from a per-frame VBO, used for item
// bands and outlines, hit boxes and UI boxes. Per instance: rect (x0, y0,
// x1, y1), colour, params (border in device pixels, fill alpha), z. With
// uWorld = 0 the rect is in device pixels (UI); with uWorld = 1 it is in
// world units and goes through uMVP at height z (scaled by uZScale).
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
noperspective out vec2 vScreen;
flat out vec4 vSRect;
flat out vec4 vColor;
flat out vec2 vParam;

vec2 screen_of(vec4 clip) {
    vec2 ndc = clip.xy / clip.w;
    return vec2((ndc.x + 1.0) * 0.5 * uViewport.x, (1.0 - ndc.y) * 0.5 * uViewport.y);
}

void main() {
    if (uWorld == 1) {
        float z = aZ * uZScale;
        vec4 c0 = uMVP * vec4(aRect.xy, z, 1.0);
        vec4 c1 = uMVP * vec4(aRect.zw, z, 1.0);
        vec2 s0 = screen_of(c0), s1 = screen_of(c1);
        vec2 w = mix(aRect.xy, aRect.zw, aQuad);
        gl_Position = uMVP * vec4(w, z, 1.0);
        vScreen = screen_of(gl_Position);
        vSRect = vec4(min(s0, s1), max(s0, s1));
    } else {
        vec2 s0 = aRect.xy, s1 = max(aRect.zw, aRect.xy + 1.0);
        vec2 s = mix(s0, s1, aQuad);
        gl_Position = vec4(s.x / uViewport.x * 2.0 - 1.0, 1.0 - s.y / uViewport.y * 2.0, 0.0, 1.0);
        vScreen = s;
        vSRect = vec4(s0, s1);
    }
    vColor = aColor;
    vParam = aParam;
}
// ---- fragment ----
#version 330 core
noperspective in vec2 vScreen;
flat in vec4 vSRect;
flat in vec4 vColor;
flat in vec2 vParam;
out vec4 frag;

void main() {
    float d = min(min(vScreen.x - vSRect.x, vSRect.z - vScreen.x),
                  min(vScreen.y - vSRect.y, vSRect.w - vScreen.y));
    if (vParam.x > 0.0 && d < vParam.x) { frag = vColor; return; }
    if (vParam.y <= 0.0) discard;
    frag = vec4(vColor.rgb, vParam.y);
}
