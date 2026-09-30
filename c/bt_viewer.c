/*
    bt_viewer.c - view a big-text atlas as one zoomable surface.
    C / SDL2 / GLES3 port of atlas_viewer.py: native through ANGLE on the
    Mac, and in the browser through Emscripten and WebGL 2.

    Usage: bt_viewer ATLAS [--shaders DIR] [--no-vector-text]
                     [--goto PATH[:LINE]] [--zoom PX_PER_LINE] [--filter WORD]
                     [--proj 2d|3d] [--tilt DEG] [--yaw DEG] [--heights] [--tint]
                     [--frames N] [--screenshot OUT.ppm]

    ATLAS is the viewer.bin that atlas_export.py writes, or the atlas
    directory holding it. The shaders are the Python viewer's own
    (the .glsl files under shaders/), rewritten from GLSL 330 to GLSL ES 3.00 as they load.
    The scripting flags are the few the port is checked with; the Python
    viewer keeps the full set (--shots, --hover, --stats, --step).

    Controls: wheel = zoom about the cursor (with a glide) | drag = pan
              double-click = fly to the file or directory under the cursor,
              again at its extents = fly back | hover = file / line / item label
              R = reset the view | Q = quit (native)
              / = focus the filter box, type to search, Backspace edits
              Enter, Down, ] = next hit | Up, [ = previous hit
              click a hit in the panel = fly there | Escape = clear the filter
              3 = the tilted projection on and off | Alt-drag = tilt and turn
              H = the heights on and off in 3D (files rise by their weight)
              C = the tint on and off, when the corpus has one
*/
#include <SDL.h>
#include <GLES3/gl3.h>

#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif

#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define CHARS_W 16384         /* width of tex_chars / tex_kinds, shared with line.glsl */
#define TEX_W 4096            /* width of the per-line and per-file textures */
#define UI_UNIT 11            /* the texture unit of the UI's own glyph atlas */
#define ZOOM_TAU 0.12         /* glide time constant, seconds */
#define ZOOM_VMAX 12.0        /* log-zoom per second at most */
#define PANEL_PT 180          /* results panel width in window points */
#define BAND_PT 3             /* the directory band's greatest width in window points */
#define DBL_CLICK_S 0.35      /* two presses this close in time and place are a double click */
#define MARGIN_PT 10          /* UI margin in window points */
#define FLY_RHO 1.4
#define FOVY 45.0             /* 3D camera field of view, degrees */
#define H_MAX 90.0            /* tallest file in world units (the world is 1600 wide) */
#define DIR_STEP 5.0          /* terrace height per directory level */
#define TILT_MAX 70.0
#define FLY_V 10.0            /* van Wijk path units per second */
#define VT_MIN_PPL 12.0       /* the vector tier draws glyphs from here */

/* Windows-1252, the corpus's and the UI atlas's encoding */
#define DOT " \xb7 "          /* middle dot */
#define CRUMB " \x9b "        /* single right angle quote */
#define ELLIPSIS "\x85"

static const float HUES[12][3] = {
    {0x6a / 255.f, 0x8f / 255.f, 0xd8 / 255.f}, {0x4f / 255.f, 0xb3 / 255.f, 0xa6 / 255.f},
    {0x7f / 255.f, 0xbf / 255.f, 0x6a / 255.f}, {0xe0 / 255.f, 0x8a / 255.f, 0x4a / 255.f},
    {0xd8 / 255.f, 0xc0 / 255.f, 0x50 / 255.f}, {0xa5 / 255.f, 0x7f / 255.f, 0xd0 / 255.f},
    {0xa8 / 255.f, 0x86 / 255.f, 0x5a / 255.f}, {0xd8 / 255.f, 0x78 / 255.f, 0xa8 / 255.f},
    {0x60 / 255.f, 0xb8 / 255.f, 0xe0 / 255.f}, {0xb0 / 255.f, 0xc8 / 255.f, 0x60 / 255.f},
    {0xd8 / 255.f, 0x68 / 255.f, 0x68 / 255.f}, {0x90 / 255.f, 0x90 / 255.f, 0xd8 / 255.f}};
static const float YELLOW[3] = {0xe8 / 255.f, 0xd4 / 255.f, 0x4d / 255.f};
static const float UI_TEXT[3] = {0xe6 / 255.f, 0xe6 / 255.f, 0xe6 / 255.f};
static const float UI_BOX[3] = {0x2a / 255.f, 0x2a / 255.f, 0x2e / 255.f};
static const float UI_DIM[3] = {0x8c / 255.f, 0x90 / 255.f, 0x9a / 255.f};

/* ---------------------------------------------------------------- the bundle */

/* viewer.bin: a table of named arrays (atlas_export.py) */
typedef struct {
    char name[49];
    char dt[5];
    int ndim;
    uint64_t dim[3];
    void *p;
    uint64_t nbytes;
} Arr;

typedef struct {
    uint8_t *blob;
    int n;
    Arr *arr;
} Bundle;

typedef struct {          /* strings: a Windows-1252 blob and n + 1 offsets */
    const char *blob;
    const uint32_t *off;
    int n;
} Strs;

static void die(const char *fmt, const char *s)
{
    fprintf(stderr, fmt, s);
    fputc('\n', stderr);
    exit(1);
}

static bool bundle_load(Bundle *b, const char *path)
{
    struct stat st;
    if (stat(path, &st) != 0 || !S_ISREG(st.st_mode))
        return false;
    FILE *f = fopen(path, "rb");
    if (!f)
        return false;
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    b->blob = malloc(size);
    if (!b->blob || fread(b->blob, 1, size, f) != (size_t)size)
        die("error: cannot read %s", path);
    fclose(f);
    if (size < 16 || memcmp(b->blob, "BTAT", 4))
        die("error: %s is not a viewer.bin; run ./atlas_export.py", path);
    uint32_t count;
    memcpy(&count, b->blob + 8, 4);
    b->n = (int)count;
    b->arr = calloc(count, sizeof(Arr));
    for (uint32_t i = 0; i < count; i++) {
        const uint8_t *e = b->blob + 16 + 96 * i;
        Arr *a = &b->arr[i];
        memcpy(a->name, e, 48);
        memcpy(a->dt, e + 48, 4);
        uint32_t nd;
        uint64_t off;
        memcpy(&nd, e + 52, 4);
        memcpy(a->dim, e + 56, 24);
        memcpy(&off, e + 80, 8);
        memcpy(&a->nbytes, e + 88, 8);
        a->ndim = (int)nd;
        a->p = b->blob + off;
    }
    return true;
}

static Arr *bundle_opt(Bundle *b, const char *name)
{
    for (int i = 0; i < b->n; i++)
        if (!strcmp(b->arr[i].name, name))
            return &b->arr[i];
    return NULL;
}

static Arr *bundle_get(Bundle *b, const char *name)
{
    Arr *a = bundle_opt(b, name);
    if (!a)
        die("error: viewer.bin has no '%s'; rerun ./atlas_export.py", name);
    return a;
}

static void *get(Bundle *b, const char *name, const char *dt)
{
    Arr *a = bundle_get(b, name);
    if (strcmp(a->dt, dt))
        die("error: viewer.bin: '%s' has an unexpected type", name);
    return a->p;
}

static void *get_opt(Bundle *b, const char *name)
{
    Arr *a = bundle_opt(b, name);
    return a ? a->p : NULL;
}

static uint64_t count_of(Bundle *b, const char *name)
{
    return bundle_get(b, name)->dim[0];
}

static Strs strs(Bundle *b, const char *name)
{
    char off[64];
    snprintf(off, sizeof off, "%s_off", name);
    Strs s;
    s.blob = get(b, name, "u1");
    s.off = get(b, off, "u4");
    s.n = (int)count_of(b, off) - 1;
    return s;
}

/* string i as a NUL-terminated copy in buf */
static const char *str_at(Strs s, int i, char *buf, size_t cap)
{
    size_t n = s.off[i + 1] - s.off[i];
    if (n >= cap)
        n = cap - 1;
    memcpy(buf, s.blob + s.off[i], n);
    buf[n] = 0;
    return buf;
}

static int str_len(Strs s, int i)
{
    return (int)(s.off[i + 1] - s.off[i]);
}

/* ---------------------------------------------------------------- small helpers */

typedef struct {          /* a growable float buffer for per-frame instances */
    float *p;
    int n, cap;
} FBuf;

static float *fbuf_add(FBuf *b, int k)
{
    if (b->n + k > b->cap) {
        b->cap = (b->n + k) * 2;
        b->p = realloc(b->p, b->cap * sizeof(float));
    }
    float *out = b->p + b->n;
    b->n += k;
    memset(out, 0, k * sizeof(float));
    return out;
}

static double now_s(void)
{
    return (double)SDL_GetPerformanceCounter() / (double)SDL_GetPerformanceFrequency();
}

static double clampd(double v, double lo, double hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

static bool in_rect(double x, double y, const double *r)
{
    return r[0] <= x && x < r[2] && r[1] <= y && y < r[3];
}

/* 1234567 -> "1,234,567" */
static const char *commas(int64_t v, char *buf)
{
    char tmp[32];
    int n = snprintf(tmp, sizeof tmp, "%lld", (long long)(v < 0 ? -v : v));
    int k = 0;
    if (v < 0)
        buf[k++] = '-';
    for (int i = 0; i < n; i++) {
        buf[k++] = tmp[i];
        if ((n - i - 1) % 3 == 0 && i < n - 1)
            buf[k++] = ',';
    }
    buf[k] = 0;
    return buf;
}

/* row-major 4x4: out = a @ b */
static void mat4_mul(double *out, const double *a, const double *b)
{
    double r[16];
    for (int i = 0; i < 4; i++)
        for (int j = 0; j < 4; j++) {
            double s = 0;
            for (int k = 0; k < 4; k++)
                s += a[i * 4 + k] * b[k * 4 + j];
            r[i * 4 + j] = s;
        }
    memcpy(out, r, sizeof r);
}

static void mat4_xform(const double *m, const double *v, double *out)
{
    for (int r = 0; r < 4; r++)
        out[r] = m[r * 4] * v[0] + m[r * 4 + 1] * v[1] + m[r * 4 + 2] * v[2] + m[r * 4 + 3] * v[3];
}

/* the general inverse by cofactors; false when singular */
static bool mat4_inv(const double *m, double *out)
{
    double inv[16];
    inv[0] = m[5] * m[10] * m[15] - m[5] * m[11] * m[14] - m[9] * m[6] * m[15] + m[9] * m[7] * m[14] + m[13] * m[6] * m[11] - m[13] * m[7] * m[10];
    inv[4] = -m[4] * m[10] * m[15] + m[4] * m[11] * m[14] + m[8] * m[6] * m[15] - m[8] * m[7] * m[14] - m[12] * m[6] * m[11] + m[12] * m[7] * m[10];
    inv[8] = m[4] * m[9] * m[15] - m[4] * m[11] * m[13] - m[8] * m[5] * m[15] + m[8] * m[7] * m[13] + m[12] * m[5] * m[11] - m[12] * m[7] * m[9];
    inv[12] = -m[4] * m[9] * m[14] + m[4] * m[10] * m[13] + m[8] * m[5] * m[14] - m[8] * m[6] * m[13] - m[12] * m[5] * m[10] + m[12] * m[6] * m[9];
    inv[1] = -m[1] * m[10] * m[15] + m[1] * m[11] * m[14] + m[9] * m[2] * m[15] - m[9] * m[3] * m[14] - m[13] * m[2] * m[11] + m[13] * m[3] * m[10];
    inv[5] = m[0] * m[10] * m[15] - m[0] * m[11] * m[14] - m[8] * m[2] * m[15] + m[8] * m[3] * m[14] + m[12] * m[2] * m[11] - m[12] * m[3] * m[10];
    inv[9] = -m[0] * m[9] * m[15] + m[0] * m[11] * m[13] + m[8] * m[1] * m[15] - m[8] * m[3] * m[13] - m[12] * m[1] * m[11] + m[12] * m[3] * m[9];
    inv[13] = m[0] * m[9] * m[14] - m[0] * m[10] * m[13] - m[8] * m[1] * m[14] + m[8] * m[2] * m[13] + m[12] * m[1] * m[10] - m[12] * m[2] * m[9];
    inv[2] = m[1] * m[6] * m[15] - m[1] * m[7] * m[14] - m[5] * m[2] * m[15] + m[5] * m[3] * m[14] + m[13] * m[2] * m[7] - m[13] * m[3] * m[6];
    inv[6] = -m[0] * m[6] * m[15] + m[0] * m[7] * m[14] + m[4] * m[2] * m[15] - m[4] * m[3] * m[14] - m[12] * m[2] * m[7] + m[12] * m[3] * m[6];
    inv[10] = m[0] * m[5] * m[15] - m[0] * m[7] * m[13] - m[4] * m[1] * m[15] + m[4] * m[3] * m[13] + m[12] * m[1] * m[7] - m[12] * m[3] * m[5];
    inv[14] = -m[0] * m[5] * m[14] + m[0] * m[6] * m[13] + m[4] * m[1] * m[14] - m[4] * m[2] * m[13] - m[12] * m[1] * m[6] + m[12] * m[2] * m[5];
    inv[3] = -m[1] * m[6] * m[11] + m[1] * m[7] * m[10] + m[5] * m[2] * m[11] - m[5] * m[3] * m[10] - m[9] * m[2] * m[7] + m[9] * m[3] * m[6];
    inv[7] = m[0] * m[6] * m[11] - m[0] * m[7] * m[10] - m[4] * m[2] * m[11] + m[4] * m[3] * m[10] + m[8] * m[2] * m[7] - m[8] * m[3] * m[6];
    inv[11] = -m[0] * m[5] * m[11] + m[0] * m[7] * m[9] + m[4] * m[1] * m[11] - m[4] * m[3] * m[9] - m[8] * m[1] * m[7] + m[8] * m[3] * m[5];
    inv[15] = m[0] * m[5] * m[10] - m[0] * m[6] * m[9] - m[4] * m[1] * m[10] + m[4] * m[2] * m[9] + m[8] * m[1] * m[6] - m[8] * m[2] * m[5];
    double det = m[0] * inv[0] + m[1] * inv[4] + m[2] * inv[8] + m[3] * inv[12];
    if (fabs(det) < 1e-300)
        return false;
    for (int i = 0; i < 16; i++)
        out[i] = inv[i] / det;
    return true;
}

/* ---------------------------------------------------------------- the viewer */

enum { P_DIR, P_FILE, P_LINE, P_RECT, P_TEXT, P_WALL, P_GLYPH, P_COUNT };
static const char *PROG_NAMES[P_COUNT] = {"dir", "file", "line", "rect", "text", "wall", "glyph"};
#define MAX_UNIFORMS 48

typedef struct {
    char *text;
    const float *color;
    int ri;               /* the hit's index, or -1 */
} PanelRow;

typedef struct {
    bool on;
    int mode;             /* 0 zoom, 1 pan */
    double c0[2], c1[2], w0, w1, u1, z1, pitch, k, r0, r1, S, T, t0;
} Fly;

typedef struct {
    /* ---- the corpus */
    Bundle b;
    const uint8_t *chars, *kinds, *item_kind, *dir_hue, *file_hue;
    const uint64_t *line_off;
    const uint32_t *line_file, *file_line0, *file_dir, *item_file, *item_start, *item_end;
    const uint32_t *file_rows, *file_row0, *line_row0, *row_line, *item_rect_item;
    const uint16_t *line_indent, *line_len, *dir_depth, *file_cols, *file_cap, *row_col0, *row_len, *char_dx;
    const float *file_tint, *row_pos, *row_width, *item_rect;
    const double *world, *dir_rect, *dir_pad, *file_rect, *file_text, *file_pitch, *file_colw, *file_weight;
    const int32_t *dir_parent;
    float *char_x;        /* a proportional face's x of every character within its row */
    int n_files, n_lines, n_rows, n_dirs, n_items, n_item_rects;
    int64_t n_chars;
    Strs paths, item_names, item_kws, dir_labels, dir_tags;
    char name[256], tint_label[64];
    double W, H, A, median_pitch;
    bool prop, flat;

    /* ---- the scheme and the faces */
    float bg[3], page[3], bar[3], band[3], ink[3], kind_colors[10][3], item_colors[5][3];
    bool has_band;
    const uint8_t *glyphs, *ui_glyphs;
    int glyphs_w, glyphs_h, ui_w, ui_h;
    float gm[3], um[3];   /* cell_w, cell_h, char_aspect */
    const float *adv;
    double ui_aspect;
    const float *vt_curves;
    const uint16_t *vt_bands;
    int vt_curves_h, vt_bands_h;
    bool vector_text;

    /* ---- derived per file and directory */
    double *file_z0, *file_h, *file_top, *dir_z0, *dir_h;
    double *file_scale, *dir_scale, *ppl;
    uint8_t *visible, *lod, *dimmed, *hits_by_file;
    int64_t *hit_count;
    uint32_t *item_rect_file;
    int *items_by_file_off, *items_by_file;     /* CSR: items of file f */
    int grid_gx, grid_gy, *grid_off, *grid_files; /* CSR: files per hover cell */
    int *dir_order;

    /* ---- GL */
    SDL_Window *win;
    SDL_GLContext ctx;
    GLuint prog[P_COUNT];
    struct { const char *name; GLint loc; } uloc[P_COUNT][MAX_UNIFORMS];
    int n_uloc[P_COUNT];
    GLuint quad_vbo, quad_vao, rect_vao, rect_vbo, text_vao, text_vbo;
    GLuint tex_chars, tex_kinds, tex_glyphs, tex_ui, tex_line_f, tex_line_u, tex_file_f, tex_file_u,
           tex_dir_f, tex_char_f, tex_vt_curves, tex_vt_bands;
    uint8_t *file_u;
    int file_u_rows;
    int64_t gpu_bytes;
    const char *shader_dir;

    /* ---- window */
    int fb_w, fb_h;
    double px, top_h, bottom_h, map_y0, map_h;

    /* ---- camera and motion */
    double cx, cy, zoom, zoom_vel, anchor[2];
    bool has_anchor;
    Fly fly;
    bool proj3d, heights, tint_on, tilting;
    double tilt, yaw, M[16], M_inv[16], focus_w, z_focus, eye[3];
    bool has_minv;

    /* ---- input */
    bool dragging, pressed, has_last_press, has_cursor;
    double drag[2], press[2], last_press[3], cursor[2], cursor_pt[2];
    bool zoomed;
    double zoomed_rect[4], zoomed_back[3];

    /* ---- hover, crumb, filter, results */
    bool has_hover;
    int hover[3];         /* file, line within the file (or -1), column */
    char crumb[1024];
    char filter_text[256];
    bool filter_focus, swallow_char;
    bool has_results;     /* a search ran (it may have no hits) */
    int n_res, res_len, result_i, current_file;
    int *res_file, *res_col;
    int64_t *res_line;
    PanelRow *panel_rows;
    int n_panel_rows;
    double panel_scroll, panel_rect[4], filter_rect[4];
    bool has_panel_rect, has_filter_rect;

    /* ---- per-frame buffers and runs */
    FBuf rects, texts, ui_boxes, ui_texts;
    bool running;
    double last_t, fps_t;
    int fps_n, frame, frames;
    const char *screenshot;
    bool ready_sent;
} App;

/* ---------------------------------------------------------------- GL helpers */

static char *read_text(const char *path)
{
    FILE *f = fopen(path, "rb");
    if (!f)
        die("error: cannot read %s", path);
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *s = malloc(n + 1);
    if (fread(s, 1, n, f) != (size_t)n)
        die("error: cannot read %s", path);
    s[n] = 0;
    fclose(f);
    return s;
}

/* One stage of a shaders/<name>.glsl for GLSL ES 3.00: everything above
   #version is dropped (the file's comments), the version line becomes the
   ES header with explicit precisions (ES has no default precision for the
   integer samplers, and a fragment int is only mediump), and prefix goes
   right after it, as the Python viewer pastes the vector tier. */
static char *es_stage(const char *text, size_t len, const char *prefix)
{
    static const char *HEADER =
        "#version 300 es\n"
        "precision highp float;\n"
        "precision highp int;\n"
        "precision highp sampler2D;\n"
        "precision highp usampler2D;\n";
    const char *v = NULL;
    for (const char *p = text; p < text + len; p = strchr(p, '\n') ? strchr(p, '\n') + 1 : text + len)
        if (!strncmp(p, "#version", 8)) {
            v = p;
            break;
        }
    if (!v)
        die("error: a shader stage has no #version line%s", "");
    const char *body = strchr(v, '\n');
    body = body ? body + 1 : text + len;
    size_t n_body = text + len - body;
    size_t cap = strlen(HEADER) + strlen(prefix) + n_body + 2;
    char *out = malloc(cap);
    snprintf(out, cap, "%s%s", HEADER, prefix);
    size_t k = strlen(out);
    memcpy(out + k, body, n_body);
    out[k + n_body] = 0;
    return out;
}

static GLuint compile(GLenum kind, const char *src, const char *name)
{
    GLuint sh = glCreateShader(kind);
    glShaderSource(sh, 1, &src, NULL);
    glCompileShader(sh);
    GLint ok;
    glGetShaderiv(sh, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[4096];
        glGetShaderInfoLog(sh, sizeof log, NULL, log);
        fprintf(stderr, "%s.glsl (%s): %s\n", name, kind == GL_VERTEX_SHADER ? "vertex" : "fragment", log);
        exit(1);
    }
    return sh;
}

static GLuint load_program(App *a, const char *name, const char *frag_prefix)
{
    char path[1024];
    snprintf(path, sizeof path, "%s/%s.glsl", a->shader_dir, name);
    char *src = read_text(path);
    const char *marker = "// ---- fragment ----";
    char *split = strstr(src, marker);
    if (!split || strstr(split + 1, marker))
        die("error: %s: expected one fragment marker", path);
    char *vs = es_stage(src, split - src, "");
    char *fs = es_stage(split, strlen(split), frag_prefix);
    GLuint prog = glCreateProgram();
    glAttachShader(prog, compile(GL_VERTEX_SHADER, vs, name));
    glAttachShader(prog, compile(GL_FRAGMENT_SHADER, fs, name));
    glLinkProgram(prog);
    GLint ok;
    glGetProgramiv(prog, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[4096];
        glGetProgramInfoLog(prog, sizeof log, NULL, log);
        fprintf(stderr, "%s.glsl: %s\n", name, log);
        exit(1);
    }
    free(src);
    free(vs);
    free(fs);
    return prog;
}

/* the uniform's location in program p, cached (a WebGL lookup is a string round trip) */
static GLint U(App *a, int p, const char *name)
{
    for (int i = 0; i < a->n_uloc[p]; i++)
        if (!strcmp(a->uloc[p][i].name, name))
            return a->uloc[p][i].loc;
    GLint loc = glGetUniformLocation(a->prog[p], name);
    if (a->n_uloc[p] < MAX_UNIFORMS) {
        a->uloc[p][a->n_uloc[p]].name = name;
        a->uloc[p][a->n_uloc[p]].loc = loc;
        a->n_uloc[p]++;
    }
    return loc;
}

/* A texture of `n` texels of `texel` bytes laid in rows of `w`, the last
   row padded: allocated whole, then the full rows and the remainder
   uploaded straight from data, so nothing is copied to pad it. */
static GLuint tex_rows(GLenum internal, GLenum fmt, GLenum type, int texel, int w, int64_t n,
                       const void *data, int64_t *bytes)
{
    int h = n > 0 ? (int)((n + w - 1) / w) : 1;
    GLuint t;
    glGenTextures(1, &t);
    glBindTexture(GL_TEXTURE_2D, t);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, internal, w, h, 0, fmt, type, NULL);
    int full = (int)(n / w), rem = (int)(n % w);
    if (full > 0)
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, full, fmt, type, data);
    if (rem > 0)
        glTexSubImage2D(GL_TEXTURE_2D, 0, 0, full, rem, 1, fmt, type, (const uint8_t *)data + (int64_t)full * w * texel);
    if (bytes)
        *bytes += (int64_t)w * h * texel;
    return t;
}

static GLuint tex_image(GLenum internal, GLenum fmt, GLenum type, int w, int h, const void *data, bool mipmap)
{
    GLuint t;
    glGenTextures(1, &t);
    glBindTexture(GL_TEXTURE_2D, t);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, mipmap ? GL_LINEAR_MIPMAP_LINEAR : GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, mipmap ? GL_LINEAR : GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D, 0, internal, w, h, 0, fmt, type, data);
    if (mipmap)
        glGenerateMipmap(GL_TEXTURE_2D);
    return t;
}

/* a VAO with the unit quad at location 0 and per-instance float attributes
   {location, size} interleaved in one dynamic VBO */
static GLuint instanced_vao(GLuint quad_vbo, const int (*attribs)[2], int n, GLuint *vbo)
{
    GLuint vao;
    glGenVertexArrays(1, &vao);
    glBindVertexArray(vao);
    glBindBuffer(GL_ARRAY_BUFFER, quad_vbo);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 8, (void *)0);
    glGenBuffers(1, vbo);
    glBindBuffer(GL_ARRAY_BUFFER, *vbo);
    int stride = 0;
    for (int i = 0; i < n; i++)
        stride += 4 * attribs[i][1];
    intptr_t off = 0;
    for (int i = 0; i < n; i++) {
        glEnableVertexAttribArray(attribs[i][0]);
        glVertexAttribPointer(attribs[i][0], attribs[i][1], GL_FLOAT, GL_FALSE, stride, (void *)off);
        glVertexAttribDivisor(attribs[i][0], 1);
        off += 4 * attribs[i][1];
    }
    glBindVertexArray(0);
    return vao;
}

/* ---------------------------------------------------------------- the corpus */

static void compute_heights(App *a);
static void build_hover_grid(App *a);

static void load_corpus(App *a, const char *path)
{
    Bundle *b = &a->b;
    char buf[1100];
    if (!bundle_load(b, path)) {
        snprintf(buf, sizeof buf, "%s/viewer.bin", path);
        if (!bundle_load(b, buf))
            die("error: no viewer.bin at %s\n\nExport an atlas first:\n\n"
                "    ./atlas_export.py data/<name>_atlas", path);
    }
    a->chars = get(b, "chars", "u1");
    a->kinds = get(b, "kinds", "u1");
    a->line_off = get(b, "line_off", "u8");
    a->line_file = get(b, "line_file", "u4");
    a->line_indent = get(b, "line_indent", "u2");
    a->line_len = get(b, "line_len", "u2");
    a->file_line0 = get(b, "file_line0", "u4");
    a->file_dir = get(b, "file_dir", "u4");
    a->item_file = get(b, "item_file", "u4");
    a->item_start = get(b, "item_start", "u4");
    a->item_end = get(b, "item_end", "u4");
    a->item_kind = get(b, "item_kind", "u1");
    a->file_tint = get_opt(b, "file_tint");
    a->world = get(b, "world", "f8");
    a->dir_rect = get(b, "dir_rect", "f8");
    a->dir_pad = get(b, "dir_pad", "f8");
    a->dir_depth = get(b, "dir_depth", "u2");
    a->dir_hue = get(b, "dir_hue", "u1");
    a->file_rect = get(b, "file_rect", "f8");
    a->file_text = get_opt(b, "file_text");
    if (!a->file_text)
        a->file_text = a->file_rect;
    a->file_pitch = get(b, "file_pitch", "f8");
    a->file_cols = get(b, "file_cols", "u2");
    a->file_rows = get(b, "file_rows", "u4");
    a->file_cap = get(b, "file_cap", "u2");
    a->file_colw = get(b, "file_colw", "f8");
    a->file_hue = get(b, "file_hue", "u1");
    a->file_row0 = get(b, "file_row0", "u4");
    a->file_weight = get_opt(b, "file_weight");
    a->line_row0 = get(b, "line_row0", "u4");
    a->row_line = get(b, "row_line", "u4");
    a->row_col0 = get(b, "row_col0", "u2");
    a->row_len = get(b, "row_len", "u2");
    a->row_pos = get(b, "row_pos", "f4");
    a->row_width = get_opt(b, "row_width");
    a->item_rect = get(b, "item_rect", "f4");
    a->item_rect_item = get(b, "item_rect_item", "u4");
    a->char_dx = get_opt(b, "char_dx");
    a->dir_parent = get(b, "dir_parent", "i4");

    a->n_files = (int)count_of(b, "file_rect");
    a->n_lines = (int)count_of(b, "line_file");
    a->n_rows = (int)count_of(b, "row_pos");
    a->n_dirs = (int)count_of(b, "dir_rect");
    a->n_items = (int)count_of(b, "item_file");
    a->n_item_rects = (int)count_of(b, "item_rect");
    a->n_chars = (int64_t)count_of(b, "chars");
    if (a->n_chars > (int64_t)CHARS_W * CHARS_W)
        die("error: the corpus exceeds the %s texture; the resident design stops here", "16384x16384");
    a->W = a->world[0];
    a->H = a->world[1];
    a->A = ((double *)get(b, "char_aspect", "f8"))[0];
    a->prop = ((double *)get(b, "proportional", "f8"))[0] != 0.0;
    a->flat = ((double *)get(b, "flat", "f8"))[0] != 0.0;
    a->median_pitch = ((double *)get(b, "median_pitch", "f8"))[0];
    a->paths = strs(b, "s_paths");
    a->item_names = strs(b, "s_item_names");
    a->item_kws = strs(b, "s_item_kws");
    a->dir_labels = strs(b, "s_dir_labels");
    a->dir_tags = strs(b, "s_dir_tags");
    str_at(strs(b, "s_name"), 0, a->name, sizeof a->name);
    str_at(strs(b, "s_tint"), 0, a->tint_label, sizeof a->tint_label);
    if (!a->file_tint)
        a->tint_label[0] = 0;

    /* a proportional face's character x from its quantized steps along the row */
    if (a->prop) {
        if (!a->char_dx || !a->row_width)
            die("error: %s: a proportional layout needs char_dx and row_width; rerun ./atlas_export.py", path);
        double scale = ((double *)get(b, "char_x_scale", "f8"))[0];
        a->char_x = malloc(a->n_chars * sizeof(float));
        int64_t o = 0;
        for (int r = 0; r < a->n_rows; r++) {
            int64_t q = 0;
            for (int k = 0; k < a->row_len[r]; k++, o++) {
                q += a->char_dx[o];
                a->char_x[o] = (float)(q / scale);
            }
        }
        if (o != a->n_chars)
            die("error: %s: the layout's rows do not cover the characters; rerun ./atlas_layout.py", path);
    }

    /* the scheme and the faces */
    memcpy(a->bg, get(b, "sc_ground", "f4"), 12);
    memcpy(a->page, get(b, "sc_page", "f4"), 12);
    memcpy(a->bar, get(b, "sc_bar", "f4"), 12);
    memcpy(a->ink, get(b, "sc_ink", "f4"), 12);
    const float *band = get_opt(b, "sc_band");
    a->has_band = band != NULL;
    memcpy(a->band, band ? band : a->bg, 12);
    memcpy(a->kind_colors, get(b, "sc_kinds", "f4"), sizeof a->kind_colors);
    memcpy(a->item_colors, get(b, "sc_items", "f4"), sizeof a->item_colors);
    Arr *g = bundle_get(b, "glyphs"), *ug = bundle_get(b, "ui_glyphs");
    a->glyphs = g->p;
    a->glyphs_h = (int)g->dim[0];
    a->glyphs_w = (int)g->dim[1];
    a->ui_glyphs = ug->p;
    a->ui_h = (int)ug->dim[0];
    a->ui_w = (int)ug->dim[1];
    memcpy(a->gm, get(b, "glyph_m", "f4"), 12);
    memcpy(a->um, get(b, "ui_m", "f4"), 12);
    a->ui_aspect = a->um[0] / a->um[1];
    a->adv = get(b, "adv", "f4");
    Arr *vc = bundle_opt(b, "vt_curves"), *vb = bundle_opt(b, "vt_bands");
    if (a->vector_text && vc && vb) {
        a->vt_curves = vc->p;
        a->vt_curves_h = (int)vc->dim[0];
        a->vt_bands = vb->p;
        a->vt_bands_h = (int)vb->dim[0];
    } else
        a->vector_text = false;

    /* derived per file and directory */
    int nf = a->n_files, nd = a->n_dirs;
    a->file_z0 = calloc(nf, sizeof(double));
    a->file_h = calloc(nf, sizeof(double));
    a->file_top = calloc(nf, sizeof(double));
    a->dir_z0 = calloc(nd, sizeof(double));
    a->dir_h = calloc(nd, sizeof(double));
    a->file_scale = calloc(nf, sizeof(double));
    a->dir_scale = calloc(nd, sizeof(double));
    a->ppl = calloc(nf, sizeof(double));
    a->visible = calloc(nf, 1);
    a->lod = calloc(nf, 1);
    a->dimmed = calloc(nf, 1);
    a->hits_by_file = calloc(nf, 1);
    a->hit_count = calloc(nf, sizeof(int64_t));
    for (int f = 0; f < nf; f++)
        a->file_scale[f] = 1.0;
    for (int d = 0; d < nd; d++)
        a->dir_scale[d] = 1.0;
    compute_heights(a);

    /* the items of each file, and the file of each item rectangle */
    a->item_rect_file = malloc((a->n_item_rects + 1) * sizeof(uint32_t));
    for (int i = 0; i < a->n_item_rects; i++)
        a->item_rect_file[i] = a->item_file[a->item_rect_item[i]];
    a->items_by_file_off = calloc(nf + 1, sizeof(int));
    a->items_by_file = malloc((a->n_items + 1) * sizeof(int));
    for (int i = 0; i < a->n_items; i++)
        a->items_by_file_off[a->item_file[i] + 1]++;
    for (int f = 0; f < nf; f++)
        a->items_by_file_off[f + 1] += a->items_by_file_off[f];
    int *fill = calloc(nf, sizeof(int));
    for (int i = 0; i < a->n_items; i++) {
        int f = a->item_file[i];
        a->items_by_file[a->items_by_file_off[f] + fill[f]++] = i;
    }
    free(fill);

    /* directories draw in depth order so parents go under children */
    a->dir_order = malloc(nd * sizeof(int));
    int k = 0, max_depth = 0;
    for (int d = 0; d < nd; d++)
        max_depth = a->dir_depth[d] > max_depth ? a->dir_depth[d] : max_depth;
    for (int depth = 0; depth <= max_depth; depth++)
        for (int d = 0; d < nd; d++)
            if (a->dir_depth[d] == depth)
                a->dir_order[k++] = d;
    build_hover_grid(a);
    a->current_file = -1;
    a->result_i = -1;
}

/* z base and height per file and directory from the layout's weight:
   directories are terraces of DIR_STEP per level, files sit on their
   directory's terrace with a height of H_MAX * sqrt(weight / max). With the
   heights off, or for a flat layout (a book), nothing rises. */
static void compute_heights(App *a)
{
    bool flat = a->flat || !a->heights;
    for (int d = 0; d < a->n_dirs; d++) {
        double depth = a->dir_depth[d];
        a->dir_z0[d] = (depth - 1 > 0 ? depth - 1 : 0) * (flat ? 0.0 : DIR_STEP);
        a->dir_h[d] = depth >= 1 ? (flat ? 0.0 : DIR_STEP) : 0.0;
    }
    double m = 1e-9;
    for (int f = 0; f < a->n_files; f++)
        if (a->file_weight && a->file_weight[f] > m)
            m = a->file_weight[f];
    if (!a->file_weight)
        m = 1.0;
    for (int f = 0; f < a->n_files; f++) {
        double w = a->file_weight ? (a->file_weight[f] > 0 ? a->file_weight[f] : 0.0) : 1.0;
        a->file_h[f] = flat ? 0.0 : H_MAX * sqrt(w / m);
        int d = a->file_dir[f];
        a->file_z0[f] = a->dir_z0[d] + a->dir_h[d];
        a->file_top[f] = a->file_z0[f] + a->file_h[f];
    }
}

static void build_hover_grid(App *a)
{
    int gx = 64, gy = (int)lround(64 * a->H / a->W);
    gy = gy < 1 ? 1 : gy;
    a->grid_gx = gx;
    a->grid_gy = gy;
    int *c0 = malloc(4 * a->n_files * sizeof(int));
    a->grid_off = calloc(gx * gy + 1, sizeof(int));
    for (int f = 0; f < a->n_files; f++) {
        const double *r = a->file_rect + 4 * f;
        int *c = c0 + 4 * f;
        c[0] = (int)clampd((int)(r[0] / a->W * gx), 0, gx - 1);
        c[1] = (int)clampd((int)(r[2] / a->W * gx), 0, gx - 1);
        c[2] = (int)clampd((int)(r[1] / a->H * gy), 0, gy - 1);
        c[3] = (int)clampd((int)(r[3] / a->H * gy), 0, gy - 1);
        for (int y = c[2]; y <= c[3]; y++)
            for (int x = c[0]; x <= c[1]; x++)
                a->grid_off[y * gx + x + 1]++;
    }
    for (int i = 0; i < gx * gy; i++)
        a->grid_off[i + 1] += a->grid_off[i];
    a->grid_files = malloc((a->grid_off[gx * gy] + 1) * sizeof(int));
    int *fill = calloc(gx * gy, sizeof(int));
    for (int f = 0; f < a->n_files; f++) {
        int *c = c0 + 4 * f;
        for (int y = c[2]; y <= c[3]; y++)
            for (int x = c[0]; x <= c[1]; x++) {
                int cell = y * gx + x;
                a->grid_files[a->grid_off[cell] + fill[cell]++] = f;
            }
    }
    free(fill);
    free(c0);
}

static int file_at(App *a, double wx, double wy)
{
    int ix = (int)floor(wx / a->W * a->grid_gx), iy = (int)floor(wy / a->H * a->grid_gy);
    if (ix < 0 || ix >= a->grid_gx || iy < 0 || iy >= a->grid_gy)
        return -1;
    int cell = iy * a->grid_gx + ix;
    for (int i = a->grid_off[cell]; i < a->grid_off[cell + 1]; i++) {
        int f = a->grid_files[i];
        const double *r = a->file_rect + 4 * f;
        if (r[0] <= wx && wx < r[2] && r[1] <= wy && wy < r[3])
            return f;
    }
    return -1;
}

/* (visual row, column within the row) of a character of a global line */
static int row_of(App *a, int64_t line, int64_t col, int64_t *rc)
{
    int r0 = a->line_row0[line], r1 = a->line_row0[line + 1];
    int row = r0;
    for (int r = r0 + 1; r < r1; r++)       /* the last row whose first column is at or before col */
        if (a->row_col0[r] <= col)
            row = r;
    *rc = col - a->row_col0[row];
    return row;
}

/* x of column rc of a visual row from the row's left, in line heights */
static double col_offset(App *a, int row, int64_t rc)
{
    if (!a->prop)
        return rc * a->A;
    int64_t start = (int64_t)a->line_off[a->row_line[row]] + a->row_col0[row];
    int64_t n = a->row_len[row];
    if (rc >= n)
        return a->row_width[row];
    int64_t idx = start + (rc < (n - 1 > 0 ? n - 1 : 0) ? rc : (n - 1 > 0 ? n - 1 : 0));
    if (idx > a->n_chars - 1)
        idx = a->n_chars - 1;
    return a->char_x[idx];
}

/* ---------------------------------------------------------------- window and GL */

static double map_w(App *a)
{
    double column = a->n_panel_rows ? (PANEL_PT + 2 * MARGIN_PT) * a->px : 0.0;
    return a->fb_w - column;
}

static void update_sizes(App *a)
{
    int ww, wh;
    SDL_GL_GetDrawableSize(a->win, &a->fb_w, &a->fb_h);
    SDL_GetWindowSize(a->win, &ww, &wh);
    a->px = ww ? (double)a->fb_w / ww : 1.0;
    /* the map's viewport is the window minus a top strip (the filter box),
       a bottom strip (the crumb trail and the status) and the right column
       while the panel is open */
    double s = a->px, m = MARGIN_PT * s, row = 13 * s + 2 * 5 * s * 0.6;
    a->top_h = m + row + m;
    a->bottom_h = m + row + m;
    a->map_y0 = a->top_h;
    a->map_h = a->fb_h - a->top_h - a->bottom_h;
    if (a->map_h < 1.0)
        a->map_h = 1.0;
}

static bool in_map(App *a, double sx, double sy)
{
    return 0 <= sx && sx < map_w(a) && a->map_y0 <= sy && sy < a->map_y0 + a->map_h;
}

static void open_window(App *a)
{
#if !defined(__EMSCRIPTEN__) && defined(BT_ANGLE_LIB_DIR)
    /* point SDL at ANGLE's libraries by absolute path, so the viewer runs
       from anywhere without DYLD_FALLBACK_LIBRARY_PATH */
    if (!getenv("SDL_VIDEO_GL_DRIVER")) {
        static char gles[1200], egl[1200];
        snprintf(gles, sizeof gles, "%s/libGLESv2.dylib", BT_ANGLE_LIB_DIR);
        snprintf(egl, sizeof egl, "%s/libEGL.dylib", BT_ANGLE_LIB_DIR);
        setenv("SDL_VIDEO_GL_DRIVER", gles, 0);
        setenv("SDL_VIDEO_EGL_DRIVER", egl, 0);
    }
#endif
    SDL_Init(SDL_INIT_VIDEO | SDL_INIT_EVENTS);
    SDL_SetHint(SDL_HINT_OPENGL_ES_DRIVER, "1");
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_EGL, 1);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_PROFILE_MASK, SDL_GL_CONTEXT_PROFILE_ES);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MAJOR_VERSION, 3);
    SDL_GL_SetAttribute(SDL_GL_CONTEXT_MINOR_VERSION, 0);
    SDL_GL_SetAttribute(SDL_GL_DOUBLEBUFFER, 1);
    SDL_GL_SetAttribute(SDL_GL_RED_SIZE, 8);
    SDL_GL_SetAttribute(SDL_GL_GREEN_SIZE, 8);
    SDL_GL_SetAttribute(SDL_GL_BLUE_SIZE, 8);
    SDL_GL_SetAttribute(SDL_GL_ALPHA_SIZE, 8);
    SDL_GL_SetAttribute(SDL_GL_DEPTH_SIZE, 24);
    int w = 1600, h = 1000;
#ifndef __EMSCRIPTEN__
    SDL_Rect r;             /* a window the screen cannot hold is clamped to its work area */
    if (SDL_GetDisplayUsableBounds(0, &r) == 0) {
        w = w < r.w ? w : r.w;
        h = h < r.h - 40 ? h : (r.h - 40) / 10 * 10;
    }
#endif
    char title[400];
    snprintf(title, sizeof title, "big-text: %s", a->name);
    a->win = SDL_CreateWindow(title, SDL_WINDOWPOS_CENTERED, SDL_WINDOWPOS_CENTERED, w, h,
                              SDL_WINDOW_OPENGL | SDL_WINDOW_ALLOW_HIGHDPI | SDL_WINDOW_SHOWN
                              | SDL_WINDOW_RESIZABLE);
    if (!a->win)
        die("SDL_CreateWindow: %s", SDL_GetError());
    a->ctx = SDL_GL_CreateContext(a->win);
    if (!a->ctx)
        die("SDL_GL_CreateContext (GLES 3.0): %s", SDL_GetError());
#ifndef __EMSCRIPTEN__
    SDL_GL_SetSwapInterval(a->frames > 0 ? 0 : 1);
#endif
    GLint max_tex;
    glGetIntegerv(GL_MAX_TEXTURE_SIZE, &max_tex);
    printf("GL: %s | %s | max texture %d\n", glGetString(GL_VERSION), glGetString(GL_RENDERER), max_tex);
    if (max_tex < CHARS_W)
        die("error: this GPU's textures stop short of the %s the corpus's characters are laid in", "16384 texels");
    update_sizes(a);
    SDL_StartTextInput();
}

static void upload_layout(App *a);

static void setup_gl(App *a)
{
    const char *vt_src = "";
    char *vt_buf = NULL;
    if (a->vector_text) {
        char path[1024];
        snprintf(path, sizeof path, "%s/vt_glyph.glsl", a->shader_dir);
        char *body = read_text(path);
        size_t n = strlen(body) + 64;
        vt_buf = malloc(n);
        snprintf(vt_buf, n, "#define VT_TEXT 1\n%s\n", body);
        free(body);
        vt_src = vt_buf;
    }
    for (int p = 0; p < P_COUNT; p++)
        a->prog[p] = load_program(a, PROG_NAMES[p], (p == P_LINE || p == P_GLYPH) ? vt_src : "");
    free(vt_buf);

    static const float quad[12] = {0, 0, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1};
    glGenBuffers(1, &a->quad_vbo);
    glBindBuffer(GL_ARRAY_BUFFER, a->quad_vbo);
    glBufferData(GL_ARRAY_BUFFER, sizeof quad, quad, GL_STATIC_DRAW);
    glGenVertexArrays(1, &a->quad_vao);
    glBindVertexArray(a->quad_vao);
    glEnableVertexAttribArray(0);
    glVertexAttribPointer(0, 2, GL_FLOAT, GL_FALSE, 8, (void *)0);
    static const int rect_attr[4][2] = {{1, 4}, {2, 4}, {3, 2}, {4, 1}};
    static const int text_attr[4][2] = {{1, 2}, {2, 2}, {3, 1}, {4, 4}};
    a->rect_vao = instanced_vao(a->quad_vbo, rect_attr, 4, &a->rect_vbo);
    a->text_vao = instanced_vao(a->quad_vbo, text_attr, 4, &a->text_vbo);
    glBindVertexArray(0);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 1);

    int64_t gpu = 0;
    a->tex_chars = tex_rows(GL_R8UI, GL_RED_INTEGER, GL_UNSIGNED_BYTE, 1, CHARS_W, a->n_chars, a->chars, &gpu);
    a->tex_kinds = tex_rows(GL_R8UI, GL_RED_INTEGER, GL_UNSIGNED_BYTE, 1, CHARS_W, a->n_chars, a->kinds, &gpu);
    upload_layout(a);
    if (a->prop) {        /* per character: its row and its x within the row */
        float *cf = malloc(a->n_chars * 2 * sizeof(float));
        int64_t o = 0;
        for (int r = 0; r < a->n_rows; r++)
            for (int k = 0; k < a->row_len[r]; k++, o++) {
                cf[2 * o] = (float)r;
                cf[2 * o + 1] = a->char_x[o];
            }
        a->tex_char_f = tex_rows(GL_RG32F, GL_RG, GL_FLOAT, 8, CHARS_W, a->n_chars, cf, &gpu);
        free(cf);
    }
    a->file_u_rows = (a->n_files + TEX_W - 1) / TEX_W;
    a->file_u_rows = a->file_u_rows < 1 ? 1 : a->file_u_rows;
    a->file_u = calloc((size_t)a->file_u_rows * TEX_W * 4, 1);
    a->tex_file_u = tex_image(GL_RGBA8UI, GL_RGBA_INTEGER, GL_UNSIGNED_BYTE, TEX_W, a->file_u_rows, a->file_u, false);
    gpu += (int64_t)a->file_u_rows * TEX_W * 4;
    a->tex_glyphs = tex_image(GL_R8, GL_RED, GL_UNSIGNED_BYTE, a->glyphs_w, a->glyphs_h, a->glyphs, true);
    a->tex_ui = tex_image(GL_R8, GL_RED, GL_UNSIGNED_BYTE, a->ui_w, a->ui_h, a->ui_glyphs, true);
    gpu += (int64_t)(a->glyphs_w * a->glyphs_h + a->ui_w * a->ui_h) * 4 / 3;
    if (a->vector_text) {
        a->tex_vt_curves = tex_image(GL_RGBA32F, GL_RGBA, GL_FLOAT, 4096, a->vt_curves_h, a->vt_curves, false);
        a->tex_vt_bands = tex_image(GL_RG16UI, GL_RG_INTEGER, GL_UNSIGNED_SHORT, 4096, a->vt_bands_h, a->vt_bands, false);
        gpu += (int64_t)4096 * (a->vt_curves_h * 16 + a->vt_bands_h * 4);
    }
    a->gpu_bytes += gpu;

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
    glDisable(GL_DEPTH_TEST);
    static const char *SAMPLERS[11] = {"uChars", "uKinds", "uGlyphs", "uLineF", "uLineU", "uFileF",
                                       "uFileU", "uDirF", "vt_curves", "vt_bands", "uCharF"};
    for (int p = 0; p < P_COUNT; p++) {
        glUseProgram(a->prog[p]);
        for (int i = 0; i < 11; i++) {
            GLint loc = U(a, p, SAMPLERS[i]);
            if (loc >= 0)
                glUniform1i(loc, p == P_TEXT && i == 2 ? UI_UNIT : i);
        }
        GLint loc;
        if ((loc = U(a, p, "uAdv")) >= 0)
            glUniform1fv(loc, 224, a->adv);
        if ((loc = U(a, p, "uAdvMax")) >= 0)
            glUniform1f(loc, a->gm[2]);
        if ((loc = U(a, p, "uProp")) >= 0)
            glUniform1i(loc, a->prop ? 1 : 0);
        if ((loc = U(a, p, "uHue")) >= 0)
            glUniform3fv(loc, 12, &HUES[0][0]);
        if ((loc = U(a, p, "uKindColor")) >= 0)
            glUniform3fv(loc, 10, &a->kind_colors[0][0]);
        if ((loc = U(a, p, "uPage")) >= 0)
            glUniform3fv(loc, 1, a->page);
        if ((loc = U(a, p, "uGround")) >= 0)
            glUniform3fv(loc, 1, a->bg);
        if ((loc = U(a, p, "uBar")) >= 0)
            glUniform3fv(loc, 1, a->bar);
        if ((loc = U(a, p, "uBandColor")) >= 0)
            glUniform3fv(loc, 1, a->band);
        if ((loc = U(a, p, "uBandFlat")) >= 0)
            glUniform1i(loc, a->has_band ? 1 : 0);
        if ((loc = U(a, p, "uInk")) >= 0)
            glUniform3fv(loc, 1, a->ink);
        if ((loc = U(a, p, "uGlyph")) >= 0) {
            if (p == P_TEXT)
                glUniform4f(loc, a->um[0], a->um[1], (float)a->ui_w, (float)a->ui_h);
            else
                glUniform4f(loc, a->gm[0], a->gm[1], (float)a->glyphs_w, (float)a->glyphs_h);
        }
        if ((loc = U(a, p, "uCharAspect")) >= 0)
            glUniform1f(loc, (float)a->A);
        if ((loc = U(a, p, "uVtMin")) >= 0)
            glUniform1f(loc, (float)VT_MIN_PPL);
    }
}

/* (Re)upload the layout textures: one texel per visual row (position, row
   index in file, width; byte offset, file, indent | len), three per file
   and three per directory. */
static void upload_layout(App *a)
{
    GLuint old[4] = {a->tex_line_f, a->tex_line_u, a->tex_file_f, a->tex_dir_f};
    for (int i = 0; i < 4; i++)
        if (old[i])
            glDeleteTextures(1, &old[i]);
    int64_t gpu = 0;
    int nr = a->n_rows;
    float *lf = malloc((size_t)nr * 4 * sizeof(float));
    uint32_t *lu = malloc((size_t)nr * 4 * sizeof(uint32_t));
    for (int r = 0; r < nr; r++) {
        uint32_t line = a->row_line[r], f = a->line_file[line];
        lf[4 * r] = a->row_pos[2 * r];
        lf[4 * r + 1] = a->row_pos[2 * r + 1];
        lf[4 * r + 2] = (float)((int64_t)r - a->file_row0[f]);   /* the row's index in its file, for LOD-0 sampling */
        if (a->row_width)
            lf[4 * r + 3] = a->row_width[r];
        else
            lf[4 * r + 3] = (float)((a->row_len[r] < a->file_cap[f] ? a->row_len[r] : a->file_cap[f]) * a->A);
        uint64_t off = a->line_off[line] + a->row_col0[r];
        uint32_t indent = a->row_col0[r] == 0 ? a->line_indent[line] : 0;
        lu[4 * r] = (uint32_t)(off & 0xFFFFFFFFu);
        lu[4 * r + 1] = f;
        lu[4 * r + 2] = indent | ((uint32_t)a->row_len[r] << 16);
        lu[4 * r + 3] = (uint32_t)(off >> 32);
    }
    a->tex_line_f = tex_rows(GL_RGBA32F, GL_RGBA, GL_FLOAT, 16, TEX_W, nr, lf, &gpu);
    a->tex_line_u = tex_rows(GL_RGBA32UI, GL_RGBA_INTEGER, GL_UNSIGNED_INT, 16, TEX_W, nr, lu, &gpu);
    free(lf);
    free(lu);
    int nf = a->n_files, nd = a->n_dirs;
    float *ff = calloc((size_t)3 * nf * 4, sizeof(float));
    for (int f = 0; f < nf; f++) {
        float *t = ff + 12 * f;
        for (int k = 0; k < 4; k++)
            t[k] = (float)a->file_rect[4 * f + k];
        t[4] = (float)a->file_pitch[f];
        t[5] = (float)a->file_colw[f];
        t[6] = (float)a->file_cap[f];
        t[7] = (float)a->file_hue[f];
        t[8] = (float)a->file_z0[f];
        t[9] = (float)a->file_h[f];
        t[10] = a->file_tint ? a->file_tint[f] : 0.0f;
    }
    a->tex_file_f = tex_rows(GL_RGBA32F, GL_RGBA, GL_FLOAT, 16, TEX_W, 3 * (int64_t)nf, ff, &gpu);
    free(ff);
    float *df = calloc((size_t)3 * nd * 4, sizeof(float));
    for (int d = 0; d < nd; d++) {
        float *t = df + 12 * d;
        for (int k = 0; k < 4; k++)
            t[k] = (float)a->dir_rect[4 * d + k];
        t[4] = (float)a->dir_pad[d];
        t[5] = (float)a->dir_depth[d];
        t[6] = (float)a->dir_hue[d];
        t[8] = (float)a->dir_z0[d];
        t[9] = (float)a->dir_h[d];
    }
    a->tex_dir_f = tex_rows(GL_RGBA32F, GL_RGBA, GL_FLOAT, 16, TEX_W, 3 * (int64_t)nd, df, &gpu);
    free(df);
    if (!a->gpu_bytes)
        a->gpu_bytes = gpu;
}

/* Bind every texture to its unit. Called after anything created or
   re-uploaded a texture: creation binds on whatever unit is active. */
static void bind_textures(App *a)
{
    GLuint units[12] = {a->tex_chars, a->tex_kinds, a->tex_glyphs, a->tex_line_f, a->tex_line_u,
                        a->tex_file_f, a->tex_file_u, a->tex_dir_f, a->tex_vt_curves, a->tex_vt_bands,
                        a->tex_char_f, a->tex_ui};
    for (int i = 0; i < 12; i++) {
        glActiveTexture(GL_TEXTURE0 + i);
        glBindTexture(GL_TEXTURE_2D, units[i]);
    }
    glActiveTexture(GL_TEXTURE0 + 5);
}

/* ---------------------------------------------------------------- camera */

static void view_rect(App *a, double *v);

static double ref_pitch_at(App *a, double wx, double wy)
{
    int f = file_at(a, wx, wy);
    return f >= 0 ? a->file_pitch[f] : a->median_pitch;
}

static double ref_pitch(App *a)
{
    return ref_pitch_at(a, a->cx, a->cy);
}

static void view2d(App *a, double *v)
{
    double w = map_w(a) / a->zoom, h = a->map_h / a->zoom;
    v[0] = a->cx - w / 2;
    v[1] = a->cy - h / 2;
    v[2] = a->cx + w / 2;
    v[3] = a->cy + h / 2;
}

/* World (x, y, z) to clip. 2D: orthographic, exactly the (world - offset)
   * scale mapping. 3D: a perspective camera at distance D from the focus,
   tilted from top-down by tilt and turned by yaw, with D chosen so that one
   world unit at the focus is still zoom device pixels. */
static void camera_matrix(App *a)
{
    double *M = a->M, mw = map_w(a);
    if (!a->proj3d) {
        double v[4];
        view2d(a, v);
        double sx = 2.0 * a->zoom / mw, sy = 2.0 * a->zoom / a->map_h;
        double m[16] = {sx, 0, 0, -1 - v[0] * sx, 0, -sy, 0, 1 + v[1] * sy, 0, 0, 0, 0, 0, 0, 0, 1};
        memcpy(M, m, sizeof m);
        a->focus_w = 1.0;
        a->has_minv = false;
        return;
    }
    double th = a->tilt * M_PI / 180, ph = a->yaw * M_PI / 180;
    double tan_h = tan(FOVY * M_PI / 180 / 2);
    double D = a->map_h / (2.0 * a->zoom * tan_h);
    double F[3] = {a->cx, a->cy, a->z_focus};
    double eye[3] = {F[0] + D * sin(th) * sin(ph), F[1] + D * sin(th) * cos(ph), F[2] + D * cos(th)};
    double fwd[3] = {F[0] - eye[0], F[1] - eye[1], F[2] - eye[2]};
    double n = sqrt(fwd[0] * fwd[0] + fwd[1] * fwd[1] + fwd[2] * fwd[2]);
    for (int i = 0; i < 3; i++)
        fwd[i] /= n;
    double right[3] = {cos(ph), -sin(ph), 0.0};
    double up[3] = {fwd[1] * right[2] - fwd[2] * right[1], fwd[2] * right[0] - fwd[0] * right[2],
                    fwd[0] * right[1] - fwd[1] * right[0]};
    double V[16] = {0};
    for (int i = 0; i < 3; i++) {
        V[i] = right[i];
        V[4 + i] = up[i];
        V[8 + i] = -fwd[i];
    }
    for (int r = 0; r < 3; r++)
        V[r * 4 + 3] = -(V[r * 4] * eye[0] + V[r * 4 + 1] * eye[1] + V[r * 4 + 2] * eye[2]);
    V[15] = 1.0;
    double near = D * 0.02 > 0.05 ? D * 0.02 : 0.05, far = D * 12 + 4000.0;
    double aspect = mw / a->map_h;
    double P[16] = {0};
    P[0] = 1.0 / (tan_h * aspect);
    P[5] = 1.0 / tan_h;
    P[10] = -(far + near) / (far - near);
    P[11] = -2.0 * far * near / (far - near);
    P[14] = -1.0;
    mat4_mul(M, P, V);
    a->focus_w = D;
    memcpy(a->eye, eye, sizeof eye);
    a->has_minv = mat4_inv(M, a->M_inv);
}

/* the world point on the plane z = wz under device pixel (sx, sy) in the
   3D camera; false if the ray does not hit it in front */
static bool unproject(App *a, double sx, double sy, double wz, double *out)
{
    double nx = sx / map_w(a) * 2.0 - 1.0, ny = 1.0 - (sy - a->map_y0) / a->map_h * 2.0;
    double q0[4] = {nx, ny, -1.0, 1.0}, q1[4] = {nx, ny, 1.0, 1.0}, p0[4], p1[4];
    mat4_xform(a->M_inv, q0, p0);
    mat4_xform(a->M_inv, q1, p1);
    for (int i = 0; i < 3; i++) {
        p0[i] /= p0[3];
        p1[i] /= p1[3];
    }
    double d[3] = {p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2]};
    if (fabs(d[2]) < 1e-12)
        return false;
    double t = (wz - p0[2]) / d[2];
    if (t < 0)
        return false;
    for (int i = 0; i < 3; i++)
        out[i] = p0[i] + t * d[i];
    return true;
}

static void project(App *a, double wx, double wy, double wz, double *sx, double *sy)
{
    double v[4] = {wx, wy, wz, 1.0}, c[4];
    mat4_xform(a->M, v, c);
    double w = fabs(c[3]) > 1e-9 ? c[3] : 1e-9;
    *sx = (c[0] / w + 1.0) * 0.5 * map_w(a);
    *sy = (1.0 - c[1] / w) * 0.5 * a->map_h + a->map_y0;
}

/* The world rectangle in view: exact in 2D; in 3D the bounding box of the
   viewport corners unprojected onto the plane at three heights (rays that
   miss count as far away), clamped to a few worlds around. */
static void view_rect(App *a, double *v)
{
    if (!a->proj3d || !a->has_minv) {
        view2d(a, v);
        return;
    }
    double far = 3.0 * (a->W > a->H ? a->W : a->H), mw = map_w(a);
    double x0 = 1e300, y0 = 1e300, x1 = -1e300, y1 = -1e300;
    double zs[3] = {0.0, a->z_focus, a->z_focus + H_MAX};
    double corners[4][2] = {{0, a->map_y0}, {mw, a->map_y0}, {0, a->map_y0 + a->map_h}, {mw, a->map_y0 + a->map_h}};
    for (int i = 0; i < 3; i++)
        for (int c = 0; c < 4; c++) {
            double p[3];
            if (!unproject(a, corners[c][0], corners[c][1], zs[i], p)) {
                x0 = fmin(x0, a->cx - far);
                y0 = fmin(y0, a->cy - far);
                x1 = fmax(x1, a->cx + far);
                y1 = fmax(y1, a->cy + far);
            } else {
                x0 = fmin(x0, p[0]);
                y0 = fmin(y0, p[1]);
                x1 = fmax(x1, p[0]);
                y1 = fmax(y1, p[1]);
            }
        }
    v[0] = fmax(x0, -far);
    v[1] = fmax(y0, -far);
    v[2] = fmin(x1, a->W + far);
    v[3] = fmin(y1, a->H + far);
}

static double fit_zoom(App *a)
{
    return fmin(map_w(a) * 0.94 / a->W, a->map_h * 0.94 / a->H);
}

static bool is_fitted(App *a)
{
    return fabs(a->zoom - fit_zoom(a)) < 1e-9 * a->zoom && a->cx == a->W / 2 && a->cy == a->H / 2;
}

/* half the fit, up to 400 device pixels per line for the file at the centre */
static void zoom_limits(App *a, double pitch, double *lo, double *hi)
{
    *lo = fit_zoom(a) * 0.5;
    *hi = 400.0 / (pitch > 0 ? pitch : ref_pitch(a));
}

static double clamp_zoom(App *a, double z, double pitch)
{
    double lo, hi;
    zoom_limits(a, pitch, &lo, &hi);
    return clampd(z, lo, hi);
}

static void fit(App *a)
{
    a->fly.on = false;
    a->zoom_vel = 0.0;
    a->cx = a->W / 2;
    a->cy = a->H / 2;
    a->zoom = fit_zoom(a);
    if (a->proj3d) {      /* perspective: back off until the map fits */
        a->z_focus = 0.0;
        double corners[4][2] = {{0, 0}, {a->W, 0}, {0, a->H}, {a->W, a->H}};
        for (int it = 0; it < 12; it++) {
            camera_matrix(a);
            double over = 0.0;
            for (int c = 0; c < 4; c++) {
                double v[4] = {corners[c][0], corners[c][1], 0, 1}, q[4];
                mat4_xform(a->M, v, q);
                double w = q[3] > 1e-6 ? q[3] : 1e-6;
                over = fmax(over, fmax(fabs(q[0] / w), fabs(q[1] / w)));
            }
            if (over <= 0.97)
                break;
            a->zoom /= over * 1.04;
        }
    }
}

static void screen_to_world_z(App *a, double sx, double sy, double wz, double *wx, double *wy)
{
    if (a->proj3d && a->has_minv) {
        double p[3];
        if (!unproject(a, sx, sy, wz, p)) {      /* above the horizon: far along the view */
            *wx = a->cx + (sx - map_w(a) / 2) * 50.0 / a->zoom;
            *wy = a->cy - 50.0 * a->map_h / a->zoom;
            return;
        }
        *wx = p[0];
        *wy = p[1];
        return;
    }
    *wx = a->cx + (sx - map_w(a) / 2) / a->zoom;
    *wy = a->cy + (sy - a->map_y0 - a->map_h / 2) / a->zoom;
}

static void screen_to_world(App *a, double sx, double sy, double *wx, double *wy)
{
    screen_to_world_z(a, sx, sy, a->z_focus, wx, wy);
}

static void world_to_screen(App *a, double wx, double wy, double wz, double *sx, double *sy)
{
    if (a->proj3d) {
        project(a, wx, wy, wz, sx, sy);
        return;
    }
    *sx = (wx - a->cx) * a->zoom + map_w(a) / 2;
    *sy = (wy - a->cy) * a->zoom + a->map_y0 + a->map_h / 2;
}

static void zoom_about(App *a, double sx, double sy, double factor)
{
    double wx, wy;
    screen_to_world(a, sx, sy, &wx, &wy);
    /* the limit follows the file under the anchor, and a clamp never moves
       the zoom against the gesture when the map slides under it */
    double lo, hi;
    zoom_limits(a, ref_pitch_at(a, wx, wy), &lo, &hi);
    if (factor > 1.0)
        hi = fmax(hi, a->zoom);
    else
        lo = fmin(lo, a->zoom);
    double z = clampd(a->zoom * factor, lo, hi);
    if (z != a->zoom * factor)
        a->zoom_vel = 0.0;
    a->zoom = z;
    if (a->proj3d) {      /* keep the plane point under the cursor */
        camera_matrix(a);
        double nx, ny;
        screen_to_world(a, sx, sy, &nx, &ny);
        a->cx += wx - nx;
        a->cy += wy - ny;
        return;
    }
    a->cx = wx - (sx - map_w(a) / 2) / z;
    a->cy = wy - (sy - a->map_y0 - a->map_h / 2) / z;
}

static void set_zoom_ppl(App *a, double ppl)
{
    a->fly.on = false;
    a->zoom_vel = 0.0;
    a->zoom = clamp_zoom(a, ppl / ref_pitch(a), 0);
}

static void update_glide(App *a, double dt)
{
    if (a->zoom_vel == 0.0 || !a->has_anchor)
        return;
    double k = exp(-dt / ZOOM_TAU);
    double dlog = a->zoom_vel * ZOOM_TAU * (1.0 - k);
    a->zoom_vel *= k;
    if (fabs(a->zoom_vel) < 0.02)
        a->zoom_vel = 0.0;
    zoom_about(a, a->anchor[0], a->anchor[1], exp(dlog));
}

/* (centre, zoom, view width, pitch) of the view that holds rect with a margin */
static void fly_target(App *a, const double *rect, double margin, double *c1, double *z1, double *w1, double *pitch)
{
    double rw = (rect[2] - rect[0]) * (1 + 2 * margin), rh = (rect[3] - rect[1]) * (1 + 2 * margin);
    double mw = map_w(a);
    double w = fmax(fmax(rw, rh * mw / a->map_h), 1e-6);
    c1[0] = (rect[0] + rect[2]) / 2;
    c1[1] = (rect[1] + rect[3]) / 2;
    *pitch = ref_pitch_at(a, c1[0], c1[1]);
    *z1 = clamp_zoom(a, mw / w, *pitch);
    *w1 = mw / *z1;
}

/* van Wijk & Nuij smooth zoom-and-pan to a view containing rect */
static void fly_to(App *a, const double *rect, double margin, bool complete)
{
    Fly *f = &a->fly;
    double c1[2], z1, w1, pitch;
    fly_target(a, rect, margin, c1, &z1, &w1, &pitch);
    a->zoom_vel = 0.0;
    if (complete) {
        f->on = false;
        a->cx = c1[0];
        a->cy = c1[1];
        a->zoom = z1;
        return;
    }
    f->c0[0] = a->cx;
    f->c0[1] = a->cy;
    f->c1[0] = c1[0];
    f->c1[1] = c1[1];
    f->w0 = map_w(a) / a->zoom;
    f->w1 = w1;
    f->z1 = z1;
    f->pitch = pitch;
    f->u1 = hypot(c1[0] - a->cx, c1[1] - a->cy);
    double rho = FLY_RHO, w0 = f->w0;
    if (f->u1 < 1e-6 * fmax(w0, w1)) {
        f->mode = 0;
        f->k = w1 > w0 ? 1.0 : -1.0;
        f->S = fabs(log(w1 / w0)) / rho;
    } else {
        double u1 = f->u1, r4 = rho * rho * rho * rho;
        double b0 = (w1 * w1 - w0 * w0 + r4 * u1 * u1) / (2 * w0 * rho * rho * u1);
        double b1 = (w1 * w1 - w0 * w0 - r4 * u1 * u1) / (2 * w1 * rho * rho * u1);
        f->r0 = log(-b0 + sqrt(b0 * b0 + 1));
        f->r1 = log(-b1 + sqrt(b1 * b1 + 1));
        f->S = (f->r1 - f->r0) / rho;
        f->mode = 1;
    }
    f->T = clampd(f->S / FLY_V, 0.3, 1.2);
    f->t0 = now_s();
    f->on = true;
}

static void update_fly(App *a)
{
    Fly *f = &a->fly;
    if (!f->on)
        return;
    double t = (now_s() - f->t0) / f->T;
    if (t >= 1.0) {
        a->cx = f->c1[0];
        a->cy = f->c1[1];
        a->zoom = f->z1;
        f->on = false;
        return;
    }
    double s = f->S * t, rho = FLY_RHO, w, u;
    if (f->mode == 0) {
        w = f->w0 * exp(f->k * rho * s);
        u = 0.0;
    } else {
        double r0 = f->r0, w0 = f->w0;
        u = (w0 / (rho * rho)) * cosh(r0) * tanh(rho * s + r0) - (w0 / (rho * rho)) * sinh(r0);
        w = w0 * cosh(r0) / cosh(rho * s + r0);
    }
    double frac = f->u1 > 0 ? u / f->u1 : 1.0;
    a->cx = f->c0[0] + (f->c1[0] - f->c0[0]) * frac;
    a->cy = f->c0[1] + (f->c1[1] - f->c0[1]) * frac;
    a->zoom = clamp_zoom(a, map_w(a) / w, f->pitch);
}

static void set_proj(App *a, bool on3d)
{
    if (on3d == a->proj3d)
        return;
    a->proj3d = on3d;
    if (on3d && a->tilt == 0.0)
        a->tilt = 55.0;
    a->has_hover = false;
}

static void set_heights(App *a, bool on)
{
    if (a->flat || on == a->heights)
        return;
    a->heights = on;
    compute_heights(a);
    upload_layout(a);
    bind_textures(a);
    a->has_hover = false;
}

/* ---------------------------------------------------------------- filter and results */

static bool is_word(int c)
{
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_';
}

static void free_results(App *a)
{
    free(a->res_file);
    free(a->res_line);
    free(a->res_col);
    a->res_file = a->res_col = NULL;
    a->res_line = NULL;
    a->n_res = 0;
    for (int i = 0; i < a->n_panel_rows; i++)
        free(a->panel_rows[i].text);
    free(a->panel_rows);
    a->panel_rows = NULL;
    a->n_panel_rows = 0;
}

static void add_panel_row(App *a, const char *text, const float *color, int ri)
{
    a->panel_rows = realloc(a->panel_rows, (a->n_panel_rows + 1) * sizeof(PanelRow));
    PanelRow *r = &a->panel_rows[a->n_panel_rows++];
    r->text = strdup(text);
    r->color = color;
    r->ri = ri;
}

static int panel_chars(App *a)
{
    return (int)((180 - 10) / (11 * a->ui_aspect));
}

/* The panel: the hits by file, each with its line number and the line's
   text windowed so the hit shows. */
static void build_panel_rows(App *a)
{
    int width = panel_chars(a);
    char buf[2048], num[32];
    snprintf(buf, sizeof buf, "Hits (%d)", a->n_res);
    add_panel_row(a, buf, YELLOW, -1);
    int last_file = -1;
    for (int i = 0; i < a->n_res; i++) {
        int f = a->res_file[i];
        if (f != last_file) {
            char path[1024];
            str_at(a->paths, f, path, sizeof path);
            int n = (int)strlen(path);
            if (n <= width)
                add_panel_row(a, path, UI_TEXT, -1);
            else {
                snprintf(buf, sizeof buf, ELLIPSIS "%s", path + n - width + 1);
                add_panel_row(a, buf, UI_TEXT, -1);
            }
            last_file = f;
        }
        int64_t line = a->res_line[i];
        const char *raw = (const char *)a->chars + a->line_off[line];
        int n = (int)(a->line_off[line + 1] - a->line_off[line]);
        int lead = 0;
        while (lead < n && (raw[lead] == ' ' || raw[lead] == '\t' || raw[lead] == '\r' || raw[lead] == '\f' || raw[lead] == '\v'))
            lead++;
        const char *body = raw + lead;
        int nb = n - lead, c = a->res_col[i] - lead;
        snprintf(num, sizeof num, "%5lld ", (long long)(line - a->file_line0[f] + 1));
        int k = snprintf(buf, sizeof buf, "%s", num);
        if (c + a->res_len > width - 6) {        /* window the text so the hit shows */
            int s = c - 8 > 0 ? c - 8 : 0;
            k += snprintf(buf + k, sizeof buf - k, ELLIPSIS);
            body += s;
            nb -= s;
        }
        if (nb > (int)sizeof buf - k - 1)
            nb = (int)sizeof buf - k - 1;
        memcpy(buf + k, body, nb > 0 ? nb : 0);
        buf[k + (nb > 0 ? nb : 0)] = 0;
        add_panel_row(a, buf, UI_DIM, i);
    }
}

static void update_file_flags(App *a)
{
    memset(a->dimmed, 0, a->n_files);
    a->current_file = -1;
    if (a->has_results && strlen(a->filter_text) >= 2) {
        for (int f = 0; f < a->n_files; f++)
            a->dimmed[f] = !a->hits_by_file[f];
        if (a->result_i >= 0)
            a->current_file = a->res_file[a->result_i];
    }
}

/* Whole-word search over the corpus bytes, as run_search: a word boundary
   is a change between [A-Za-z0-9_] and anything else, line ends included,
   and a mention inside a comment or a string (kinds 4 and 5) is no hit.
   Lines are in file order, so the hits come sorted by file, line, column. */
static void run_search(App *a, const char *word)
{
    int wl = (int)strlen(word);
    int cap = 1024;
    a->res_file = malloc(cap * sizeof(int));
    a->res_line = malloc(cap * sizeof(int64_t));
    a->res_col = malloc(cap * sizeof(int));
    a->n_res = 0;
    a->res_len = wl;
    bool w0 = is_word((uint8_t)word[0]), w1 = is_word((uint8_t)word[wl - 1]);
    for (int64_t l = 0; l < a->n_lines; l++) {
        const uint8_t *s = a->chars + a->line_off[l];
        int n = (int)(a->line_off[l + 1] - a->line_off[l]);
        for (int i = 0; i + wl <= n;) {
            const uint8_t *p = memchr(s + i, (uint8_t)word[0], n - wl + 1 - i);
            if (!p)
                break;
            i = (int)(p - s);
            bool before = i > 0 && is_word(s[i - 1]), after = i + wl < n && is_word(s[i + wl]);
            if (memcmp(s + i, word, wl) || before == w0 || after == w1) {
                i++;
                continue;
            }
            uint8_t kind = a->kinds[a->line_off[l] + i];
            if (kind != 4 && kind != 5) {
                if (a->n_res == cap) {
                    cap *= 2;
                    a->res_file = realloc(a->res_file, cap * sizeof(int));
                    a->res_line = realloc(a->res_line, cap * sizeof(int64_t));
                    a->res_col = realloc(a->res_col, cap * sizeof(int));
                }
                a->res_file[a->n_res] = a->line_file[l];
                a->res_line[a->n_res] = l;
                a->res_col[a->n_res] = i;
                a->n_res++;
            }
            i += wl;                             /* matches do not overlap */
        }
    }
}

static void set_filter(App *a, const char *text)
{
    bool fitted = is_fitted(a);   /* before the panel changes the map's width */
    if (text != a->filter_text)
        snprintf(a->filter_text, sizeof a->filter_text, "%s", text);
    free_results(a);
    a->has_results = false;
    a->result_i = -1;
    a->panel_scroll = 0;
    memset(a->hits_by_file, 0, a->n_files);
    memset(a->hit_count, 0, a->n_files * sizeof(int64_t));
    if (strlen(a->filter_text) >= 2) {
        run_search(a, a->filter_text);
        a->has_results = true;
        if (a->n_res == 0)
            add_panel_row(a, "no hits", UI_DIM, -1);
        else {
            for (int i = 0; i < a->n_res; i++) {
                a->hit_count[a->res_file[i]]++;
                a->hits_by_file[a->res_file[i]] = 1;
            }
            build_panel_rows(a);
        }
    }
    update_file_flags(a);
    if (fitted)           /* a fitted map stays fitted to the map viewport */
        fit(a);
}

/* world rect around a line (about `lines` by `cols` cells), so the
   destination of a fly-to is at the text LOD */
static void line_rect(App *a, int f, int64_t line, double col, double lines, double cols, double *out)
{
    double p = a->file_pitch[f];
    int64_t rc;
    int row = row_of(a, line, (int64_t)col, &rc);
    double x = a->row_pos[2 * row], y = a->row_pos[2 * row + 1];
    double xc = x + col_offset(a, row, rc) * p, yc = y + p / 2;
    double hw = cols / 2 * p * a->A, hh = lines / 2 * p;
    /* keep the centre on the file, so the file under the view centre is this one */
    const double *r = a->file_rect + 4 * f;
    xc = clampd(xc, r[0] + 1e-3, fmax(r[2] - 1e-3, r[0] + 1e-3));
    yc = clampd(yc, r[1] + 1e-3, fmax(r[3] - 1e-3, r[1] + 1e-3));
    out[0] = xc - hw;
    out[1] = yc - hh;
    out[2] = xc + hw;
    out[3] = yc + hh;
}

static int panel_visible_rows(App *a)
{
    if (!a->has_panel_rect)
        return 1;
    int n = (int)((a->panel_rect[3] - a->panel_rect[1] - 8 * a->px) / (13 * a->px));
    return n > 1 ? n : 1;
}

static void goto_result(App *a, int i, bool complete)
{
    a->result_i = i;
    update_file_flags(a);
    int f = a->res_file[i];
    double rect[4];
    line_rect(a, f, a->res_line[i], a->res_col[i] + a->res_len / 2.0, 40, 100, rect);
    fly_to(a, rect, 0.06, complete);
    for (int k = 0; k < a->n_panel_rows; k++)         /* keep the current row in view */
        if (a->panel_rows[k].ri == i) {
            int vis = panel_visible_rows(a);
            if (k < a->panel_scroll || k >= a->panel_scroll + vis)
                a->panel_scroll = k - vis / 2 > 0 ? k - vis / 2 : 0;
            break;
        }
}

static void step(App *a, int d)
{
    if (!a->has_results || a->n_res == 0)
        return;
    int n = a->n_res;
    goto_result(a, a->result_i >= 0 ? ((a->result_i + d) % n + n) % n : (d > 0 ? 0 : n - 1), false);
}

/* --goto path[:line]; the path may hold colons (a book's `Book One: 1805/...`) */
static void goto_spec(App *a, const char *spec)
{
    char path[1024];
    snprintf(path, sizeof path, "%s", spec);
    long line = -1;
    char *colon = strrchr(path, ':');
    if (colon && colon[1] && strspn(colon + 1, "0123456789") == strlen(colon + 1)) {
        line = atol(colon + 1);
        *colon = 0;
    }
    int best = -1, best_len = 0;
    size_t pl = strlen(path);
    for (int i = 0; i < a->n_files; i++) {
        char p[1024];
        str_at(a->paths, i, p, sizeof p);
        size_t n = strlen(p);
        bool match = !strcmp(p, path) || (n > pl && p[n - pl - 1] == '/' && !strcmp(p + n - pl, path));
        if (match && (best < 0 || (int)n < best_len)) {
            best = i;
            best_len = (int)n;
        }
    }
    if (best < 0)
        die("error: --goto: no file matches '%s'", path);
    int f = best;
    int64_t l0 = a->file_line0[f], l1 = a->file_line0[f + 1];
    double rect[4];
    if (line >= 0 && l1 > l0) {
        int64_t j = (int64_t)clampd(line - 1, 0, (double)(l1 - l0 - 1));
        int col = a->line_len[l0 + j] / 2 < 30 ? a->line_len[l0 + j] / 2 : 30;
        line_rect(a, f, l0 + j, col, 40, 100, rect);
    } else
        memcpy(rect, a->file_rect + 4 * f, sizeof rect);
    fly_to(a, rect, 0.06, true);
}

/* ---------------------------------------------------------------- hover and crumb */

static int hover_at(App *a, double sx, double sy, int *out)
{
    if (!in_map(a, sx, sy))
        return 0;
    double wx, wy;
    screen_to_world(a, sx, sy, &wx, &wy);
    int f = file_at(a, wx, wy);
    if (a->proj3d)        /* re-pick on that file's top, then its neighbour's */
        for (int it = 0; it < 2; it++) {
            double z = f >= 0 ? a->file_top[f] : 0.0;
            screen_to_world_z(a, sx, sy, z, &wx, &wy);
            int f2 = file_at(a, wx, wy);
            if (f2 == f)
                break;
            f = f2;
        }
    if (f < 0)
        return 0;
    const double *t = a->file_text + 4 * f;       /* the text block: a book page's inner rectangle */
    double x0 = t[0], y0 = t[1], p = a->file_pitch[f], colw = a->file_colw[f];
    int k = a->file_cols[f], rows = (int)a->file_rows[f];
    int n = (int)(a->file_row0[f + 1] - a->file_row0[f]);
    int c = (int)clampd(floor((wx - x0) / colw), 0, k - 1 > 0 ? k - 1 : 0);
    int r = (int)floor((wy - y0 - p) / p);
    int j = (0 <= r && r < rows) ? c * rows + r : -1;
    if (j >= n)
        j = -1;
    int col = (int)floor((wx - x0 - c * colw) / (p * a->A));
    if (j >= 0) {         /* visual row -> logical line and column */
        int row = a->file_row0[f] + j;
        uint32_t line = a->row_line[row];
        int col0 = a->row_col0[row];
        double xr = (wx - a->row_pos[2 * row]) / p;      /* from the row's left, in line heights */
        if (a->prop) {
            int64_t start = (int64_t)a->line_off[line] + col0;
            int nr = a->row_len[row], m = 0;
            while (m < nr && a->char_x[start + m] <= xr)
                m++;
            col = col0 + (m - 1 > 0 ? m - 1 : 0);
        } else
            col = col0 + (xr / a->A > 0 ? (int)floor(xr / a->A) : 0);
        j = (int)(line - a->file_line0[f]);
    }
    out[0] = f;
    out[1] = j;
    out[2] = col;
    return 1;
}

static int enclosing_item(App *a, int f, int j)
{
    int best = -1;
    uint32_t best_span = 0;
    for (int k = a->items_by_file_off[f]; k < a->items_by_file_off[f + 1]; k++) {
        int it = a->items_by_file[k];
        uint32_t s = a->item_start[it], e = a->item_end[it];
        if (s <= (uint32_t)j && (uint32_t)j < e && (best < 0 || e - s < best_span)) {
            best = it;
            best_span = e - s;
        }
    }
    return best;
}

static bool hover_label(App *a, char *out, size_t cap)
{
    if (!a->has_hover)
        return false;
    int f = a->hover[0], j = a->hover[1];
    char path[1024];
    int k = snprintf(out, cap, "%s", str_at(a->paths, f, path, sizeof path));
    if (a->lod[f] >= 2 && j >= 0) {
        k += snprintf(out + k, cap - k, ":%d", j + 1);
        int it = enclosing_item(a, f, j);
        if (it >= 0) {
            char kw[64], nm[512];
            k += snprintf(out + k, cap - k, " %s %s", str_at(a->item_kws, it, kw, sizeof kw),
                          str_at(a->item_names, it, nm, sizeof nm));
        }
    }
    if (a->lod[f] == 3 && a->has_results)
        snprintf(out + k, cap - k, DOT "%lld hits", (long long)a->hit_count[f]);
    return true;
}

static int deepest_dir_at(App *a, double wx, double wy, int min_depth)
{
    int best = -1;
    for (int d = 0; d < a->n_dirs; d++) {
        const double *r = a->dir_rect + 4 * d;
        if (r[0] <= wx && wx < r[2] && r[1] <= wy && wy < r[3] && a->dir_depth[d] >= min_depth
            && (best < 0 || a->dir_depth[d] > a->dir_depth[best]))
            best = d;
    }
    return best;
}

/* where the view centre is: the corpus, then the path of the file under
   the centre, or of the deepest directory there */
static void update_crumb(App *a)
{
    int k = snprintf(a->crumb, sizeof a->crumb, "at %s", a->name);
    int f = file_at(a, a->cx, a->cy);
    if (f >= 0) {
        char path[1024];
        str_at(a->paths, f, path, sizeof path);
        for (char *part = strtok(path, "/"); part; part = strtok(NULL, "/"))
            k += snprintf(a->crumb + k, sizeof a->crumb - k, CRUMB "%s", part);
        return;
    }
    int d = deepest_dir_at(a, a->cx, a->cy, 0);
    int chain[64], n = 0;
    while (d > 0 && n < 64) {
        chain[n++] = d;
        d = a->dir_parent[d];
    }
    for (int i = n - 1; i >= 0; i--) {
        char label[512];
        str_at(a->dir_labels, chain[i], label, sizeof label);
        size_t len = strlen(label);
        if (len && label[len - 1] == '/')
            label[len - 1] = 0;
        k += snprintf(a->crumb + k, sizeof a->crumb - k, CRUMB "%s", label);
    }
}

/* ---------------------------------------------------------------- input */

static bool over_ui(App *a, double sx, double sy)
{
    return !in_map(a, sx, sy) || (a->has_panel_rect && in_rect(sx, sy, a->panel_rect))
           || (a->has_filter_rect && in_rect(sx, sy, a->filter_rect));
}

/* the file under the cursor, else the deepest directory there: its world rectangle */
static bool rect_at(App *a, double sx, double sy, double *rect)
{
    int f = a->has_hover ? a->hover[0] : -1;
    double wx, wy;
    screen_to_world(a, sx, sy, &wx, &wy);
    if (f < 0)
        f = file_at(a, wx, wy);
    if (f >= 0) {
        memcpy(rect, a->file_rect + 4 * f, 4 * sizeof(double));
        return true;
    }
    int d = deepest_dir_at(a, wx, wy, 1);
    if (d < 0)
        return false;
    memcpy(rect, a->dir_rect + 4 * d, 4 * sizeof(double));
    return true;
}

/* Fly to the extents of the file or directory under the cursor; a second
   double click while still at those extents flies back to the view before. */
static void double_click(App *a, double sx, double sy)
{
    double rect[4];
    if (!rect_at(a, sx, sy, rect))
        return;
    if (a->zoomed && !memcmp(a->zoomed_rect, rect, sizeof rect)) {
        double c1[2], z1, w1, pitch;
        fly_target(a, rect, 0.06, c1, &z1, &w1, &pitch);
        bool at_extents = fabs(a->cx - c1[0]) < 1e-6 * a->W && fabs(a->cy - c1[1]) < 1e-6 * a->H
                          && fabs(a->zoom - z1) < 1e-6 * z1;
        if (at_extents) {
            double bx = a->zoomed_back[0], by = a->zoomed_back[1], bz = a->zoomed_back[2];
            double w = map_w(a) / bz, h = a->map_h / bz;
            double back[4] = {bx - w / 2, by - h / 2, bx + w / 2, by + h / 2};
            a->zoomed = false;
            fly_to(a, back, 0.0, false);
            return;
        }
    }
    a->zoomed = true;
    memcpy(a->zoomed_rect, rect, sizeof rect);
    a->zoomed_back[0] = a->cx;
    a->zoomed_back[1] = a->cy;
    a->zoomed_back[2] = a->zoom;
    fly_to(a, rect, 0.06, false);
}

static int panel_row_at(App *a, double sy)
{
    if (!a->has_panel_rect)
        return -1;
    int k = (int)floor((sy - a->panel_rect[1] - 4 * a->px) / (13 * a->px)) + (int)a->panel_scroll;
    return 0 <= k && k < a->n_panel_rows ? k : -1;
}

static void click(App *a, double sx, double sy)
{
    if (a->has_filter_rect && in_rect(sx, sy, a->filter_rect)) {
        a->filter_focus = true;
        return;
    }
    if (a->has_panel_rect && in_rect(sx, sy, a->panel_rect)) {
        int row = panel_row_at(a, sy);
        if (row >= 0 && a->panel_rows[row].ri >= 0)
            goto_result(a, a->panel_rows[row].ri, false);
        return;
    }
    a->filter_focus = false;
}

static void on_mouse_button(App *a, int button, bool down, double x, double y)
{
    double sx = x * a->px, sy = y * a->px;
    if (down) {
        a->fly.on = false;
        a->zoom_vel = 0.0;
        double now = now_s();
        bool had = a->has_last_press;
        a->has_last_press = false;
        if (button == SDL_BUTTON_LEFT && had && now - a->last_press[0] < DBL_CLICK_S
            && fabs(x - a->last_press[1]) < 4 && fabs(y - a->last_press[2]) < 4 && !over_ui(a, sx, sy)) {
            double_click(a, sx, sy);      /* no drag, no click on the release */
            a->dragging = a->pressed = false;
            return;
        }
        if (button == SDL_BUTTON_LEFT) {
            a->has_last_press = true;
            a->last_press[0] = now;
            a->last_press[1] = x;
            a->last_press[2] = y;
        }
        a->tilting = (SDL_GetModState() & KMOD_ALT) != 0;
        a->dragging = a->pressed = true;
        a->drag[0] = a->press[0] = x;
        a->drag[1] = a->press[1] = y;
    } else {
        if (a->pressed) {
            bool moved = fabs(x - a->press[0]) >= 3 || fabs(y - a->press[1]) >= 3;
            if (!moved && button == SDL_BUTTON_LEFT)
                click(a, sx, sy);
        }
        a->dragging = a->pressed = false;
    }
}

static void on_cursor(App *a, double x, double y)
{
    a->has_cursor = true;
    a->cursor_pt[0] = x;
    a->cursor_pt[1] = y;
    a->cursor[0] = x * a->px;
    a->cursor[1] = y * a->px;
    if (!a->dragging)
        return;
    double dx = (x - a->drag[0]) * a->px, dy = (y - a->drag[1]) * a->px;
    if (a->tilting) {     /* Alt-drag: tilt and turn the 3D camera */
        if (!a->proj3d) {
            set_proj(a, true);
            a->tilt = 0.0;
        }
        a->tilt = clampd(a->tilt + dy * 0.25 / a->px, 0.0, TILT_MAX);
        a->yaw = fmod(a->yaw + dx * 0.25 / a->px + 360.0, 360.0);
        a->drag[0] = x;
        a->drag[1] = y;
        return;
    }
    if (a->proj3d) {      /* pan along the plane under the cursor */
        camera_matrix(a);
        double bx, by, ax, ay;
        screen_to_world(a, a->drag[0] * a->px, a->drag[1] * a->px, &bx, &by);
        screen_to_world(a, x * a->px, y * a->px, &ax, &ay);
        a->cx -= ax - bx;
        a->cy -= ay - by;
        a->drag[0] = x;
        a->drag[1] = y;
        return;
    }
    a->drag[0] = x;
    a->drag[1] = y;
    a->cx -= dx / a->zoom;
    a->cy -= dy / a->zoom;
}

static void on_scroll(App *a, double dy)
{
    if (a->has_cursor && a->has_panel_rect && in_rect(a->cursor[0], a->cursor[1], a->panel_rect)) {
        a->panel_scroll = fmax(0.0, a->panel_scroll - dy * 3);   /* rows, fractional */
        return;
    }
    if (!a->has_cursor)
        return;
    a->fly.on = false;
    a->zoom_vel = clampd(a->zoom_vel + dy * log(1.2) / ZOOM_TAU, -ZOOM_VMAX, ZOOM_VMAX);
    a->anchor[0] = a->cursor[0];
    a->anchor[1] = a->cursor[1];
    a->has_anchor = true;
}

static void on_key(App *a, SDL_Keycode key)
{
    if (key == SDLK_ESCAPE) {
        if (a->filter_text[0] || a->filter_focus) {
            set_filter(a, "");
            a->filter_focus = false;
        } else if (a->result_i >= 0) {
            a->result_i = -1;
            update_file_flags(a);
        }
        return;
    }
    if (key == SDLK_RETURN || key == SDLK_KP_ENTER || key == SDLK_DOWN)
        step(a, 1);
    else if (key == SDLK_UP)
        step(a, -1);
    else if (key == SDLK_BACKSPACE && a->filter_focus) {
        size_t n = strlen(a->filter_text);
        if (n) {
            char text[256];
            snprintf(text, sizeof text, "%.*s", (int)n - 1, a->filter_text);
            set_filter(a, text);
        }
    } else if (a->filter_focus)
        return;           /* typed characters arrive as text input */
    else if (key == SDLK_RIGHTBRACKET)
        step(a, 1);
    else if (key == SDLK_LEFTBRACKET)
        step(a, -1);
    else if (key == SDLK_c)
        a->tint_on = a->file_tint && !a->tint_on;
    else if (key == SDLK_h)
        set_heights(a, !a->heights);
    else if (key == SDLK_r)
        fit(a);
    else if (key == SDLK_3)
        set_proj(a, !a->proj3d);
    else if (key == SDLK_SLASH) {
        a->filter_focus = true;
        a->swallow_char = true;
    }
#ifndef __EMSCRIPTEN__    /* a web page has nothing to quit */
    else if (key == SDLK_q)
        a->running = false;
#endif
}

/* Windows-1252 for the Unicode code points of its 0x80..0x9F row */
static const uint16_t CP1252_HI[32] = {
    0x20AC, 0, 0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021, 0x02C6, 0x2030, 0x0160, 0x2039, 0x0152, 0, 0x017D, 0,
    0, 0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014, 0x02DC, 0x2122, 0x0161, 0x203A, 0x0153, 0, 0x017E, 0x0178};

static void on_text(App *a, const char *utf8)
{
    if (a->swallow_char) {
        a->swallow_char = false;
        return;
    }
    if (!a->filter_focus)
        return;
    char text[256];
    size_t n = strlen(a->filter_text);
    memcpy(text, a->filter_text, n);
    for (const uint8_t *s = (const uint8_t *)utf8; *s && n < sizeof text - 1;) {
        uint32_t cp;
        int len = *s < 0x80 ? 1 : (*s >> 5) == 6 ? 2 : (*s >> 4) == 14 ? 3 : 4;
        cp = len == 1 ? *s : len == 2 ? *s & 0x1F : len == 3 ? *s & 0x0F : *s & 0x07;
        for (int i = 1; i < len && s[i]; i++)
            cp = (cp << 6) | (s[i] & 0x3F);
        s += len;
        int byte = -1;
        if ((cp >= 32 && cp < 127) || (cp >= 0xA0 && cp <= 0xFF))
            byte = (int)cp;
        for (int i = 0; i < 32 && byte < 0; i++)
            if (CP1252_HI[i] && CP1252_HI[i] == cp)
                byte = 0x80 + i;
        if (byte >= 0)
            text[n++] = (char)byte;
    }
    text[n] = 0;
    if (strcmp(text, a->filter_text))
        set_filter(a, text);
}

/* ---------------------------------------------------------------- frame */

/* visible and foreshortening scale per rectangle at height z through the
   3D camera: visible when its projected corners overlap the viewport (a
   corner behind the camera counts as visible), scale = focus_w / w */
static void project_rect(App *a, const double *r, double z, uint8_t *visible, double *scale)
{
    double xs[4] = {r[0], r[2], r[0], r[2]}, ys[4] = {r[1], r[1], r[3], r[3]};
    double nxmax = -INFINITY, nxmin = INFINITY, nymax = -INFINITY, nymin = INFINITY, wsum = 0;
    int front = 0;
    for (int c = 0; c < 4; c++) {
        double v[4] = {xs[c], ys[c], z, 1.0}, q[4];
        mat4_xform(a->M, v, q);
        wsum += q[3];
        if (q[3] > 1e-6) {
            front++;
            nxmax = fmax(nxmax, q[0] / q[3]);
            nxmin = fmin(nxmin, q[0] / q[3]);
            nymax = fmax(nymax, q[1] / q[3]);
            nymin = fmin(nymin, q[1] / q[3]);
        }
    }
    bool inside = nxmax > -1 && nxmin < 1 && nymax > -1 && nymin < 1;
    if (visible)
        *visible = front == 4 ? inside : front > 0;
    double wc = wsum / 4;
    *scale = a->focus_w / (wc > 1e-6 ? wc : 1e-6);
}

static double focus_target(App *a)
{
    int f = file_at(a, a->cx, a->cy);
    if (f < 0)            /* over a directory band: hold the height */
        return a->z_focus;
    double w = clampd(a->file_pitch[f] * a->zoom / 2.0, 0.0, 1.0);
    return a->file_top[f] * w;
}

static void update_per_file(App *a)
{
    if (a->proj3d)
        a->z_focus += (focus_target(a) - a->z_focus) * (a->frames > 0 ? 1.0 : 0.2);
    else
        a->z_focus = 0.0;
    camera_matrix(a);
    double v[4];
    view_rect(a, v);
    for (int f = 0; f < a->n_files; f++) {
        const double *r = a->file_rect + 4 * f;
        if (a->proj3d)
            project_rect(a, r, a->file_top[f], &a->visible[f], &a->file_scale[f]);
        else {
            a->visible[f] = r[2] > v[0] && r[0] < v[2] && r[3] > v[1] && r[1] < v[3];
            a->file_scale[f] = 1.0;
        }
        double ppl = a->file_pitch[f] * a->zoom * a->file_scale[f];
        a->ppl[f] = ppl;
        a->lod[f] = (ppl >= 1) + (ppl >= 3) + (ppl >= 6);
    }
    for (int d = 0; d < a->n_dirs; d++) {
        if (a->proj3d)
            project_rect(a, a->dir_rect + 4 * d, a->dir_z0[d] + a->dir_h[d], NULL, &a->dir_scale[d]);
        else
            a->dir_scale[d] = 1.0;
    }
    a->has_hover = a->has_cursor && !a->dragging && !over_ui(a, a->cursor[0], a->cursor[1])
                   && hover_at(a, a->cursor[0], a->cursor[1], a->hover);
    bool filtering = a->has_results && strlen(a->filter_text) >= 2;
    for (int f = 0; f < a->n_files; f++) {
        uint8_t flags = 0;
        if (a->has_hover && a->hover[0] == f)
            flags |= 1;
        if (filtering) {
            if (a->hits_by_file[f])
                flags |= 2;
            if (a->current_file == f)
                flags |= 4;
            if (a->dimmed[f])
                flags |= 8;
        }
        a->file_u[4 * f] = a->lod[f];
        a->file_u[4 * f + 1] = flags;
    }
    glActiveTexture(GL_TEXTURE6);
    glBindTexture(GL_TEXTURE_2D, a->tex_file_u);
    glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, TEX_W, a->file_u_rows, GL_RGBA_INTEGER, GL_UNSIGNED_BYTE, a->file_u);
    update_crumb(a);
}

static void set_camera_uniforms(App *a, int p, bool world)
{
    glUseProgram(a->prog[p]);
    GLint loc = U(a, p, "uMVP");
    if (world) {
        float m[16], v4[4];
        double v[4];
        for (int i = 0; i < 16; i++)
            m[i] = (float)a->M[i];
        view_rect(a, v);
        for (int i = 0; i < 4; i++)
            v4[i] = (float)v[i];
        glUniformMatrix4fv(loc, 1, GL_TRUE, m);
        glUniform4fv(U(a, p, "uView"), 1, v4);
        glUniform1f(U(a, p, "uScale"), (float)a->zoom);
        glUniform1f(U(a, p, "uFocusW"), (float)a->focus_w);
        glUniform1f(U(a, p, "uZScale"), a->proj3d ? 1.0f : 0.0f);
        glUniform1i(U(a, p, "uWorld"), 1);
        glUniform2f(U(a, p, "uViewport"), (float)map_w(a), (float)a->map_h);
        glUniform1f(U(a, p, "uBandPx"), (float)(BAND_PT * a->px));
    } else {
        static const float I[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
        glUniformMatrix4fv(loc, 1, GL_TRUE, I);
        glUniform1i(U(a, p, "uWorld"), 0);
        glUniform2f(U(a, p, "uViewport"), (float)a->fb_w, (float)a->fb_h);
    }
}

/* One instanced draw per run of consecutive visible files (at LOD
   min_lod or above): files are laid out depth-first, so a visible region
   is a few contiguous ranges of rows (lines) or of characters (glyphs). */
static void draw_runs(App *a, int p, int min_lod, bool glyphs)
{
    glUseProgram(a->prog[p]);
    GLint loc = U(a, p, "uBase");
    glBindVertexArray(a->quad_vao);
    int f = 0, nf = a->n_files;
    while (f < nf) {
        while (f < nf && !(a->visible[f] && a->lod[f] >= min_lod))
            f++;
        int f0 = f;
        while (f < nf && a->visible[f] && a->lod[f] >= min_lod)
            f++;
        if (f0 == f)
            break;
        int64_t base, end;
        if (glyphs) {     /* the first character of each file */
            base = (int64_t)a->line_off[a->file_line0[f0]];
            end = (int64_t)a->line_off[a->file_line0[f]];
        } else {
            base = a->file_row0[f0];
            end = a->file_row0[f];
        }
        if (end > base) {
            glUniform1i(loc, (GLint)base);
            glDrawArraysInstanced(GL_TRIANGLES, 0, 6, (GLsizei)(end - base));
        }
    }
}

static void draw_instanced(App *a, int p, int n)
{
    glUseProgram(a->prog[p]);
    glBindVertexArray(a->quad_vao);
    glDrawArraysInstanced(GL_TRIANGLES, 0, 6, n);
}

/* inst: 11 floats each, x0 y0 x1 y1 r g b a border_px fill_alpha z */
static void draw_rects(App *a, FBuf *inst, bool world)
{
    if (!inst->n)
        return;
    set_camera_uniforms(a, P_RECT, world);
    glBindVertexArray(a->rect_vao);
    glBindBuffer(GL_ARRAY_BUFFER, a->rect_vbo);
    glBufferData(GL_ARRAY_BUFFER, inst->n * sizeof(float), inst->p, GL_STREAM_DRAW);
    glDrawArraysInstanced(GL_TRIANGLES, 0, 6, inst->n / 11);
}

/* inst: 9 floats each, x y w h cell r g b a in device pixels */
static void draw_text(App *a, FBuf *inst)
{
    if (!inst->n)
        return;
    glUseProgram(a->prog[P_TEXT]);
    glUniform2f(U(a, P_TEXT, "uViewport"), (float)a->fb_w, (float)a->fb_h);
    glBindVertexArray(a->text_vao);
    glBindBuffer(GL_ARRAY_BUFFER, a->text_vbo);
    glBufferData(GL_ARRAY_BUFFER, inst->n * sizeof(float), inst->p, GL_STREAM_DRAW);
    glDrawArraysInstanced(GL_TRIANGLES, 0, 6, inst->n / 9);
}

static const float *item_color(App *a, int kind)
{
    return a->item_colors[(kind < 1 || kind > 5) ? 4 : kind - 1];
}

/* The kind bands: the item rectangles of visible files as filled bands in
   the item kind colour, under the bars, at 18 percent up to 3 px per line
   and fading to nothing by 6, where the outlines have taken over. */
static void band_instances(App *a, FBuf *out)
{
    out->n = 0;
    for (int i = 0; i < a->n_item_rects; i++) {
        int f = a->item_rect_file[i];
        if (!a->visible[f] || a->lod[f] > 2)
            continue;
        float *t = fbuf_add(out, 11);
        for (int k = 0; k < 4; k++)
            t[k] = a->item_rect[4 * i + k];
        memcpy(t + 4, item_color(a, a->item_kind[a->item_rect_item[i]]), 12);
        t[7] = 1.0f;
        t[9] = (float)(0.18 * clampd((6.0 - a->ppl[f]) / 3.0, 0.0, 1.0));
        t[10] = (float)(a->file_top[f] + 0.02);
    }
}

static void item_instances(App *a, FBuf *out)
{
    out->n = 0;
    for (int i = 0; i < a->n_item_rects; i++) {
        int f = a->item_rect_file[i];
        if (!a->visible[f] || a->lod[f] < 2)
            continue;
        float *t = fbuf_add(out, 11);
        for (int k = 0; k < 4; k++)
            t[k] = a->item_rect[4 * i + k];
        memcpy(t + 4, item_color(a, a->item_kind[a->item_rect_item[i]]), 12);
        t[7] = 0.6f;
        t[8] = 1.5f;
        t[10] = (float)(a->file_top[f] + 0.06);
    }
}

static void hit_instances(App *a, FBuf *out)
{
    out->n = 0;
    if (!a->has_results)
        return;
    for (int i = 0; i < a->n_res; i++) {
        int f = a->res_file[i];
        if (!a->visible[f] || a->lod[f] < 2)
            continue;
        double p = a->file_pitch[f];
        int64_t rc;
        int row = row_of(a, a->res_line[i], a->res_col[i], &rc);
        int64_t rlen = a->row_len[row];
        double c0 = col_offset(a, row, rc < rlen ? rc : rlen) * p;
        double c1 = col_offset(a, row, rc + a->res_len < rlen ? rc + a->res_len : rlen) * p;
        double x = a->row_pos[2 * row], y = a->row_pos[2 * row + 1];
        float *t = fbuf_add(out, 11);
        t[0] = (float)(x + c0);
        t[1] = (float)y;
        t[2] = (float)(x + c1);
        t[3] = (float)(y + p);
        memcpy(t + 4, YELLOW, 12);
        t[7] = 1.0f;
        t[8] = 2.0f;
        t[9] = i == a->result_i ? 0.25f : 0.0f;
        t[10] = (float)(a->file_top[f] + 0.06);
    }
}

/* ---- UI */

static void ui_box(App *a, double x0, double y0, double x1, double y1, const float *color, double alpha, double border)
{
    float *t = fbuf_add(&a->ui_boxes, 11);
    t[0] = (float)x0;
    t[1] = (float)y0;
    t[2] = (float)x1;
    t[3] = (float)y1;
    memcpy(t + 4, color, 12);
    t[7] = 1.0f;
    t[8] = (float)border;
    t[9] = border > 0 ? 0.0f : (float)alpha;
}

/* per-character instances for one Windows-1252 string, cut to max_chars
   with an ellipsis (max_chars < 0: no cut); returns its width */
static double ui_label(App *a, double x, double y, const char *text, double size, const float *color,
                       double alpha, int max_chars)
{
    int n = (int)strlen(text);
    bool cut = max_chars >= 0 && n > max_chars;
    if (cut)
        n = max_chars > 0 ? max_chars : 0;
    double cw = size * a->ui_aspect;
    for (int i = 0; i < n; i++) {
        int c = (uint8_t)((cut && i == n - 1) ? 0x85 : (uint8_t)text[i]);
        float *t = fbuf_add(&a->ui_texts, 9);
        t[0] = (float)(x + i * cw);
        t[1] = (float)y;
        t[2] = (float)cw;
        t[3] = (float)size;
        t[4] = (float)(c >= 32 ? c - 32 : '?' - 32);
        memcpy(t + 5, color, 12);
        t[8] = (float)alpha;
    }
    return n * cw;
}

static void ui_flush(App *a)
{
    draw_rects(a, &a->ui_boxes, false);
    draw_text(a, &a->ui_texts);
    a->ui_boxes.n = a->ui_texts.n = 0;
}

/* the status: the corpus, or the search, and the tint's label or "" */
static void status_texts(App *a, char *status, size_t cap, const char **extra)
{
    char c1[32], c2[32], c3[32], c4[32];
    if (a->has_results && strlen(a->filter_text) >= 2) {
        int files = 0;
        for (int f = 0; f < a->n_files; f++)
            files += a->hits_by_file[f];
        snprintf(status, cap, "%s hits" DOT "%s files" DOT "%s/%s", commas(a->n_res, c1), commas(files, c2),
                 commas(a->result_i + 1, c3), commas(a->n_res, c4));
    } else
        snprintf(status, cap, "%s files" DOT "%s lines" DOT "%s chars", commas(a->n_files, c1),
                 commas(a->n_lines, c2), commas(a->n_chars, c3));
    *extra = a->tint_on && a->tint_label[0] ? a->tint_label : "";
}

/* split a " · " separated status into lines of at most n characters */
static int wrap_parts(const char *text, int n, char parts[][256], int max_parts)
{
    int k = 0;
    char cur[256] = "";
    const char *p = text;
    while (*p && k < max_parts - 1) {
        const char *q = strstr(p, DOT);
        int len = q ? (int)(q - p) : (int)strlen(p);
        char part[256];
        snprintf(part, sizeof part, "%.*s", len, p);
        if (!cur[0])
            snprintf(cur, sizeof cur, "%s", part);
        else if ((int)(strlen(cur) + 3 + strlen(part)) > n) {
            snprintf(parts[k++], 256, "%s", cur);
            snprintf(cur, sizeof cur, "%s", part);
        } else {
            char joined[256];
            snprintf(joined, sizeof joined, "%s" DOT "%s", cur, part);
            snprintf(cur, sizeof cur, "%s", joined);
        }
        p = q ? q + 3 : p + len;
    }
    snprintf(parts[k++], 256, "%s", cur);
    return k;
}

static void draw_ui(App *a)
{
    double s = a->px, size = 13 * s, small = 11 * s, cw = size * a->ui_aspect;
    double m = MARGIN_PT * s, pad = 5 * s, mw = map_w(a);

    /* directory labels: a tag at the top-left of the visible part of each
       directory's inner rectangle, so a directory zoomed into keeps its tag
       at the corner of the view */
    double v[4];
    view_rect(a, v);
    double zs = a->proj3d ? 1.0 : 0.0, bh = size + 2 * pad * 0.6;
    double vx0 = 2 * s, vy0 = a->map_y0 + 2 * s, vx1 = mw - 2 * s, vy1 = a->map_y0 + a->map_h - 2 * s;
    double (*placed)[4] = malloc((a->n_dirs + 1) * sizeof *placed);
    int n_placed = 0;
    for (int k = 0; k < a->n_dirs; k++) {
        int d = a->dir_order[k];
        const double *r = a->dir_rect + 4 * d;
        double side = fmin(r[2] - r[0], r[3] - r[1]) * a->zoom * a->dir_scale[d];
        if (!(r[2] > v[0] && r[0] < v[2] && r[3] > v[1] && r[1] < v[3] && a->dir_depth[d] >= 1 && side >= 48))
            continue;
        if (str_len(a->dir_tags, d) == 0)         /* hidden by a label chain */
            continue;
        char tag[512];
        str_at(a->dir_tags, d, tag, sizeof tag);
        double bw = strlen(tag) * cw + 2 * pad;
        if (bw > 0.4 * (r[2] - r[0]) * a->zoom * a->dir_scale[d])
            continue;
        double pd = a->dir_pad[d], z = (a->dir_z0[d] + a->dir_h[d]) * zs;
        double inner[4] = {r[0] + pd, r[1] + pd, r[2] - pd, r[3] - pd}, sb[4];
        if (!a->proj3d) {
            world_to_screen(a, inner[0], inner[1], 0, &sb[0], &sb[1]);
            world_to_screen(a, inner[2], inner[3], 0, &sb[2], &sb[3]);
        } else {
            double xs[4] = {inner[0], inner[2], inner[0], inner[2]}, ys[4] = {inner[1], inner[1], inner[3], inner[3]};
            bool behind = false;
            sb[0] = sb[1] = 1e300;
            sb[2] = sb[3] = -1e300;
            for (int c = 0; c < 4 && !behind; c++) {
                double q4[4] = {xs[c], ys[c], z, 1.0}, q[4];
                mat4_xform(a->M, q4, q);
                if (q[3] <= 1e-6) {
                    behind = true;
                    break;
                }
                double px = (q[0] / q[3] + 1.0) * 0.5 * mw, py = (1.0 - q[1] / q[3]) * 0.5 * a->map_h + a->map_y0;
                sb[0] = fmin(sb[0], px);
                sb[1] = fmin(sb[1], py);
                sb[2] = fmax(sb[2], px);
                sb[3] = fmax(sb[3], py);
            }
            if (behind)
                continue;
        }
        double bx0 = fmax(sb[0], vx0), by0 = fmax(sb[1], vy0), bx1 = fmin(sb[2], vx1), by1 = fmin(sb[3], vy1);
        if (bx1 - bx0 < bw || by1 - by0 < bh)
            continue;
        /* a child's corner is inset by its padding only, so its tag would
           sit on the parent's: slide it right along the top edge, past the
           tag in the way; down only if the right edge runs out */
        double sx = bx0, sy = by0;
        bool clear = false;
        for (int it = 0; it < 8; it++) {
            int hit = -1;
            for (int q = 0; q < n_placed && hit < 0; q++)
                if (sx < placed[q][2] && sx + bw > placed[q][0] && sy < placed[q][3] && sy + bh > placed[q][1])
                    hit = q;
            if (hit < 0) {
                clear = true;
                break;
            }
            sx = placed[hit][2] + 2 * s;
            if (sx + bw > bx1) {
                sx = bx0;
                sy = sy + bh + 2 * s;
            }
        }
        if (!clear || sy + bh > by1)
            continue;
        placed[n_placed][0] = sx;
        placed[n_placed][1] = sy;
        placed[n_placed][2] = sx + bw;
        placed[n_placed][3] = sy + bh;
        n_placed++;
        ui_box(a, sx, sy, sx + bw, sy + bh, UI_BOX, 0.85, 0);
        ui_label(a, sx + pad, sy + pad * 0.6, tag, size, UI_TEXT, 1.0, -1);
    }
    free(placed);
    glEnable(GL_SCISSOR_TEST);    /* labels stay inside the map viewport, under the panels */
    glScissor(0, (int)(a->fb_h - a->map_y0 - a->map_h), (int)mw, (int)a->map_h);
    ui_flush(a);
    glDisable(GL_SCISSOR_TEST);

    /* the bottom strip: the crumb trail left and, while the panel is
       closed, the status right-aligned on the same row */
    double row_y = a->fb_h - m - size - 2 * pad * 0.6;
    double bw = strlen(a->crumb) * cw + 2 * pad;
    ui_box(a, m, row_y, m + bw, a->fb_h - m, UI_BOX, 0.85, 0);
    ui_label(a, m + pad, row_y + pad * 0.6, a->crumb, size, UI_TEXT, 1.0, -1);
    char status[256];
    const char *extra;
    status_texts(a, status, sizeof status, &extra);
    if (!a->n_panel_rows) {
        double x1 = mw - m, free_w = x1 - (m + bw + m);
        const char *texts[2] = {extra, status};
        const float *colors[2] = {YELLOW, UI_TEXT};
        for (int i = 0; i < 2; i++) {
            if (!texts[i][0] || free_w < 6 * cw)
                continue;
            int n_chars = (int)strlen(texts[i]);
            int fit_n = (int)((free_w - 2 * pad) / cw);
            n_chars = n_chars < fit_n ? n_chars : fit_n;
            double tw = n_chars * cw + 2 * pad;
            ui_box(a, x1 - tw, row_y, x1, a->fb_h - m, UI_BOX, 0.85, 0);
            ui_label(a, x1 - tw + pad, row_y + pad * 0.6, texts[i], size, colors[i], 1.0, n_chars);
            x1 -= tw + 2 * s;
            free_w -= tw + 2 * s;
        }
    }

    /* the right column: status line, filter box, results panel */
    double pw = PANEL_PT * s, cx0 = a->fb_w - m - pw, cx1 = a->fb_w - m, fy0;
    if (a->n_panel_rows) {
        char joined[512], parts[16][256];
        snprintf(joined, sizeof joined, "%s%s%s", status, extra[0] ? DOT : "", extra);
        int n = wrap_parts(joined, panel_chars(a), parts, 16);
        double row_h = small + 2 * s, sy1 = m + n * row_h + 2 * pad * 0.6;
        ui_box(a, cx0, m, cx1, sy1, UI_BOX, 0.85, 0);
        for (int k = 0; k < n; k++)
            ui_label(a, cx0 + pad, m + pad * 0.6 + k * row_h, parts[k], small, UI_TEXT, 1.0, -1);
        fy0 = sy1 + m;
    } else
        fy0 = m;
    double fy1 = fy0 + size + 2 * pad * 0.6;
    a->filter_rect[0] = cx0;
    a->filter_rect[1] = fy0;
    a->filter_rect[2] = cx1;
    a->filter_rect[3] = fy1;
    a->has_filter_rect = true;
    ui_box(a, cx0, fy0, cx1, fy1, UI_BOX, 0.85, 0);
    ui_box(a, cx0, fy0, cx1, fy1, a->filter_focus ? YELLOW : UI_DIM, 1.0, 1.0 * s);
    if (a->filter_text[0] || a->filter_focus) {
        char text[300];
        snprintf(text, sizeof text, "%s%s", a->filter_text, a->filter_focus ? "_" : "");
        ui_label(a, cx0 + pad, fy0 + pad * 0.6, text, size, UI_TEXT, 1.0, (int)((pw - 2 * pad) / cw));
    } else
        ui_label(a, cx0 + pad, fy0 + pad * 0.6, "/ filter", size, UI_DIM, 1.0, -1);

    /* the top strip: the input hints, dim, top left, cut to the room before
       the filter box; only the keys this corpus answers to */
    char hints[512];
    int k = snprintf(hints, sizeof hints,
                     "wheel zoom" DOT "drag pan" DOT "double-click fly" DOT "/ filter" DOT "Enter next hit"
                     DOT "3 tilt" DOT "Alt-drag turn");
    if (!a->flat)
        k += snprintf(hints + k, sizeof hints - k, DOT "H heights");
    if (a->file_tint)
        k += snprintf(hints + k, sizeof hints - k, DOT "C %s", a->tint_label[0] ? a->tint_label : "tint");
    k += snprintf(hints + k, sizeof hints - k, DOT "R reset");
#ifndef __EMSCRIPTEN__
    snprintf(hints + k, sizeof hints - k, DOT "Q quit");
#endif
    double free_w = cx0 - m - m;
    int n_chars = (int)strlen(hints), fit_n = (int)((free_w - 2 * pad) / cw);
    n_chars = n_chars < fit_n ? n_chars : fit_n;
    if (n_chars >= 6) {
        double tw = n_chars * cw + 2 * pad;
        ui_box(a, m, m, m + tw, m + size + 2 * pad * 0.6, UI_BOX, 0.85, 0);
        ui_label(a, m + pad, m + pad * 0.6, hints, size, UI_DIM, 1.0, n_chars);
    }

    /* results panel down the rest of the column */
    if (a->n_panel_rows) {
        double py0 = fy1 + m, py1 = a->fb_h - m;
        a->panel_rect[0] = cx0;
        a->panel_rect[1] = py0;
        a->panel_rect[2] = cx1;
        a->panel_rect[3] = py1;
        a->has_panel_rect = true;
        ui_box(a, cx0, py0, cx1, py1, UI_BOX, 0.85, 0);
        double row_h = 13 * s;
        int n_vis = panel_visible_rows(a);
        a->panel_scroll = fmin(a->panel_scroll, fmax(0, a->n_panel_rows - n_vis));
        int top = (int)a->panel_scroll, max_chars = panel_chars(a);
        for (int r = top; r < a->n_panel_rows && r < top + n_vis; r++) {
            PanelRow *row = &a->panel_rows[r];
            double y = py0 + 4 * s + (r - top) * row_h;
            if (row->ri >= 0 && row->ri == a->result_i)
                ui_box(a, cx0 + 2 * s, y, cx1 - 2 * s, y + row_h, YELLOW, 0.25, 0);
            ui_label(a, cx0 + pad, y + (row_h - small) / 2, row->text, small, row->color, 1.0, max_chars);
        }
    } else
        a->has_panel_rect = false;

    /* hover label next to the cursor */
    char label[2048];
    if (hover_label(a, label, sizeof label) && a->has_cursor) {
        double lw = strlen(label) * cw + 2 * pad, lh = size + 2 * pad * 0.6;
        double hx = a->cursor[0] + 16 * s, hy = a->cursor[1] + 16 * s;
        if (hx + lw > mw - m)
            hx = fmax(m, a->cursor[0] - 16 * s - lw);
        if (hy + lh > a->fb_h - m)
            hy = a->cursor[1] - 16 * s - lh;
        ui_box(a, hx, hy, hx + lw, hy + lh, UI_BOX, 0.85, 0);
        ui_label(a, hx + pad, hy + pad * 0.6, label, size, UI_TEXT, 1.0, -1);
    }
    ui_flush(a);
}

static void render(App *a)
{
    update_sizes(a);
    glViewport(0, 0, a->fb_w, a->fb_h);
    glClearColor(a->bg[0], a->bg[1], a->bg[2], 1.0f);
    glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT);
    update_per_file(a);
    /* the map in its own viewport, the window minus the panel column */
    glViewport(0, (int)(a->fb_h - a->map_y0 - a->map_h), (int)map_w(a), (int)a->map_h);
    static const int WORLD_PROGS[5] = {P_DIR, P_FILE, P_LINE, P_WALL, P_GLYPH};
    for (int i = 0; i < 5; i++)
        set_camera_uniforms(a, WORLD_PROGS[i], true);
    if (a->proj3d) {
        glEnable(GL_DEPTH_TEST);
        glDepthFunc(GL_LEQUAL);
        /* the terrace tops sit at the same depth as the files on them: push
           them back a little so the files never fight them */
        glEnable(GL_POLYGON_OFFSET_FILL);
        glPolygonOffset(1.0f, 2.0f);
    }
    draw_instanced(a, P_DIR, a->n_dirs);
    glDisable(GL_POLYGON_OFFSET_FILL);
    if (a->proj3d) {
        glUseProgram(a->prog[P_WALL]);
        glUniform1i(U(a, P_WALL, "uNFiles"), a->n_files);
        draw_instanced(a, P_WALL, 4 * (a->n_files + a->n_dirs));
    }
    glUseProgram(a->prog[P_FILE]);
    glUniform1i(U(a, P_FILE, "uRingOnly"), 0);
    glUniform1i(U(a, P_FILE, "uTint"), a->tint_on ? 1 : 0);
    draw_instanced(a, P_FILE, a->n_files);
    band_instances(a, &a->rects);              /* kind bands under the bars */
    draw_rects(a, &a->rects, true);
    draw_runs(a, P_LINE, 0, false);
    if (a->prop)
        draw_runs(a, P_GLYPH, 2, true);
    item_instances(a, &a->rects);
    draw_rects(a, &a->rects, true);
    hit_instances(a, &a->rects);
    draw_rects(a, &a->rects, true);
    glUseProgram(a->prog[P_FILE]);             /* borders over the text */
    glUniform1i(U(a, P_FILE, "uRingOnly"), 1);
    draw_instanced(a, P_FILE, a->n_files);
    glDisable(GL_DEPTH_TEST);
    glViewport(0, 0, a->fb_w, a->fb_h);
    draw_ui(a);
}

/* the frame as a binary PPM, read before the swap */
static void screenshot(App *a, const char *path)
{
    int w = a->fb_w, h = a->fb_h;
    uint8_t *px = malloc((size_t)w * h * 4);
    glReadPixels(0, 0, w, h, GL_RGBA, GL_UNSIGNED_BYTE, px);
    FILE *f = fopen(path, "wb");
    if (!f)
        die("error: cannot write %s", path);
    fprintf(f, "P6\n%d %d\n255\n", w, h);
    for (int y = h - 1; y >= 0; y--)
        for (int x = 0; x < w; x++)
            fwrite(px + ((size_t)y * w + x) * 4, 1, 3, f);
    fclose(f);
    free(px);
    printf("screenshot -> %s\n", path);
}

static void handle_events(App *a)
{
    SDL_Event e;
    while (SDL_PollEvent(&e)) {
        switch (e.type) {
        case SDL_QUIT:
            a->running = false;
            break;
        case SDL_MOUSEBUTTONDOWN:
        case SDL_MOUSEBUTTONUP:
            on_cursor(a, e.button.x, e.button.y);
            on_mouse_button(a, e.button.button, e.type == SDL_MOUSEBUTTONDOWN, e.button.x, e.button.y);
            break;
        case SDL_MOUSEMOTION:
            on_cursor(a, e.motion.x, e.motion.y);
            break;
        case SDL_MOUSEWHEEL:
            on_scroll(a, e.wheel.preciseY);
            break;
        case SDL_MULTIGESTURE:     /* two-finger pinch: zoom about the fingers' centre */
            if (e.mgesture.numFingers == 2 && e.mgesture.dDist != 0.0f) {
                a->fly.on = false;
                a->zoom_vel = 0.0;
                a->dragging = a->pressed = false;      /* the first finger's mouse drag stops */
                zoom_about(a, e.mgesture.x * a->fb_w, e.mgesture.y * a->fb_h, exp(e.mgesture.dDist * 4.0));
            }
            break;
        case SDL_KEYDOWN:
            on_key(a, e.key.keysym.sym);
            break;
        case SDL_TEXTINPUT:
            on_text(a, e.text.text);
            break;
        case SDL_WINDOWEVENT:
            if (e.window.event == SDL_WINDOWEVENT_LEAVE)
                a->has_cursor = false;
            break;
        }
    }
}

static void frame(void *arg)
{
    App *a = arg;
    double now = now_s(), dt = now - a->last_t;
    a->last_t = now;
    if (a->frames <= 0)
        handle_events(a);
    else
        SDL_PumpEvents();
    update_glide(a, dt);
    update_fly(a);
    render(a);
    a->frame++;
    if (a->frames > 0 && a->frame >= a->frames) {
        if (a->screenshot)
            screenshot(a, a->screenshot);
        a->running = false;
    }
    SDL_GL_SwapWindow(a->win);
#ifdef __EMSCRIPTEN__
    if (!a->ready_sent) {         /* the page drops its loading screen */
        a->ready_sent = true;
        EM_ASM({ if (Module.btReady) Module.btReady(); });
    }
#endif
    a->fps_n++;
    if (now - a->fps_t > 0.5) {
        double fps = a->fps_n / (now - a->fps_t);
        char title[600];
        snprintf(title, sizeof title, "big-text: %s | %5.1f fps | %.1f px/line at centre", a->name, fps,
                 ref_pitch(a) * a->zoom);
#ifdef __EMSCRIPTEN__
        EM_ASM({ if (Module.btStatus) Module.btStatus($0, $1); }, fps, ref_pitch(a) * a->zoom);
#else
        SDL_SetWindowTitle(a->win, title);
#endif
        a->fps_t = now;
        a->fps_n = 0;
    }
#ifdef __EMSCRIPTEN__
    if (!a->running)
        emscripten_cancel_main_loop();
#endif
}

static const char *default_shader_dir(void)
{
#ifdef __EMSCRIPTEN__
    return "/shaders";    /* embedded by build_web.sh */
#else
    static char dir[1100];
    char *base = SDL_GetBasePath();         /* c/ when run from the build */
    snprintf(dir, sizeof dir, "%s../shaders", base ? base : "./");
    SDL_free(base);
    return dir;
#endif
}

int main(int argc, char **argv)
{
    static App app;       /* static: on the web it outlives main() */
    App *a = &app;
    a->vector_text = true;
    a->frames = 0;
    const char *atlas = NULL, *goto_arg = NULL, *filter = NULL;
    double zoom_ppl = -1;
    bool heights = false;
    for (int i = 1; i < argc; i++) {
        const char *s = argv[i];
        const char *next = i + 1 < argc ? argv[i + 1] : NULL;
        if (!strcmp(s, "--shaders") && next)
            a->shader_dir = argv[++i];
        else if (!strcmp(s, "--no-vector-text"))
            a->vector_text = false;
        else if (!strcmp(s, "--goto") && next)
            goto_arg = argv[++i];
        else if (!strcmp(s, "--zoom") && next)
            zoom_ppl = atof(argv[++i]);
        else if (!strcmp(s, "--filter") && next)
            filter = argv[++i];
        else if (!strcmp(s, "--proj") && next)
            a->proj3d = !strcmp(argv[++i], "3d");
        else if (!strcmp(s, "--tilt") && next)
            a->tilt = atof(argv[++i]);
        else if (!strcmp(s, "--yaw") && next)
            a->yaw = atof(argv[++i]);
        else if (!strcmp(s, "--heights"))
            heights = true;
        else if (!strcmp(s, "--tint"))
            a->tint_on = true;
        else if (!strcmp(s, "--frames") && next)
            a->frames = atoi(argv[++i]);
        else if (!strcmp(s, "--screenshot") && next)
            a->screenshot = argv[++i];
        else if (s[0] != '-' && !atlas)
            atlas = s;
        else {
            fprintf(stderr, "usage: bt_viewer ATLAS [--shaders DIR] [--no-vector-text] [--goto PATH[:LINE]]\n"
                            "       [--zoom PX_PER_LINE] [--filter WORD] [--proj 2d|3d] [--tilt DEG] [--yaw DEG]\n"
                            "       [--heights] [--tint] [--frames N] [--screenshot OUT.ppm]\n");
            return 2;
        }
    }
    if (!atlas) {
        fprintf(stderr, "usage: bt_viewer ATLAS   (a viewer.bin, or the atlas directory holding one)\n");
        return 2;
    }
    if (a->proj3d && a->tilt == 0.0)
        a->tilt = 55.0;
    double t0 = now_s();
    load_corpus(a, atlas);
    a->tint_on = a->tint_on && a->file_tint;
    if (heights && !a->flat) {
        a->heights = true;
        compute_heights(a);
    }
    if (!a->shader_dir)
        a->shader_dir = default_shader_dir();
    double t1 = now_s();
    open_window(a);
    setup_gl(a);
    bind_textures(a);
    double t2 = now_s();
    printf("%s: %d files, %d lines, %lld chars, %d dirs; loaded in %.0f ms, GPU %.1f MB in %.0f ms; "
           "framebuffer %dx%d (%gx)%s%s%s\n",
           a->name, a->n_files, a->n_lines, (long long)a->n_chars, a->n_dirs, (t1 - t0) * 1000,
           a->gpu_bytes / 1e6, (t2 - t1) * 1000, a->fb_w, a->fb_h, a->px,
           a->vector_text ? "; vector text from 12 px per line" : "", a->tint_label[0] ? "; tint: " : "",
           a->tint_label);

    fit(a);
    if (goto_arg) {
        goto_spec(a, goto_arg);
        a->z_focus = a->proj3d ? focus_target(a) : 0.0;
    }
    if (zoom_ppl > 0)
        set_zoom_ppl(a, zoom_ppl);
    if (filter)
        set_filter(a, filter);
    a->running = true;
    a->last_t = a->fps_t = now_s();
#ifdef __EMSCRIPTEN__
    emscripten_set_main_loop_arg(frame, a, 0, true);   /* does not return */
#else
    while (a->running)
        frame(a);
    SDL_GL_DeleteContext(a->ctx);
    SDL_DestroyWindow(a->win);
    SDL_Quit();
#endif
    return 0;
}
