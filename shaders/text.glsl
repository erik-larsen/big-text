// text.glsl: screen-space UI text. One instance per character: position of
// the cell's top-left corner in device pixels, cell size, atlas cell index,
// colour. Samples the same glyph atlas as the text rung.
#version 330 core
layout(location = 0) in vec2 aQuad;
layout(location = 1) in vec2 aPos;
layout(location = 2) in vec2 aSize;
layout(location = 3) in float aCell;
layout(location = 4) in vec4 aColor;
uniform vec2 uViewport;
uniform vec4 uGlyph;        // cell_w, cell_h, atlas_w, atlas_h
out vec2 vAUV;
flat out vec4 vColor;

void main() {
    vec2 s = aPos + aSize * aQuad;
    gl_Position = vec4(s.x / uViewport.x * 2.0 - 1.0, 1.0 - s.y / uViewport.y * 2.0, 0.0, 1.0);
    int g = int(aCell + 0.5);
    vec2 cell = vec2(g & 15, g >> 4);
    vAUV = (cell + aQuad) * uGlyph.xy / uGlyph.zw;
    vColor = aColor;
}
// ---- fragment ----
#version 330 core
uniform sampler2D uGlyphs;
in vec2 vAUV;
flat in vec4 vColor;
out vec4 frag;

void main() {
    float cov = texture(uGlyphs, vAUV).r;
    frag = vec4(vColor.rgb, vColor.a * cov);
}
