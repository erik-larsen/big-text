// rect.glsl: instanced rectangles from a per-frame VBO, used for item
// outlines, hit boxes and UI boxes. Per instance: rect (x0, y0, x1, y1),
// colour, params (border in device pixels, fill alpha). With uScale = 1
// and uOffset = 0 the rect is already in device pixels (UI).
#version 330 core
layout(location = 0) in vec2 aQuad;
layout(location = 1) in vec4 aRect;
layout(location = 2) in vec4 aColor;
layout(location = 3) in vec2 aParam;
uniform vec2 uOffset;
uniform float uScale;
uniform vec2 uViewport;
out vec2 vScreen;
flat out vec4 vSRect;
flat out vec4 vColor;
flat out vec2 vParam;

void main() {
    vec2 s0 = (aRect.xy - uOffset) * uScale;
    vec2 s1 = (aRect.zw - uOffset) * uScale;
    s1 = max(s1, s0 + 1.0);
    vec2 s = mix(s0, s1, aQuad);
    gl_Position = vec4(s.x / uViewport.x * 2.0 - 1.0, 1.0 - s.y / uViewport.y * 2.0, 0.0, 1.0);
    vScreen = s;
    vSRect = vec4(s0, s1);
    vColor = aColor;
    vParam = aParam;
}
// ---- fragment ----
#version 330 core
in vec2 vScreen;
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
