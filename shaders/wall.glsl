// wall.glsl: the sides of the extruded files and directories in 3D. Four
// instances per rectangle (one per side); instance i < 4 * nFiles is a
// file, the rest are directories. The quad runs along the edge and from
// the rectangle's z base to its top. Flat shading by side: the face toward
// the camera (the +y edge, since the camera tilts in from +y) is the
// brightest.
#version 330 core
layout(location = 0) in vec2 aQuad;
uniform sampler2D uFileF;
uniform sampler2D uDirF;
uniform int uNFiles;
uniform mat4 uMVP;
uniform vec4 uView;
uniform float uZScale;
uniform vec3 uHue[12];
flat out vec3 vColor;

ivec2 tc(int i) { return ivec2(i & 4095, i >> 12); }

void main() {
    int i = gl_InstanceID;
    bool isFile = i < 4 * uNFiles;
    int idx = isFile ? i / 4 : (i - 4 * uNFiles) / 4;
    int side = i % 4;
    vec4 rect, meta, zz;
    if (isFile) {
        rect = texelFetch(uFileF, tc(3 * idx), 0);
        meta = texelFetch(uFileF, tc(3 * idx + 1), 0);
        zz = texelFetch(uFileF, tc(3 * idx + 2), 0);
    } else {
        rect = texelFetch(uDirF, tc(3 * idx), 0);
        meta = texelFetch(uDirF, tc(3 * idx + 1), 0);
        zz = texelFetch(uDirF, tc(3 * idx + 2), 0);
    }
    float hue = isFile ? meta.w : meta.z;
    bool vis = zz.y > 0.0 && rect.z > uView.x && rect.x < uView.z
               && rect.w > uView.y && rect.y < uView.w && (isFile || meta.y >= 1.0);
    vec2 a, b;
    if (side == 0)      { a = rect.xy; b = vec2(rect.z, rect.y); }
    else if (side == 1) { a = vec2(rect.z, rect.y); b = rect.zw; }
    else if (side == 2) { a = rect.zw; b = vec2(rect.x, rect.w); }
    else                { a = vec2(rect.x, rect.w); b = rect.xy; }
    vec2 w = mix(a, b, aQuad.x);
    float z = (zz.x + zz.y * aQuad.y) * uZScale;
    gl_Position = vis ? uMVP * vec4(w, z, 1.0) : vec4(-2.0, -2.0, 0.0, 1.0);
    float shade = side == 2 ? 0.95 : (side == 0 ? 0.45 : 0.68);
    vec3 base = uHue[int(hue) % 12];
    vColor = (isFile ? mix(base, vec3(0.12, 0.12, 0.14), 0.45) : base) * shade;
}
// ---- fragment ----
#version 330 core
flat in vec3 vColor;
out vec4 frag;

void main() {
    frag = vec4(vColor, 1.0);
}
