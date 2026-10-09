/* Retained character-cell rendering, Unicode input and bounded VT output.
   Licensing and source attribution are preserved in the project notices. */
#define _CRT_SECURE_NO_WARNINGS
#define _POSIX_C_SOURCE 200809L
#include "px_backend.h"
#include "command_schema.h"
#include "pt_unicode.h"
#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#else
#include <fcntl.h>
#include <sys/ioctl.h>
#include <termios.h>
#include <unistd.h>
#endif
#ifdef PT_WITH_SDL_IMAGE
#include <SDL3/SDL.h>
#include <SDL3_image/SDL_image.h>
#endif

#define PT_MAX_CELLS (1024u * 1024u)
#define PT_MAX_TEXT (8u * 1024u * 1024u)
#define PT_MAX_IMAGE_BYTES (8u * 1024u * 1024u)
#define PT_IMAGE_PREFIX "data:image/png;base64,"
#define PT_MAX_IMAGE_SOURCE ((sizeof(PT_IMAGE_PREFIX) - 1) + 4u * ((PT_MAX_IMAGE_BYTES + 2u) / 3u))
#define PT_IMAGE_CACHE_BUDGET (32u * 1024u * 1024u)
#define PT_MAX_POOL (16u * 1024u * 1024u)
#define PT_HASH 4096
#define PT_MAX_COMMANDS 200000u
#define PT_SCENE_BUDGET (64u * 1024u * 1024u)

/* Protocol paths are UTF-8; the Windows narrow CRT uses the ANSI code page. */
static FILE *capture_file(const char *path) {
#ifdef _WIN32
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, NULL, 0);
    wchar_t *wide;
    FILE *file;
    if (!count)
        return NULL;
    wide = (wchar_t *)malloc((size_t)count * sizeof(*wide));
    if (!wide)
        return NULL;
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, path, -1, wide, count)) {
        free(wide);
        return NULL;
    }
    file = _wfopen(wide, L"wb");
    free(wide);
    return file;
#else
    return fopen(path, "wb");
#endif
}

typedef struct {
    int r, g, b, a;
} Color;
typedef struct {
    double x, y, w, h;
} Rect;
typedef struct {
    int64_t l, t, r, b;
} Box;
typedef struct {
    const char *text;
    Color fg, bg;
    unsigned char width, underline, ink;
} Cell;
typedef struct Glyph {
    struct Glyph *next;
    unsigned hash;
    char text[1];
} Glyph;
typedef struct {
    Cell *cells;
    int cols, rows, refs, failed, mono;
    Box clip;
    Color background;
    Glyph *glyphs[PT_HASH];
    size_t pool_size;
} Frame;
typedef struct {
    char *data;
    size_t size, capacity;
    int failed;
} Buffer;
typedef struct {
    uint32_t cp;
    PTUnicode p;
} Rune;
typedef struct {
    const char *at;
    Rune pending;
    int has_pending;
} TextIterator;
typedef struct {
    char text[264];
    int width, kind;
} Cluster;
typedef struct ImageResource {
    struct ImageResource *next;
    char *source;
    unsigned char *pixels;
    int width, height;
    size_t bytes;
} ImageResource;
typedef struct {
    char *id;
    cJSON *commands;
    size_t count, bytes;
} PTSegment;
typedef struct {
    PTSegment *segment;
    size_t position;
} PTSegmentSlot;

struct PTBackend {
    int cols, rows, headless, active, color_mode, input_failed, keyboard_enabled, io_failed;
    cJSON *commands, *events;
    Frame *current, *previous;
    Buffer output;
    size_t sent;
    char *title;
    PTSegment **segments;
    PTSegmentSlot *segment_table;
    size_t segment_count, segment_capacity, command_count, retained_scene_bytes;
    Color scene_background;
    int segmented, pending_work, images_dirty;
    uint64_t commands_touched, commands_replayed, last_commands_touched, last_commands_replayed;
    uint64_t segments_updated, segments_removed, segments_tested, last_segments_tested;
    uint64_t scene_patch_updates, unchanged_segments, cell_frames_composed, cell_cache_hits;
    uint64_t ansi_frames_encoded, ansi_bytes_encoded, terminal_bytes_written;
    uint64_t resource_revision;
    ImageResource *images;
    size_t image_bytes;
    unsigned image_count;
    unsigned char *input;
    size_t input_size, input_cap;
    double escape_at;
    Buffer paste;
    int in_paste, paste_overflow;
    double pointer_x, pointer_y;
#ifdef _WIN32
    HANDLE input_handle, output_handle;
    DWORD input_mode, output_mode;
    UINT output_cp;
    HANDLE reader;
    volatile LONG stop_reader, read_failed;
    CRITICAL_SECTION input_lock;
    Buffer incoming;
    int lock_ready;
    HANDLE writer, writer_wake, writer_done;
    volatile LONG stop_writer;
    int writer_busy, writer_ok, write_failed;
    DWORD writer_count, writer_written;
    unsigned char writer_bytes[65536];
#else
    struct termios saved;
    int input_flags, output_flags, in_fd, out_fd;
#endif
};

static void fail(char *out, int size, const char *text) {
    if (out && size > 0)
        snprintf(out, (size_t)size, "%s", text);
}
static const cJSON *item(const cJSON *j, const char *key) {
    return cJSON_GetObjectItemCaseSensitive(j, key);
}
static const cJSON *arg(const cJSON *j, int i) {
    return cJSON_GetArrayItem(j, i);
}
static const char *str(const cJSON *j, const char *fallback) {
    return cJSON_IsString(j) ? j->valuestring : fallback;
}
static double num(const cJSON *j, double fallback) {
    return cJSON_IsNumber(j) ? j->valuedouble : fallback;
}
static int boolean(const cJSON *j, int fallback) {
    return cJSON_IsBool(j) ? cJSON_IsTrue(j) : cJSON_IsNumber(j) ? j->valueint : fallback;
}
static char *copy_string(const char *s) {
    size_t n = strlen(s) + 1;
    char *p = (char *)malloc(n);
    if (p)
        memcpy(p, s, n);
    return p;
}
static double now_seconds(void) {
#ifdef _WIN32
    LARGE_INTEGER t, f;
    QueryPerformanceCounter(&t);
    QueryPerformanceFrequency(&f);
    return (double)t.QuadPart / (double)f.QuadPart;
#else
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
#endif
}
static int append(Buffer *b, const void *data, size_t count) {
    size_t capacity, needed;
    char *next;
    if (b->failed || count > SIZE_MAX - b->size - 1) {
        b->failed = 1;
        return 0;
    }
    needed = b->size + count + 1;
    if (needed > b->capacity) {
        capacity = b->capacity ? b->capacity : 1024;
        while (capacity < needed) {
            if (capacity > SIZE_MAX / 2) {
                capacity = needed;
                break;
            }
            capacity *= 2;
        }
        next = (char *)realloc(b->data, capacity);
        if (!next) {
            b->failed = 1;
            return 0;
        }
        b->data = next;
        b->capacity = capacity;
    }
    if (count)
        memcpy(b->data + b->size, data, count);
    b->size += count;
    b->data[b->size] = 0;
    return 1;
}
static int emit(Buffer *b, const char *s) {
    return append(b, s, strlen(s));
}
static int bounded_int(double n) {
    if (n > 1000000000)
        return 1000000000;
    if (n < -1000000000)
        return -1000000000;
    return (int)n;
}
static int64_t integer(double n) {
    if (n > 9e15)
        return INT64_C(9000000000000000);
    if (n < -9e15)
        return -INT64_C(9000000000000000);
    return (int64_t)n;
}
static int channel(double n) {
    return n < 0 ? 0 : n > 255 ? 255 : (int)nearbyint(n);
}
static Color mix(Color a, Color b, double v) {
    Color c = {channel(a.r + (b.r - a.r) * v), channel(a.g + (b.g - a.g) * v), channel(a.b + (b.b - a.b) * v),
               channel(a.a + (b.a - a.a) * v)};
    return c;
}
static Color over(Color a, Color b) {
    Color c = {(a.r * a.a + b.r * (255 - a.a) + 127) / 255, (a.g * a.a + b.g * (255 - a.a) + 127) / 255,
               (a.b * a.a + b.b * (255 - a.a) + 127) / 255, 255};
    return c;
}
static int same_color(Color a, Color b) {
    return a.r == b.r && a.g == b.g && a.b == b.b;
}
static int hex_digit(char c) {
    if (c >= '0' && c <= '9')
        return c - '0';
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    if (c >= 'A' && c <= 'F')
        return c - 'A' + 10;
    return -1;
}
static int color(const char *s, Color *result) {
    size_t n;
    int v[8], i;
    Color c = {0, 0, 0, 0};
    if (!s)
        return 0;
    if (!*s) {
        *result = c;
        return 1;
    }
    if (*s == '#')
        s++;
    n = strlen(s);
    if (n != 3 && n != 4 && n != 6 && n != 8)
        return 0;
    for (i = 0; i < (int)n; i++)
        if ((v[i] = hex_digit(s[i])) < 0)
            return 0;
    if (n <= 4) {
        c.r = v[0] * 17;
        c.g = v[1] * 17;
        c.b = v[2] * 17;
        c.a = n == 4 ? v[3] * 17 : 255;
    } else {
        c.r = v[0] * 16 + v[1];
        c.g = v[2] * 16 + v[3];
        c.b = v[4] * 16 + v[5];
        c.a = n == 8 ? v[6] * 16 + v[7] : 255;
    }
    *result = c;
    return 1;
}
static int rect_value(const cJSON *j, Rect *r) {
    double *values[4] = {&r->x, &r->y, &r->w, &r->h};
    int i;
    if (!cJSON_IsArray(j) || cJSON_GetArraySize(j) != 4)
        return 0;
    for (i = 0; i < 4; i++) {
        const cJSON *v = arg(j, i);
        if (!cJSON_IsNumber(v) || !isfinite(v->valuedouble))
            return 0;
        *values[i] = v->valuedouble;
    }
    return isfinite(r->x + r->w) && isfinite(r->y + r->h);
}
static Box box(Rect r) {
    Box b = {0, 0, 0, 0};
    if (r.w > 0 && r.h > 0) {
        b.l = integer(ceil(r.x / 8 - .5));
        b.t = integer(ceil(r.y / 16 - .5));
        b.r = integer(ceil((r.x + r.w) / 8 - .5));
        b.b = integer(ceil((r.y + r.h) / 16 - .5));
    }
    return b;
}
static Box bounds(Box a, Box b) {
    Box c = {a.l > b.l ? a.l : b.l, a.t > b.t ? a.t : b.t, a.r < b.r ? a.r : b.r, a.b < b.b ? a.b : b.b};
    return c;
}
static Cell blank(Color bg) {
    Cell c = {" ", {255, 255, 255, 255}, {0, 0, 0, 255}, 1, 0, 0};
    c.bg = bg;
    return c;
}
static Frame *frame_create(int cols, int rows, int mono) {
    Frame *f = (Frame *)calloc(1, sizeof(*f));
    if (!f)
        return NULL;
    f->cells = (Cell *)calloc((size_t)cols * rows, sizeof(Cell));
    if (!f->cells) {
        free(f);
        return NULL;
    }
    f->cols = cols;
    f->rows = rows;
    f->refs = 1;
    f->mono = mono;
    f->clip = (Box){0, 0, cols, rows};
    return f;
}
static void frame_release(Frame *f) {
    int i;
    if (!f || --f->refs)
        return;
    for (i = 0; i < PT_HASH; i++) {
        Glyph *g = f->glyphs[i];
        while (g) {
            Glyph *next = g->next;
            free(g);
            g = next;
        }
    }
    free(f->cells);
    free(f);
}
static const char *intern(Frame *f, const char *text) {
    unsigned h = 2166136261u;
    size_t n = strlen(text), i;
    Glyph *g;
    for (i = 0; i < n; i++)
        h = (h ^ (unsigned char)text[i]) * 16777619u;
    for (g = f->glyphs[h % PT_HASH]; g; g = g->next)
        if (g->hash == h && !strcmp(g->text, text))
            return g->text;
    if (f->pool_size + n + sizeof(*g) > PT_MAX_POOL) {
        f->failed = 1;
        return "?";
    }
    g = (Glyph *)malloc(sizeof(*g) + n);
    if (!g) {
        f->failed = 1;
        return "?";
    }
    g->hash = h;
    memcpy(g->text, text, n + 1);
    g->next = f->glyphs[h % PT_HASH];
    f->glyphs[h % PT_HASH] = g;
    f->pool_size += sizeof(*g) + n;
    return g->text;
}
static void begin_frame(Frame *f, Color c) {
    size_t i, n = (size_t)f->cols * f->rows;
    f->background = over(c, (Color){0, 0, 0, 255});
    for (i = 0; i < n; i++)
        f->cells[i] = blank(f->background);
    f->clip = (Box){0, 0, f->cols, f->rows};
}
static int visible(Frame *f, int64_t x, int64_t y) {
    return x >= f->clip.l && x < f->clip.r && y >= f->clip.t && y < f->clip.b;
}
static void erase(Frame *f, int x, int y) {
    Cell c = f->cells[(size_t)y * f->cols + x];
    int start = c.width == 0 ? x - 1 : x, end = c.width == 2 ? x + 2 : x + 1, i;
    for (i = start < 0 ? 0 : start; i < end && i < f->cols; i++) {
        Cell *p = &f->cells[(size_t)y * f->cols + i];
        *p = blank(p->bg);
    }
}
static void put(Frame *f, int64_t x, int64_t y, const char *text, int width, Color ink, int text_ink) {
    int i;
    Cell *c;
    if (!ink.a || !visible(f, x, y) || x + width > f->clip.r)
        return;
    for (i = 0; i < width; i++)
        if (f->cells[(size_t)y * f->cols + (size_t)x + i].width != 1)
            erase(f, (int)x + i, (int)y);
    c = &f->cells[(size_t)y * f->cols + (size_t)x];
    c->text = intern(f, text);
    c->fg = over(ink, c->bg);
    c->width = (unsigned char)width;
    c->underline = 0;
    c->ink = (unsigned char)text_ink;
    if (width == 2) {
        c[1] = *c;
        c[1].text = "";
        c[1].width = 0;
    }
}
static char luminance(Color a, Color b) {
    static const char ramp[] = " .:-=+*#%@";
    double v = ((a.r + b.r) * 2126.0 + (a.g + b.g) * 7152.0 + (a.b + b.b) * 722.0) / (2 * 10000 * 255);
    return ramp[(int)nearbyint(v * 9)];
}
static void fill(Frame *f, int x, int y, Color ink) {
    Cell *p = &f->cells[(size_t)y * f->cols + x];
    Color fg, bg;
    int start, i;
    if (!ink.a)
        return;
    if (ink.a == 255) {
        if (p->width != 1)
            erase(f, x, y);
        *p = blank(ink);
        return;
    }
    fg = over(ink, p->fg);
    bg = over(ink, p->bg);
    if (p->width != 1) {
        start = p->width == 0 ? x - 1 : x;
        for (i = start; i <= start + 1; i++) {
            Cell *c = &f->cells[(size_t)y * f->cols + i];
            c->fg = fg;
            c->bg = bg;
        }
    } else {
        p->fg = fg;
        p->bg = bg;
        if (f->mono && p->ink == 2) {
            char s[2] = {luminance(fg, bg), 0};
            p->text = intern(f, s);
        }
    }
}
static const char *const box_glyphs[] = {"│", "─", "┌", "┐", "└", "┘", "├", "┤", "┬", "┴", "┼", "╭", "╮", "╰", "╯"};
static const int box_masks[] = {5, 10, 6, 12, 3, 9, 7, 13, 14, 11, 15, 6, 12, 3, 9};
static int glyph_mask(const char *s) {
    int i;
    for (i = 0; i < 15; i++)
        if (!strcmp(s, box_glyphs[i]))
            return box_masks[i];
    return 0;
}
static void stroke(Frame *f, int64_t x, int64_t y, const char *glyph, Color ink, int merge) {
    Cell *old, *owner;
    int a, b, i;
    if (!visible(f, x, y))
        return;
    old = &f->cells[(size_t)y * f->cols + (size_t)x];
    if (old->ink == 1 || old->underline)
        return;
    owner = old->width == 0 ? old - 1 : old;
    if (ink.a < 128 && strcmp(owner->text, " ") && *owner->text && !glyph_mask(owner->text))
        return;
    if (merge && (a = glyph_mask(old->text)) && (b = glyph_mask(glyph)))
        for (i = 0; i < 11; i++)
            if (box_masks[i] == (a | b)) {
                glyph = box_glyphs[i];
                break;
            }
    put(f, x, y, glyph, 1, ink, 0);
}
static PTUnicode properties(uint32_t cp) {
    size_t lo = 0, hi = sizeof(pt_unicode_ranges) / sizeof(pt_unicode_ranges[0]);
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        if (pt_unicode_ranges[mid].hi < cp)
            lo = mid + 1;
        else
            hi = mid;
    }
    return pt_unicode_ranges[lo];
}
static uint32_t decode(const char **at) {
    const unsigned char *p = (const unsigned char *)*at;
    uint32_t cp;
    int n, i;
    if (!*p)
        return 0;
    if (*p < 128) {
        *at += 1;
        return *p;
    }
    n = *p >= 0xc2 && *p <= 0xdf ? 2 : *p >= 0xe0 && *p <= 0xef ? 3 : *p >= 0xf0 && *p <= 0xf4 ? 4 : 0;
    if (!n) {
        *at += 1;
        return 0xfffd;
    }
    cp = *p & ((1u << (7 - n)) - 1);
    if ((p[0] == 0xe0 && p[1] && p[1] < 0xa0) || (p[0] == 0xed && p[1] >= 0xa0) ||
        (p[0] == 0xf0 && p[1] && p[1] < 0x90) || (p[0] == 0xf4 && p[1] >= 0x90)) {
        *at += 1;
        return 0xfffd;
    }
    for (i = 1; i < n; i++)
        if (!p[i] || (p[i] & 0xc0) != 0x80) {
            *at += i;
            return 0xfffd;
        } else
            cp = (cp << 6) | (p[i] & 63);
    if ((n == 2 && cp < 128) || (n == 3 && cp < 2048) || (n == 4 && cp < 65536) || cp > 0x10ffff ||
        (cp >= 0xd800 && cp <= 0xdfff)) {
        *at += 1;
        return 0xfffd;
    }
    *at += n;
    return cp;
}
static int encode(uint32_t cp, char *s) {
    if (cp < 128) {
        s[0] = (char)cp;
        return 1;
    }
    if (cp < 2048) {
        s[0] = (char)(0xc0 | (cp >> 6));
        s[1] = (char)(0x80 | (cp & 63));
        return 2;
    }
    if (cp < 65536) {
        s[0] = (char)(0xe0 | (cp >> 12));
        s[1] = (char)(0x80 | ((cp >> 6) & 63));
        s[2] = (char)(0x80 | (cp & 63));
        return 3;
    }
    s[0] = (char)(0xf0 | (cp >> 18));
    s[1] = (char)(0x80 | ((cp >> 12) & 63));
    s[2] = (char)(0x80 | ((cp >> 6) & 63));
    s[3] = (char)(0x80 | (cp & 63));
    return 4;
}
static const PTCase *case_of(uint32_t cp) {
    size_t lo = 0, hi = sizeof(pt_case_table) / sizeof(pt_case_table[0]);
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        if (pt_case_table[mid].cp < cp)
            lo = mid + 1;
        else
            hi = mid;
    }
    return lo < sizeof(pt_case_table) / sizeof(pt_case_table[0]) && pt_case_table[lo].cp == cp ? &pt_case_table[lo]
                                                                                               : NULL;
}
static int next_rune(TextIterator *it, Rune *r) {
    if (it->has_pending) {
        *r = it->pending;
        it->has_pending = 0;
        return 1;
    }
    while (*it->at) {
        r->cp = decode(&it->at);
        r->p = properties(r->cp);
        if (!(r->p.flags & 1) || r->cp == 10 || r->cp == 9 || r->cp == 0x200c || r->cp == 0x200d)
            return 1;
    }
    return 0;
}
static int control_gcb(int c) {
    return c == PT_GCB_CONTROL || c == PT_GCB_CR || c == PT_GCB_LF;
}
static int simple_break(int l, int r) {
    if (l == PT_GCB_CR && r == PT_GCB_LF)
        return 0;
    if (control_gcb(l) || control_gcb(r))
        return 1;
    if (l == PT_GCB_L && (r == PT_GCB_L || r == PT_GCB_V || r == PT_GCB_LV || r == PT_GCB_LVT))
        return 0;
    if ((l == PT_GCB_LV || l == PT_GCB_V) && (r == PT_GCB_V || r == PT_GCB_T))
        return 0;
    if ((l == PT_GCB_LVT || l == PT_GCB_T) && r == PT_GCB_T)
        return 0;
    if (r == PT_GCB_EXTEND || r == PT_GCB_ZWJ || r == PT_GCB_SPACINGMARK || l == PT_GCB_PREPEND)
        return 0;
    return -1;
}
static int next_cluster(TextIterator *it, Cluster *out) {
    Rune r;
    int previous = PT_GCB_OTHER, regional = 0, emoji = 0, indic = 0, count = 0, n = 0, base = 0, wide = 0,
        variation = 0, joiner = 0, pict = 0;
    out->kind = 0;
    while (next_rune(it, &r)) {
        int split = simple_break(previous, r.p.gcb);
        if (split < 0)
            split = !((r.p.incb == PT_INCB_CONSONANT && indic == 2) || ((r.p.flags & 16) && emoji == 2) ||
                      (r.p.gcb == PT_GCB_REGIONAL_INDICATOR && (regional % 2)));
        if (count && split) {
            it->pending = r;
            it->has_pending = 1;
            break;
        }
        if (count < 64) {
            n += encode(r.cp, out->text + n);
            if (!(r.p.flags & 4)) {
                base = 1;
                if (r.p.flags & 8)
                    wide = 1;
                if (r.cp >= 0x1f1e6 && r.cp <= 0x1f1ff)
                    wide = 1;
            }
            if (r.cp == 0xfe0f || r.cp == 0x20e3)
                variation = 1;
            if (r.cp == 0x200d)
                joiner = 1;
            if ((r.cp >= 0x2600 && r.cp <= 0x27bf) || (r.cp >= 0x1f000 && r.cp <= 0x1faff))
                pict = 1;
        }
        regional = r.p.gcb == PT_GCB_REGIONAL_INDICATOR ? regional + 1 : 0;
        emoji = (r.p.flags & 16)                         ? 1
                : emoji == 1 && r.p.gcb == PT_GCB_EXTEND ? 1
                : emoji == 1 && r.p.gcb == PT_GCB_ZWJ    ? 2
                                                         : 0;
        indic = r.p.incb == PT_INCB_CONSONANT         ? 1
                : indic && r.p.incb == PT_INCB_LINKER ? 2
                : r.p.incb == PT_INCB_EXTEND          ? indic
                                                      : 0;
        previous = r.p.gcb;
        count++;
    }
    if (!count)
        return 0;
    out->text[n] = 0;
    out->width = wide || variation || (joiner && pict) ? 2 : 1;
    if (n == 1 && (out->text[0] == '\n' || out->text[0] == '\t')) {
        out->kind = out->text[0];
        out->width = 0;
    } else if (!base) {
        memmove(out->text + 3, out->text, (size_t)n + 1);
        memcpy(out->text, "\xe2\x97\x8c", 3);
        out->width = 1;
    }
    return 1;
}
static void measure_text(const char *text, double *w, double *h) {
    TextIterator it = {text, {0}, 0};
    Cluster c;
    int64_t col = 0, maximum = 0, lines = 1;
    while (next_cluster(&it, &c)) {
        if (c.kind == 10) {
            if (col > maximum)
                maximum = col;
            col = 0;
            lines++;
        } else if (c.kind == 9)
            col += 4 - col % 4;
        else
            col += c.width;
    }
    if (col > maximum)
        maximum = col;
    *w = (double)maximum * 8;
    *h = (double)lines * 16;
}
static void draw_text(Frame *f, const char *text, double x, double y, Color ink) {
    TextIterator it = {text, {0}, 0};
    Cluster c;
    int64_t col = integer(ceil(x / 8 - .5)), row = integer(ceil(y / 16 - .5)), start = col;
    while (next_cluster(&it, &c)) {
        if (c.kind == 10) {
            col = start;
            row++;
        } else if (c.kind == 9) {
            int i, n = 4 - (int)((col - start) % 4);
            for (i = 0; i < n; i++)
                put(f, col++, row, " ", 1, ink, 1);
        } else {
            put(f, col, row, c.text, c.width, ink, 1);
            col += c.width;
        }
        if (row >= f->clip.b)
            break;
    }
}

static void draw_line(Frame *f, double x1, double y1, double x2, double y2, Color ink, double width) {
    double a = x1 / 8, b = y1 / 16, c = x2 / 8, d = y2 / 16, dx = c - a, dy = d - b, low = 0, high = 1, p[4], q[4];
    int x, y, end_x, end_y, delta_x, delta_y, step_x, step_y, error, i;
    const char *glyph;
    if (width <= 0 || f->clip.l >= f->clip.r || f->clip.t >= f->clip.b)
        return;
    p[0] = -dx;
    p[1] = dx;
    p[2] = -dy;
    p[3] = dy;
    q[0] = a - f->clip.l;
    q[1] = f->clip.r - 1e-9 - a;
    q[2] = b - f->clip.t;
    q[3] = f->clip.b - 1e-9 - b;
    for (i = 0; i < 4; i++) {
        if (p[i] == 0) {
            if (q[i] < 0)
                return;
        } else if (p[i] < 0) {
            double t = q[i] / p[i];
            if (t > low)
                low = t;
        } else {
            double t = q[i] / p[i];
            if (t < high)
                high = t;
        }
    }
    if (low > high)
        return;
    x = bounded_int(floor(a + low * dx));
    y = bounded_int(floor(b + low * dy));
    end_x = bounded_int(floor(a + high * dx));
    end_y = bounded_int(floor(b + high * dy));
    if (x < f->clip.l)
        x = (int)f->clip.l;
    if (x >= f->clip.r)
        x = (int)f->clip.r - 1;
    if (y < f->clip.t)
        y = (int)f->clip.t;
    if (y >= f->clip.b)
        y = (int)f->clip.b - 1;
    if (end_x < f->clip.l)
        end_x = (int)f->clip.l;
    if (end_x >= f->clip.r)
        end_x = (int)f->clip.r - 1;
    if (end_y < f->clip.t)
        end_y = (int)f->clip.t;
    if (end_y >= f->clip.b)
        end_y = (int)f->clip.b - 1;
    delta_x = abs(end_x - x);
    delta_y = abs(end_y - y);
    step_x = x < end_x ? 1 : -1;
    step_y = y < end_y ? 1 : -1;
    error = delta_x - delta_y;
    glyph = dy == 0 || (dx != 0 && delta_y == 0) ? "─"
            : dx == 0 || delta_x == 0            ? "│"
            : (dx > 0) == (dy > 0)               ? "╲"
                                                 : "╱";
    for (i = 0; i <= (delta_x > delta_y ? delta_x : delta_y); i++) {
        int twice;
        stroke(f, x, y, glyph, ink, 1);
        if (x == end_x && y == end_y)
            break;
        twice = error * 2;
        if (twice > -delta_y) {
            error -= delta_y;
            x += step_x;
        }
        if (twice < delta_x) {
            error += delta_x;
            y += step_y;
        }
    }
}
static void border_put(Frame *f, Box b, int64_t x, int64_t y, const char *glyph, Color a, Color z, int gradient,
                       int horizontal) {
    double v = horizontal ? (double)(x - b.l) / fmax(1, (double)(b.r - b.l - 1))
                          : (double)(y - b.t) / fmax(1, (double)(b.b - b.t - 1));
    stroke(f, x, y, glyph, gradient ? mix(a, z, v) : a, 0);
}
static void border(Frame *f, Box b, Color a, Color z, int gradient, int horizontal, double radius, int compact) {
    Box v = bounds(b, f->clip);
    int64_t x, y, x1 = b.l, y1 = b.t, x2 = b.r - 1, y2 = b.b - 1;
    const char *const square[] = {"┌", "┐", "└", "┘"}, *const round[] = {"╭", "╮", "╰", "╯"};
    const char *const *corners = radius > 0 ? round : square;
    if (x2 < x1 || y2 < y1)
        return;
    if (compact && y2 - y1 < 2 && x1 != x2) {
        for (y = v.t; y < v.b; y++) {
            border_put(f, b, x1, y, y1 == y2 ? "[" : corners[y == y1 ? 0 : 2], a, z, gradient, horizontal);
            border_put(f, b, x2, y, y1 == y2 ? "]" : corners[y == y1 ? 1 : 3], a, z, gradient, horizontal);
        }
        return;
    }
    for (x = v.l; x < v.r; x++)
        if ((x1 < x && x < x2) || y1 == y2) {
            border_put(f, b, x, y1, "─", a, z, gradient, horizontal);
            if (y2 != y1)
                border_put(f, b, x, y2, "─", a, z, gradient, horizontal);
        }
    if (y1 != y2)
        for (y = v.t; y < v.b; y++)
            if ((y1 < y && y < y2) || x1 == x2) {
                border_put(f, b, x1, y, "│", a, z, gradient, horizontal);
                if (x2 != x1)
                    border_put(f, b, x2, y, "│", a, z, gradient, horizontal);
            }
    if (x1 != x2 && y1 != y2) {
        border_put(f, b, x1, y1, corners[0], a, z, gradient, horizontal);
        border_put(f, b, x2, y1, corners[1], a, z, gradient, horizontal);
        border_put(f, b, x1, y2, corners[2], a, z, gradient, horizontal);
        border_put(f, b, x2, y2, corners[3], a, z, gradient, horizontal);
    }
}
static void draw_rect(Frame *f, Rect r, Color ink, int has_fill, double radius, Color outline, int has_border,
                      double width) {
    Box b = box(r), v = bounds(b, f->clip);
    int64_t x, y;
    if (has_fill && r.w > 0 && r.h > 0) {
        if (r.h < 8 && r.w >= 8) {
            draw_line(f, r.x, r.y, r.x + r.w - 4, r.y, ink, 1);
            return;
        }
        if (r.w < 4 && r.h >= 16) {
            draw_line(f, r.x, r.y, r.x, r.y + r.h - 8, ink, 1);
            return;
        }
    }
    if (has_fill)
        for (y = v.t; y < v.b; y++)
            for (x = v.l; x < v.r; x++) {
                if (ink.a < 255 && x > v.l && f->cells[(size_t)y * f->cols + (size_t)x].width == 0)
                    continue;
                fill(f, (int)x, (int)y, ink);
            }
    if (has_border && width > 0 && b.r > b.l && b.b > b.t)
        border(f, b, outline, outline, 0, 0, radius, 0);
}
static void gradient_rect(Frame *f, Rect r, Color a, Color z, int horizontal, double radius, double width) {
    Box b = box(r), v = bounds(b, f->clip);
    int64_t x, y;
    if (width) {
        if (width > 0)
            border(f, b, a, z, 1, horizontal, radius, 0);
        return;
    }
    for (y = v.t; y < v.b; y++)
        for (x = v.l; x < v.r; x++) {
            double amount = horizontal ? (double)(x - b.l) / fmax(1, (double)(b.r - b.l - 1))
                                       : (double)(y - b.t) / fmax(1, (double)(b.b - b.t - 1));
            Color ink = mix(a, z, amount);
            if (ink.a < 255 && x > v.l && f->cells[(size_t)y * f->cols + (size_t)x].width == 0)
                continue;
            fill(f, (int)x, (int)y, ink);
        }
}
static Color style_color(const cJSON *s, const char *name, Color fallback) {
    const cJSON *v = item(s, name);
    Color c;
    if (!cJSON_IsString(v) || !*v->valuestring)
        return fallback;
    return color(v->valuestring, &c) ? c : fallback;
}
static int style_has(const cJSON *s, const char *name) {
    const cJSON *v = item(s, name);
    return cJSON_IsString(v) && *v->valuestring;
}
static void styled_rect(Frame *f, Rect r, const cJSON *s) {
    Box b = box(r), v = bounds(b, f->clip);
    Color a = style_color(s, "fill", (Color){0, 0, 0, 0}), z = style_color(s, "fill_end", a),
          edge = style_color(s, "border", (Color){0, 0, 0, 0}), end = style_color(s, "border_end", edge);
    double radius = fmin(num(item(s, "radius"), 0), fmin(r.w / 2, r.h / 2));
    int has_border = style_has(s, "border") && num(item(s, "border_width"), 0) != 0,
        horizontal = !strcmp(str(item(s, "gradient_axis"), "vertical"), "horizontal");
    int64_t x, row;
    if (r.w <= 0 || r.h <= 0)
        return;
    if (r.h < 8 && r.w >= 8) {
        row = integer(ceil((r.y + r.h / 2) / 16) - 1);
        for (x = v.l; x < v.r; x++) {
            double amount = horizontal ? (double)(x - b.l) / fmax(1, (double)(b.r - b.l - 1)) : .5;
            stroke(f, x, row, "─", mix(has_border ? edge : a, has_border ? end : z, amount), 0);
        }
        return;
    }
    if (style_has(s, "fill")) {
        if (style_has(s, "fill_end") && strcmp(str(item(s, "fill"), ""), str(item(s, "fill_end"), "")))
            gradient_rect(f, r, a, z, horizontal, radius, 0);
        else
            draw_rect(f, r, a, 1, radius, edge, 0, 0);
    }
    if (has_border)
        border(f, b, edge, end, style_has(s, "border_end"), horizontal, radius, 1);
}
static void marker(Frame *f, Rect r, const cJSON *s, const char *shape, int checked) {
    Color a = style_color(s, "fill", (Color){0, 0, 0, 0}), z = style_color(s, "fill_end", a),
          fill_color = mix(a, z, .5), fg = style_color(s, "foreground", fill_color),
          edge = style_color(s, "border", fg), ink;
    int has_border = style_has(s, "border") && num(item(s, "border_width"), 0) != 0;
    int64_t x = integer(ceil((r.x + r.w / 2) / 8) - 1), y = integer(ceil((r.y + r.h / 2) / 16) - 1);
    const char *glyph;
    if (r.w <= 0 || r.h <= 0 || !visible(f, x, y))
        return;
    if (!strcmp(shape, "square")) {
        fill(f, (int)x, (int)y, fill_color);
        glyph = checked ? "✓" : "□";
        ink = checked ? fg : has_border ? edge : fg;
    } else {
        glyph = checked ? "◉" : has_border ? "○" : "●";
        ink = has_border ? edge : checked ? fg : fill_color;
    }
    put(f, x, y, glyph, 1, ink, 1);
}
static void underline(Frame *f, int64_t x, int64_t y, const Color *ink) {
    Cell *p;
    int i;
    if (!visible(f, x, y))
        return;
    p = &f->cells[(size_t)y * f->cols + (size_t)x];
    if (p->width == 0) {
        x--;
        p--;
    }
    if (x < f->clip.l || x + p->width > f->clip.r)
        return;
    for (i = 0; i < p->width; i++) {
        if (ink && !strcmp(p->text, " "))
            p[i].fg = over(*ink, p->bg);
        p[i].underline = 1;
    }
}
static void focus_ring(Frame *f, Rect r, Color ink, double radius) {
    Box b = box(r), v = bounds(b, f->clip);
    int64_t x, y;
    border(f, b, ink, ink, 0, 0, radius, 1);
    if (!f->mono || b.r <= b.l || b.b <= b.t)
        return;
    for (x = v.l; x < v.r; x++) {
        underline(f, x, b.t, NULL);
        if (b.b - 1 != b.t)
            underline(f, x, b.b - 1, NULL);
    }
    for (y = v.t > b.t + 1 ? v.t : b.t + 1; y < v.b && y < b.b - 1; y++) {
        underline(f, b.l, y, NULL);
        if (b.r - 1 != b.l)
            underline(f, b.r - 1, y, NULL);
    }
}
typedef struct {
    const char *name, *glyph;
} Icon;
static const Icon icons[] = {
    {"check", "✓"},        {"close", "×"},         {"plus", "+"},         {"minus", "−"},         {"play", "▶"},
    {"pause", "‖"},        {"home", "⌂"},          {"save", "▣"},         {"folder", "▱"},        {"search", "⌕"},
    {"undo", "↶"},         {"redo", "↷"},          {"chevron_left", "‹"}, {"chevron_right", "›"}, {"chevron_up", "▴"},
    {"chevron_down", "▾"}, {"arrow_left", "←"},    {"arrow_right", "→"},  {"arrow_up", "↑"},      {"arrow_down", "↓"},
    {"diamond", "◇"},      {"settings", "⚙"},      {"layers", "▱▱"},      {"sparkles", "✧"},      {"sun", "☼"},
    {"moon", "☾"},         {"heart", "♥"},         {"copy", "⧉"},         {"download", "↓"},      {"upload", "↑"},
    {"refresh", "↻"},      {"external_link", "↗"}, {"grid", "▦"},         {"chart", "▥"},         {"bolt", "ϟ"},
    {"menu", "≡"},         {"code", "‹›"},         {"mail", "✉"}};
static const char *icon_glyph(const char *name) {
    size_t i;
    for (i = 0; i < sizeof(icons) / sizeof(icons[0]); i++)
        if (!strcmp(name, icons[i].name))
            return icons[i].glyph;
    return NULL;
}
static void draw_icon(Frame *f, const char *glyph, double x, double y, double size, Color ink) {
    Rect r = {x, y, size, size};
    Box b = box(r), saved = f->clip;
    double w, h;
    int64_t col, row;
    int count;
    if (size <= 0)
        return;
    measure_text(glyph, &w, &h);
    count = (int)(w / 8);
    if (b.r - b.l < count || b.b <= b.t)
        return;
    col = integer(ceil((x + size / 2) / 8)) - 1 - count / 2;
    row = integer(ceil((y + size / 2) / 16)) - 1;
    if (col < b.l)
        col = b.l;
    if (col > b.r - count)
        col = b.r - count;
    if (row < b.t)
        row = b.t;
    if (row >= b.b)
        row = b.b - 1;
    f->clip = bounds(b, saved);
    draw_text(f, glyph, (double)col * 8, (double)row * 16, ink);
    f->clip = saved;
}

static void resource_error(PTBackend *r, const char *message) {
    cJSON *e;
    if (cJSON_GetArraySize(r->events) >= 128)
        return;
    e = cJSON_CreateObject();
    if (!e)
        return;
    cJSON_AddStringToObject(e, "kind", "resource_error");
    cJSON_AddStringToObject(e, "text", message);
    cJSON_AddItemToArray(r->events, e);
}
static void release_image(ImageResource *im) {
    if (im) {
        free(im->source);
        free(im->pixels);
        free(im);
    }
}
#ifdef PT_WITH_SDL_IMAGE
/* Match the original PNG resource contract and reject oversized dimensions
   before the image decoder allocates the decompressed pixel buffer. */
static SDL_Surface *load_bounded_png(SDL_IOStream *io) {
    unsigned char *h;
    uint32_t width, height;
    size_t size, pixel_bytes;
    Sint64 length;
    SDL_Surface *surface = NULL;
    if (!io)
        return NULL;
    length = SDL_GetIOSize(io);
    if (length < 33 || length > PT_MAX_IMAGE_BYTES) {
        SDL_CloseIO(io);
        return NULL;
    }
    /* Check and decode one bounded copy; a live file can change after a seek. */
    h = (unsigned char *)malloc((size_t)length + 1);
    size = h ? SDL_ReadIO(io, h, (size_t)length + 1) : 0;
    SDL_CloseIO(io);
    if (!h)
        return NULL;
    if (size != (size_t)length || memcmp(h, "\211PNG\r\n\032\n\0\0\0\rIHDR", 16) || (h[24] == 16 && h[25] != 6))
        goto done;
    width = ((uint32_t)h[16] << 24) | ((uint32_t)h[17] << 16) | ((uint32_t)h[18] << 8) | h[19];
    height = ((uint32_t)h[20] << 24) | ((uint32_t)h[21] << 16) | ((uint32_t)h[22] << 8) | h[23];
    pixel_bytes = h[24] == 16 ? 8 : 4;
    if (!width || !height || width > PT_IMAGE_CACHE_BUDGET / pixel_bytes / height)
        goto done;
    io = SDL_IOFromConstMem(h, size);
    if (io) {
        surface = IMG_LoadPNG_IO(io);
        SDL_CloseIO(io);
    }
done:
    free(h);
    return surface;
}
#endif
static ImageResource *image_resource(PTBackend *r, const char *source) {
    ImageResource **at = &r->images, *im;
    const char *reason = "Native terminal image decoding is unavailable in this build";
    while (*at) {
        if (!strcmp((*at)->source, source)) {
            im = *at;
            *at = im->next;
            im->next = r->images;
            r->images = im;
            return im;
        }
        at = &(*at)->next;
    }
    im = (ImageResource *)calloc(1, sizeof(*im));
    if (!im)
        return NULL;
    im->source = copy_string(source);
    if (!im->source) {
        free(im);
        return NULL;
    }
#ifdef PT_WITH_SDL_IMAGE
    {
        SDL_Surface *raw = NULL, *rgba = NULL;
        if (!strncmp(source, "data:image/png;base64,", 22)) {
            const char *p = source + 22;
            size_t n = strlen(p), used = 0;
            unsigned accumulator = 0;
            int bits = 0, valid = 1;
            unsigned char *decoded = n <= 32u * 1024u * 1024u ? (unsigned char *)malloc(n + 1) : NULL;
            if (decoded) {
                for (; *p; p++) {
                    int v = *p >= 'A' && *p <= 'Z'   ? *p - 'A'
                            : *p >= 'a' && *p <= 'z' ? *p - 'a' + 26
                            : *p >= '0' && *p <= '9' ? *p - '0' + 52
                            : *p == '+'              ? 62
                            : *p == '/'              ? 63
                                                     : -1;
                    if (*p == '=')
                        break;
                    if (v < 0) {
                        valid = 0;
                        break;
                    }
                    accumulator = (accumulator << 6) | (unsigned)v;
                    bits += 6;
                    if (bits >= 8) {
                        bits -= 8;
                        decoded[used++] = (unsigned char)(accumulator >> bits);
                    }
                }
                if (valid) {
                    SDL_IOStream *io = SDL_IOFromConstMem(decoded, used);
                    if (io)
                        raw = load_bounded_png(io);
                }
                free(decoded);
            }
        } else if (!strstr(source, "://") && strncmp(source, "data:", 5)) {
            SDL_IOStream *io = SDL_IOFromFile(source, "rb");
            if (io) {
                Sint64 size = SDL_GetIOSize(io);
                if (size >= 0 && size <= 32 * 1024 * 1024)
                    raw = load_bounded_png(io);
                else
                    SDL_CloseIO(io);
            }
        }
        reason = "Terminal images require bounded PNG data; 16-bit sources must use RGBA channels";
        if (raw && raw->w > 0 && raw->h > 0 && (size_t)raw->w * raw->h <= 8u * 1024u * 1024u)
            rgba = SDL_ConvertSurface(raw, SDL_PIXELFORMAT_RGBA32);
        if (rgba) {
            size_t row_bytes = (size_t)rgba->w * 4;
            int y;
            im->bytes = row_bytes * (size_t)rgba->h;
            im->pixels = (unsigned char *)malloc(im->bytes);
            if (im->pixels) {
                im->width = rgba->w;
                im->height = rgba->h;
                for (y = 0; y < rgba->h; y++)
                    memcpy(im->pixels + (size_t)y * row_bytes,
                           (const unsigned char *)rgba->pixels + (size_t)y * rgba->pitch, row_bytes);
            }
        }
        if (rgba)
            SDL_DestroySurface(rgba);
        if (raw)
            SDL_DestroySurface(raw);
    }
#endif
    if (!im->pixels) {
        im->width = im->height = 0;
        im->bytes = 0;
        resource_error(r, reason);
    }
    /* C retains UTF-8 bytes, not Python's potentially four-byte characters. */
    im->bytes += strlen(source) + 1 + sizeof(*im);
    /* A valid source plus its decoded pixels can exceed the cache budget.
       Let this draw use it without retaining it or evicting smaller entries. */
    if (im->bytes > PT_IMAGE_CACHE_BUDGET)
        return im;
    while (r->images && (r->image_count >= 64 || r->image_bytes + im->bytes > PT_IMAGE_CACHE_BUDGET)) {
        ImageResource **tail = &r->images;
        while ((*tail)->next)
            tail = &(*tail)->next;
        r->image_bytes -= (*tail)->bytes;
        release_image(*tail);
        *tail = NULL;
        r->image_count--;
    }
    im->next = r->images;
    r->images = im;
    r->image_count++;
    r->image_bytes += im->bytes;
    return im;
}
typedef struct {
    Rect source, dest;
} ImageRegion;
static int image_regions(ImageResource *im, Rect r, const cJSON *edges, const char *fit, ImageRegion regions[9]) {
    int n = 0, row, col;
    if (edges) {
        double l = num(arg(edges, 0), 0), t = num(arg(edges, 1), 0), right = num(arg(edges, 2), 0),
               bottom = num(arg(edges, 3), 0), xs = r.w / fmax(1, nearbyint(r.w)), ys = r.h / fmax(1, nearbyint(r.h));
        double sx[4] = {0, l, im->width - right, im->width}, sy[4] = {0, t, im->height - bottom, im->height},
               dx[4] = {0, l * xs, r.w - right * xs, r.w}, dy[4] = {0, t * ys, r.h - bottom * ys, r.h};
        for (row = 0; row < 3; row++)
            for (col = 0; col < 3; col++) {
                Rect a = {sx[col], sy[row], sx[col + 1] - sx[col], sy[row + 1] - sy[row]},
                     b = {dx[col], dy[row], dx[col + 1] - dx[col], dy[row + 1] - dy[row]};
                if (a.w > 0 && a.h > 0 && b.w > 0 && b.h > 0)
                    regions[n++] = (ImageRegion){a, b};
            }
        return n;
    }
    regions[0] = (ImageRegion){{0, 0, im->width, im->height}, {0, 0, r.w, r.h}};
    if (strcmp(fit, "stretch") && r.w > 0 && r.h > 0) {
        double scale =
            !strcmp(fit, "contain") ? fmin(r.w / im->width, r.h / im->height) : fmax(r.w / im->width, r.h / im->height);
        if (!strcmp(fit, "contain")) {
            double w = im->width * scale, h = im->height * scale;
            regions[0].dest = (Rect){(r.w - w) / 2, (r.h - h) / 2, w, h};
        } else {
            double w = r.w / scale, h = r.h / scale;
            regions[0].source = (Rect){(im->width - w) / 2, (im->height - h) / 2, w, h};
        }
    }
    return 1;
}
static Color image_sample(ImageResource *im, ImageRegion *regions, int count, double x, double y) {
    int i;
    for (i = count - 1; i >= 0; i--) {
        Rect d = regions[i].dest, s = regions[i].source;
        if (x >= d.x && x < d.x + d.w && y >= d.y && y < d.y + d.h) {
            int px = bounded_int(s.x + (x - d.x) / d.w * s.w), py = bounded_int(s.y + (y - d.y) / d.h * s.h);
            const unsigned char *p;
            if (px < 0)
                px = 0;
            if (py < 0)
                py = 0;
            if (px >= im->width)
                px = im->width - 1;
            if (py >= im->height)
                py = im->height - 1;
            p = im->pixels + ((size_t)py * im->width + px) * 4;
            return (Color){p[0], p[1], p[2], p[3]};
        }
    }
    return (Color){0, 0, 0, 0};
}
static Color image_half(ImageResource *im, ImageRegion *regions, int count, Rect r, int64_t col, int64_t row, int lower,
                        Color tint) {
    int yi, xi;
    double red = 0, green = 0, blue = 0, alpha = 0;
    for (yi = 0; yi < 2; yi++)
        for (xi = 0; xi < 2; xi++) {
            Color p = image_sample(im, regions, count, (col + .25 + xi * .5) * 8 - r.x,
                                   (row + .125 + yi * .25 + lower * .5) * 16 - r.y);
            red += p.r * p.a;
            green += p.g * p.a;
            blue += p.b * p.a;
            alpha += p.a;
        }
    if (!alpha)
        return (Color){0, 0, 0, 0};
    return (Color){channel(red * tint.r / (alpha * 255)), channel(green * tint.g / (alpha * 255)),
                   channel(blue * tint.b / (alpha * 255)), channel(alpha * tint.a / (4 * 255))};
}
static void draw_image(PTBackend *r, Frame *f, const char *source, Rect rect, Color tint, const char *fit,
                       const cJSON *edges) {
    Box b = bounds(box(rect), f->clip);
    ImageResource *im;
    ImageRegion regions[9];
    int count;
    int64_t x, y;
    if (!tint.a || b.l >= b.r || b.t >= b.b)
        return;
    im = image_resource(r, source);
    if (!im || !im->pixels) {
        Box saved = f->clip;
        f->clip = b;
        draw_text(f, "[image]", rect.x, rect.y, tint);
        f->clip = saved;
        if (im && im->bytes > PT_IMAGE_CACHE_BUDGET)
            release_image(im);
        return;
    }
    count = image_regions(im, rect, edges, fit, regions);
    for (y = b.t; y < b.b; y++)
        for (x = b.l; x < b.r; x++) {
            Color top = image_half(im, regions, count, rect, x, y, 0, tint),
                  bottom = image_half(im, regions, count, rect, x, y, 1, tint), fg, bg;
            Cell *c;
            const char *glyph = "▀";
            char mono[2] = {0, 0};
            if (!top.a && !bottom.a)
                continue;
            c = &f->cells[(size_t)y * f->cols + (size_t)x];
            fg = over(top, c->ink == 2 ? c->fg : c->bg);
            bg = over(bottom, c->bg);
            if (c->width != 1)
                erase(f, (int)x, (int)y);
            if (f->mono) {
                mono[0] = luminance(fg, bg);
                glyph = mono;
            }
            c->text = intern(f, glyph);
            c->fg = fg;
            c->bg = bg;
            c->width = 1;
            c->underline = 0;
            c->ink = 2;
        }
    if (im->bytes > PT_IMAGE_CACHE_BUDGET)
        release_image(im);
}

static int finite_json(const cJSON *j, int depth) {
    const cJSON *v;
    if (depth > 12)
        return 0;
    if (cJSON_IsNumber(j))
        return isfinite(j->valuedouble);
    if (cJSON_IsString(j))
        return strlen(j->valuestring) <= PT_MAX_TEXT;
    if (cJSON_IsArray(j) || cJSON_IsObject(j))
        cJSON_ArrayForEach(v, j) if (!finite_json(v, depth + 1)) return 0;
    return 1;
}
static int finite_commands(const cJSON *commands) {
    const cJSON *c, *v;
    cJSON_ArrayForEach(c, commands) {
        const char *name = str(arg(c, 0), "");
        const cJSON *source = (!strcmp(name, "image") || !strcmp(name, "image_nine")) ? arg(c, 1) : NULL;
        if (!cJSON_IsArray(c))
            return 0;
        /* Compare runs before drawing validation. Reject duplicate style keys
           even when cJSON considers them equal to the committed scalar style. */
        if ((!strcmp(name, "styled_rect") || !strcmp(name, "marker")) && !px_style_members(arg(c, 2)))
            return 0;
        cJSON_ArrayForEach(v, c) {
            if (v == source && cJSON_IsString(v) &&
                !strncmp(v->valuestring, PT_IMAGE_PREFIX, sizeof(PT_IMAGE_PREFIX) - 1)) {
                size_t n = strlen(v->valuestring), encoded, decoded;
                if (n > PT_MAX_IMAGE_SOURCE)
                    return 0;
                encoded = n - (sizeof(PT_IMAGE_PREFIX) - 1);
                decoded = encoded / 4 * 3 + encoded % 4 * 3 / 4;
                /* The final padded quartet can encode one byte above the cap
                   without increasing the URI length. Account for its padding. */
                if (encoded && v->valuestring[n - 1] == '=' && decoded)
                    decoded--;
                if (encoded > 1 && v->valuestring[n - 2] == '=' && decoded)
                    decoded--;
                if (decoded > PT_MAX_IMAGE_BYTES)
                    return 0;
            } else if (!finite_json(v, 2))
                return 0;
        }
    }
    return 1;
}
static int numbers(const cJSON *c, int first, int last) {
    int i;
    for (i = first; i <= last; i++)
        if (!cJSON_IsNumber(arg(c, i)) || !isfinite(arg(c, i)->valuedouble))
            return 0;
    return 1;
}
static int style_valid(const cJSON *s) {
    static const char *const names[] = {"fill",         "fill_end",    "foreground",    "border",
                                        "border_end",   "bevel_light", "bevel_dark",    "highlight",
                                        "inner_border", "glow",        "pattern_color", "shadow"};
    size_t i;
    Color ink;
    if (!px_style_members(s))
        return 0;
    for (i = 0; i < sizeof(names) / sizeof(names[0]); i++) {
        const cJSON *v = item(s, names[i]);
        if (v && !cJSON_IsNull(v) && (!cJSON_IsString(v) || !color(v->valuestring, &ink)))
            return 0;
    }
    return 1;
}
static int optional_rect(const cJSON *value) {
    Rect rect;
    return !value || cJSON_IsNull(value) || (rect_value(value, &rect) && rect.w >= 0 && rect.h >= 0);
}
static int command(PTBackend *r, Frame *f, const cJSON *c) {
    const char *name;
    int n = cJSON_GetArraySize(c);
    Rect rect;
    Color a, b;
    const cJSON *v;
    double radius, width;
    if (!cJSON_IsArray(c) || n < 1 || !cJSON_IsString(arg(c, 0)))
        return 0;
    name = arg(c, 0)->valuestring;
    if (!strcmp(name, "begin")) {
        if (n != 2 || !color(str(arg(c, 1), NULL), &a))
            return 0;
        begin_frame(f, a);
    } else if (!strcmp(name, "clip")) {
        if (n != 2)
            return 0;
        if (cJSON_IsNull(arg(c, 1)))
            f->clip = (Box){0, 0, f->cols, f->rows};
        else {
            if (!rect_value(arg(c, 1), &rect))
                return 0;
            f->clip = bounds(box(rect), (Box){0, 0, f->cols, f->rows});
        }
    } else if (!strcmp(name, "rect")) {
        if (n != 6 || !rect_value(arg(c, 1), &rect) || !color(str(arg(c, 2), NULL), &a) ||
            !color(str(arg(c, 4), NULL), &b) || !cJSON_IsNumber(arg(c, 3)) || !cJSON_IsNumber(arg(c, 5)))
            return 0;
        draw_rect(f, rect, a, *str(arg(c, 2), "") != 0, num(arg(c, 3), 0), b, *str(arg(c, 4), "") != 0,
                  num(arg(c, 5), 1));
    } else if (!strcmp(name, "text")) {
        if (n != 7 || !cJSON_IsString(arg(c, 1)) || !numbers(c, 2, 3) || !color(str(arg(c, 4), NULL), &a) ||
            !cJSON_IsNumber(arg(c, 5)) || !cJSON_IsBool(arg(c, 6)))
            return 0;
        draw_text(f, arg(c, 1)->valuestring, num(arg(c, 2), 0), num(arg(c, 3), 0), a);
    } else if (!strcmp(name, "line")) {
        if (n != 7 || !numbers(c, 1, 4) || !color(str(arg(c, 5), NULL), &a) || !cJSON_IsNumber(arg(c, 6)))
            return 0;
        draw_line(f, num(arg(c, 1), 0), num(arg(c, 2), 0), num(arg(c, 3), 0), num(arg(c, 4), 0), a, num(arg(c, 6), 1));
    } else if (!strcmp(name, "lines") || !strcmp(name, "segments")) {
        const cJSON *points = arg(c, 1), *prior = NULL;
        int independent = !strcmp(name, "segments");
        if (n != 4 || !cJSON_IsArray(points) || !color(str(arg(c, 2), NULL), &a) || !cJSON_IsNumber(arg(c, 3)) ||
            cJSON_GetArraySize(points) > 100000 || (independent && cJSON_GetArraySize(points) % 2))
            return 0;
        cJSON_ArrayForEach(v,
                           points) if (!cJSON_IsArray(v) || cJSON_GetArraySize(v) != 2 || !numbers(v, 0, 1)) return 0;
        cJSON_ArrayForEach(v, points) {
            if (prior)
                draw_line(f, num(arg(prior, 0), 0), num(arg(prior, 1), 0), num(arg(v, 0), 0), num(arg(v, 1), 0), a,
                          num(arg(c, 3), 1));
            prior = independent && prior ? NULL : v;
        }
    } else if (!strcmp(name, "gradient_rect")) {
        const char *axis = str(arg(c, 4), "");
        if (n != 7 || !rect_value(arg(c, 1), &rect) || !color(str(arg(c, 2), NULL), &a) ||
            !color(str(arg(c, 3), NULL), &b) || !numbers(c, 5, 6) ||
            (strcmp(axis, "vertical") && strcmp(axis, "horizontal")))
            return 0;
        gradient_rect(f, rect, a, b, !strcmp(axis, "horizontal"), num(arg(c, 5), 0), num(arg(c, 6), 0));
    } else if (!strcmp(name, "styled_rect")) {
        if ((n != 3 && n != 4) || !rect_value(arg(c, 1), &rect) || !style_valid(arg(c, 2)) || !optional_rect(arg(c, 3)))
            return 0;
        styled_rect(f, rect, arg(c, 2));
    } else if (!strcmp(name, "marker")) {
        const char *shape = str(arg(c, 3), "");
        if ((n != 5 && n != 6) || !rect_value(arg(c, 1), &rect) || !style_valid(arg(c, 2)) ||
            (strcmp(shape, "square") && strcmp(shape, "circle")) || !cJSON_IsBool(arg(c, 4)) ||
            !optional_rect(arg(c, 5)))
            return 0;
        marker(f, rect, arg(c, 2), shape, cJSON_IsTrue(arg(c, 4)));
    } else if (!strcmp(name, "focus_ring")) {
        if (n != 4 || !rect_value(arg(c, 1), &rect) || !color(str(arg(c, 2), NULL), &a) || !cJSON_IsNumber(arg(c, 3)))
            return 0;
        focus_ring(f, rect, a, num(arg(c, 3), 0));
    } else if (!strcmp(name, "caret")) {
        if (n != 5 || !numbers(c, 1, 3) || !color(str(arg(c, 4), NULL), &a))
            return 0;
        if (num(arg(c, 3), 0) > 0)
            underline(f, integer(ceil(num(arg(c, 1), 0) / 8 - .5)), integer(ceil(num(arg(c, 2), 0) / 16 - .5)), &a);
    } else if (!strcmp(name, "icon")) {
        const char *glyph = icon_glyph(str(arg(c, 1), ""));
        if (n != 6 || !glyph || !numbers(c, 2, 4) || !color(str(arg(c, 5), NULL), &a) || num(arg(c, 4), 0) < 0)
            return 0;
        draw_icon(f, glyph, num(arg(c, 2), 0), num(arg(c, 3), 0), num(arg(c, 4), 0), a);
    } else if (!strcmp(name, "image") || !strcmp(name, "image_nine")) {
        int nine = !strcmp(name, "image_nine");
        const char *fit = nine ? "stretch" : str(arg(c, 4), "stretch");
        if (n != 5 || !cJSON_IsString(arg(c, 1)) || !rect_value(arg(c, 2), &rect) ||
            !cJSON_IsString(arg(c, nine ? 4 : 3)) || (!nine && !cJSON_IsString(arg(c, 4))) ||
            !color(str(arg(c, nine ? 4 : 3), "#ffffff"), &a))
            return 0;
        if (nine) {
            if (!cJSON_IsArray(arg(c, 3)) || cJSON_GetArraySize(arg(c, 3)) != 4 || !numbers(arg(c, 3), 0, 3))
                return 0;
        } else if (strcmp(fit, "stretch") && strcmp(fit, "contain") && strcmp(fit, "cover"))
            return 0;
        draw_image(r, f, arg(c, 1)->valuestring, rect, a, fit, nine ? arg(c, 3) : NULL);
    } else
        return 0;
    radius = width = 0;
    (void)radius;
    (void)width;
    return !f->failed;
}
static size_t json_heap_bytes(const cJSON *value) {
    const cJSON *child;
    size_t bytes = value ? sizeof(cJSON) : 0;
    if (!value)
        return 0;
    if (value->valuestring)
        bytes += strlen(value->valuestring) + 1;
    if (value->string)
        bytes += strlen(value->string) + 1;
    cJSON_ArrayForEach(child, value) {
        bytes += json_heap_bytes(child);
        if (bytes > PT_SCENE_BUDGET)
            return PT_SCENE_BUDGET + 1;
    }
    return bytes;
}
static size_t segment_capacity(size_t count) {
    size_t capacity = 8;
    while (capacity < count * 2 + 1)
        capacity *= 2;
    return capacity;
}
static PTSegmentSlot *segment_slot(PTSegmentSlot *table, size_t capacity, const char *id) {
    size_t hash = (size_t)2166136261u, at;
    const unsigned char *s = (const unsigned char *)id;
    if (!table || !capacity)
        return NULL;
    while (*s) {
        hash ^= *s++;
        hash *= (size_t)16777619u;
    }
    at = hash & (capacity - 1);
    while (table[at].segment && strcmp(table[at].segment->id, id))
        at = (at + 1) & (capacity - 1);
    return table + at;
}
static void segment_free(PTSegment *s) {
    if (s) {
        free(s->id);
        cJSON_Delete(s->commands);
        free(s);
    }
}
static void segments_clear(PTBackend *r) {
    size_t i;
    for (i = 0; i < r->segment_count; i++)
        segment_free(r->segments[i]);
    free(r->segments);
    free(r->segment_table);
    r->segments = NULL;
    r->segment_table = NULL;
    r->segment_count = r->segment_capacity = 0;
    r->segmented = 0;
}
static int render_commands(PTBackend *r, Frame *f, const cJSON *commands, int segmented, char *error, int error_size) {
    const cJSON *c;
    size_t index = 0;
    cJSON_ArrayForEach(c, commands) {
        if ((segmented && !strcmp(str(arg(c, 0), ""), "begin")) || !command(r, f, c)) {
            char text[200];
            snprintf(text, sizeof(text), "Invalid terminal command %zu (%s), geometry, color, or allocation budget",
                     index, str(arg(c, 0), "?"));
            fail(error, error_size, text);
            return 0;
        }
        index++;
    }
    return 1;
}
/* Frames are immutable after composition. Retaining one makes uncapped native
   presents O(1), while queued ANSI keeps its own reference to its diff baseline. */
static Frame *render(PTBackend *r, const cJSON *commands, PTSegment **segments, size_t count, Color background,
                     char *error, int error_size) {
    Frame *f = frame_create(r->cols, r->rows, r->color_mode == 3);
    size_t i;
    if (!f) {
        fail(error, error_size, "Cannot allocate terminal cells");
        return NULL;
    }
    begin_frame(f, background);
    if (commands) {
        if (!render_commands(r, f, commands, 0, error, error_size))
            goto failed;
    } else
        for (i = 0; i < count; i++) {
            /* Segments carry their own inherited clip commands; a final clip in
           one control must never leak into the following control's segment. */
            f->clip = (Box){0, 0, f->cols, f->rows};
            if (!render_commands(r, f, segments[i]->commands, 1, error, error_size))
                goto failed;
        }
    return f;
failed:
    frame_release(f);
    return NULL;
}
static int frame_current(PTBackend *r) {
    return r->current && !r->images_dirty && r->current->cols == r->cols && r->current->rows == r->rows &&
           r->current->mono == (r->color_mode == 3);
}
static void composed(PTBackend *r, Frame *f, size_t replayed, size_t tested) {
    frame_release(r->current);
    r->current = f;
    r->images_dirty = 0;
    r->cell_frames_composed++;
    r->commands_replayed += replayed;
    r->last_commands_replayed = replayed;
    r->segments_tested += tested;
    r->last_segments_tested = tested;
}
int pt_commit(PTBackend *r, const cJSON *commands, char *error, int error_size) {
    cJSON *copy;
    Frame *f;
    size_t count, bytes;
    if (!r) {
        fail(error, error_size, "Terminal is closed");
        return 0;
    }
    count = (size_t)cJSON_GetArraySize(commands);
    if (!cJSON_IsArray(commands) || count > PT_MAX_COMMANDS || !finite_commands(commands)) {
        fail(error, error_size, "Invalid or oversized retained command list");
        return 0;
    }
    bytes = json_heap_bytes(commands);
    if (bytes > PT_SCENE_BUDGET) {
        fail(error, error_size, "Retained scene exceeds 64 MiB memory budget");
        return 0;
    }
    if (r->commands && frame_current(r) && cJSON_Compare(commands, r->commands, 1)) {
        r->last_commands_replayed = r->last_segments_tested = 0;
    } else {
        copy = cJSON_Duplicate(commands, 1);
        if (!copy) {
            fail(error, error_size, "Cannot retain command list");
            return 0;
        }
        f = render(r, copy, NULL, 0, (Color){0, 0, 0, 255}, error, error_size);
        if (!f) {
            cJSON_Delete(copy);
            return 0;
        }
        cJSON_Delete(r->commands);
        r->commands = copy;
        segments_clear(r);
        r->command_count = count;
        r->retained_scene_bytes = bytes;
        composed(r, f, count, 0);
    }
    r->commands_touched += count;
    r->last_commands_touched = count;
    r->pending_work = 1;
    return 1;
}
int pt_patch(PTBackend *r, const cJSON *patch, char *error, int error_size) {
    const cJSON *upsert = item(patch, "upsert"), *remove = item(patch, "remove"), *order = item(patch, "order"),
                *background = item(patch, "background"), *entry, *c;
    PTSegment **next = NULL, **created = NULL, **ordered = NULL;
    PTSegmentSlot *incoming = NULL, *table = NULL, *slot, *old_slot;
    unsigned char *removed = NULL, *seen = NULL;
    Frame *f = NULL;
    size_t updates = (size_t)cJSON_GetArraySize(upsert), count = 0, made = 0, i, total = 0, retained = 0, touched = 0,
           removed_count = 0, unchanged = 0, capacity, incoming_capacity;
    int changed;
    Color bg;
    if (!r) {
        fail(error, error_size, "Terminal is closed");
        return 0;
    }
    bg = r->segmented ? r->scene_background : (Color){0, 0, 0, 255};
    if (!cJSON_IsObject(patch) || (upsert && !cJSON_IsArray(upsert)) || (remove && !cJSON_IsArray(remove)) ||
        (order && !cJSON_IsArray(order)) || updates > PT_MAX_COMMANDS ||
        cJSON_GetArraySize(remove) > (int)PT_MAX_COMMANDS || cJSON_GetArraySize(order) > (int)PT_MAX_COMMANDS ||
        (background && !color(str(background, NULL), &bg)))
        goto invalid;
    capacity = segment_capacity(r->segment_count + updates);
    incoming_capacity = segment_capacity(updates);
    next = (PTSegment **)calloc(r->segment_count + updates + 1, sizeof(*next));
    created = (PTSegment **)calloc(updates + 1, sizeof(*created));
    table = (PTSegmentSlot *)calloc(capacity, sizeof(*table));
    incoming = (PTSegmentSlot *)calloc(incoming_capacity, sizeof(*incoming));
    removed = (unsigned char *)calloc(r->segment_count + 1, 1);
    if (!next || !created || !table || !incoming || !removed)
        goto allocation;
    cJSON_ArrayForEach(entry, remove) {
        if (!cJSON_IsString(entry) || !*entry->valuestring || strlen(entry->valuestring) > 256)
            goto invalid;
        slot = segment_slot(r->segment_table, r->segment_capacity, entry->valuestring);
        if (!slot || !slot->segment)
            continue;
        if (removed[slot->position])
            goto invalid;
        removed[slot->position] = 1;
        removed_count++;
    }
    cJSON_ArrayForEach(entry, upsert) {
        const cJSON *id = item(entry, "id"), *list = item(entry, "commands");
        PTSegment *s;
        Rect rect;
        size_t bytes, n;
        if (!cJSON_IsObject(entry) || !cJSON_IsString(id) || !*id->valuestring || strlen(id->valuestring) > 256 ||
            !rect_value(item(entry, "bounds"), &rect) || rect.w < 0 || rect.h < 0 || !cJSON_IsArray(list) ||
            !finite_commands(list))
            goto invalid;
        slot = segment_slot(incoming, incoming_capacity, id->valuestring);
        if (slot->segment)
            goto invalid;
        old_slot = segment_slot(r->segment_table, r->segment_capacity, id->valuestring);
        if (old_slot && old_slot->segment && removed[old_slot->position])
            goto invalid;
        n = (size_t)cJSON_GetArraySize(list);
        touched += n;
        if (touched > PT_MAX_COMMANDS)
            goto invalid;
        /* Reject begin even in an otherwise invisible segment: background and
           clip ownership belong to the retained scene, not to individual IDs. */
        cJSON_ArrayForEach(c, list) if (!strcmp(str(arg(c, 0), ""), "begin")) goto invalid;
        if (old_slot && old_slot->segment && cJSON_Compare(list, old_slot->segment->commands, 1)) {
            slot->segment = old_slot->segment;
            unchanged++;
            continue;
        }
        bytes = json_heap_bytes(list) + sizeof(*s) + strlen(id->valuestring) + 1;
        if (bytes > PT_SCENE_BUDGET)
            goto budget;
        s = (PTSegment *)calloc(1, sizeof(*s));
        if (!s)
            goto allocation;
        created[made++] = s;
        s->id = copy_string(id->valuestring);
        s->commands = cJSON_Duplicate(list, 1);
        s->count = n;
        s->bytes = bytes;
        if (!s->id || !s->commands)
            goto allocation;
        slot->segment = s;
    }
    for (i = 0; i < r->segment_count; i++) {
        PTSegment *s = r->segments[i];
        if (removed[i])
            continue;
        slot = segment_slot(incoming, incoming_capacity, s->id);
        if (slot->segment)
            s = slot->segment;
        next[count++] = s;
    }
    for (i = 0; i < made; i++) {
        slot = segment_slot(r->segment_table, r->segment_capacity, created[i]->id);
        if (!slot || !slot->segment)
            next[count++] = created[i];
    }
    if (count > PT_MAX_COMMANDS)
        goto invalid;
    for (i = 0; i < count; i++) {
        slot = segment_slot(table, capacity, next[i]->id);
        slot->segment = next[i];
        slot->position = i;
        total += next[i]->count;
        retained += next[i]->bytes;
        if (total > PT_MAX_COMMANDS)
            goto invalid;
        if (retained > PT_SCENE_BUDGET)
            goto budget;
    }
    if (order) {
        if ((size_t)cJSON_GetArraySize(order) != count)
            goto invalid;
        ordered = (PTSegment **)calloc(count + 1, sizeof(*ordered));
        seen = (unsigned char *)calloc(count + 1, 1);
        if (!ordered || !seen)
            goto allocation;
        i = 0;
        cJSON_ArrayForEach(entry, order) {
            if (!cJSON_IsString(entry))
                goto invalid;
            slot = segment_slot(table, capacity, entry->valuestring);
            if (!slot || !slot->segment || seen[slot->position])
                goto invalid;
            seen[slot->position] = 1;
            ordered[i++] = slot->segment;
        }
        free(next);
        next = ordered;
        ordered = NULL;
        for (i = 0; i < count; i++)
            segment_slot(table, capacity, next[i]->id)->position = i;
    }
    changed = !r->segmented || !frame_current(r) || made || removed_count || count != r->segment_count ||
              !same_color(bg, r->scene_background) || bg.a != r->scene_background.a;
    if (!changed)
        for (i = 0; i < count; i++)
            if (next[i] != r->segments[i]) {
                changed = 1;
                break;
            }
    if (changed) {
        f = render(r, NULL, next, count, bg, error, error_size);
        if (!f)
            goto failed;
    }
    /* Validation and composition succeeded. Only now retire replaced entries;
       unchanged JSON and the previous ANSI baseline remain owned and intact. */
    for (i = 0; i < r->segment_count; i++) {
        slot = segment_slot(table, capacity, r->segments[i]->id);
        if (!slot->segment || slot->segment != r->segments[i])
            segment_free(r->segments[i]);
    }
    free(r->segments);
    free(r->segment_table);
    r->segments = next;
    r->segment_table = table;
    next = NULL;
    table = NULL;
    r->segment_count = count;
    r->segment_capacity = capacity;
    cJSON_Delete(r->commands);
    r->commands = NULL;
    r->segmented = 1;
    r->scene_background = bg;
    r->command_count = total;
    r->retained_scene_bytes = retained;
    r->last_commands_replayed = r->last_segments_tested = 0;
    if (f)
        composed(r, f, total, count);
    r->commands_touched += touched;
    r->last_commands_touched = touched;
    r->pending_work = 1;
    r->segments_updated += made;
    r->segments_removed += removed_count;
    r->scene_patch_updates++;
    r->unchanged_segments += unchanged;
    free(created);
    free(incoming);
    free(removed);
    free(seen);
    return 1;
invalid:
    fail(error, error_size, "Invalid retained terminal segment patch");
    goto failed;
budget:
    fail(error, error_size, "Retained scene exceeds 64 MiB memory budget");
    goto failed;
allocation:
    fail(error, error_size, "Cannot allocate retained terminal segment patch");
failed:
    for (i = 0; i < made; i++)
        segment_free(created[i]);
    free(created);
    free(incoming);
    free(removed);
    free(seen);
    free(next);
    free(table);
    free(ordered);
    return 0;
}
int pt_draw(PTBackend *r, char *error, int error_size) {
    Frame *f;
    if (!r || (!r->commands && !r->segmented)) {
        fail(error, error_size, "Terminal has no retained frame");
        return 0;
    }
    if (!r->pending_work)
        r->last_commands_touched = r->last_commands_replayed = r->last_segments_tested = 0;
    r->pending_work = 0;
    if (frame_current(r)) {
        r->cell_cache_hits++;
        return 1;
    }
    f = render(r, r->commands, r->segments, r->segment_count,
               r->segmented ? r->scene_background : (Color){0, 0, 0, 255}, error, error_size);
    if (!f)
        return 0;
    composed(r, f, r->command_count, r->segment_count);
    return 1;
}
void pt_reset_stats(PTBackend *r) {
    if (!r)
        return;
    r->commands_touched = r->commands_replayed = r->last_commands_touched = r->last_commands_replayed = 0;
    r->segments_updated = r->segments_removed = r->segments_tested = r->last_segments_tested = 0;
    r->scene_patch_updates = r->unchanged_segments = r->cell_frames_composed = r->cell_cache_hits = 0;
    r->ansi_frames_encoded = r->ansi_bytes_encoded = r->terminal_bytes_written = 0;
    r->pending_work = 0;
}
void pt_last_work(PTBackend *r, uint64_t *touched, uint64_t *replayed) {
    if (touched)
        *touched = r ? r->last_commands_touched : 0;
    if (replayed)
        *replayed = r ? r->last_commands_replayed : 0;
}
uint64_t pt_last_segment_tests(PTBackend *r) {
    return r ? r->last_segments_tested : 0;
}
static int same_cell(Cell a, Cell b, int mono) {
    return a.width == b.width && a.underline == b.underline && !strcmp(a.text, b.text) &&
           (mono || (same_color(a.fg, b.fg) && same_color(a.bg, b.bg)));
}
static int palette_index(Color c, int count) {
    static const unsigned char base[16][3] = {{0, 0, 0},       {128, 0, 0},   {0, 128, 0},   {128, 128, 0},
                                              {0, 0, 128},     {128, 0, 128}, {0, 128, 128}, {192, 192, 192},
                                              {128, 128, 128}, {255, 0, 0},   {0, 255, 0},   {255, 255, 0},
                                              {0, 0, 255},     {255, 0, 255}, {0, 255, 255}, {255, 255, 255}};
    static const int levels[] = {0, 95, 135, 175, 215, 255};
    int i, best = 0, distance = INT32_MAX;
    for (i = 0; i < count; i++) {
        int red, green, blue, d;
        if (i < 16) {
            red = base[i][0];
            green = base[i][1];
            blue = base[i][2];
        } else if (i < 232) {
            int p = i - 16;
            red = levels[p / 36];
            green = levels[p / 6 % 6];
            blue = levels[p % 6];
        } else
            red = green = blue = 8 + (i - 232) * 10;
        d = (red - c.r) * (red - c.r) + (green - c.g) * (green - c.g) + (blue - c.b) * (blue - c.b);
        if (d < distance) {
            distance = d;
            best = i;
        }
    }
    return best;
}
static void sgr(Buffer *out, Color fg, Color bg, int mode) {
    char temp[100];
    if (mode == 0)
        snprintf(temp, sizeof(temp), "\033[38;2;%d;%d;%d;48;2;%d;%d;%dm", fg.r, fg.g, fg.b, bg.r, bg.g, bg.b);
    else if (mode == 1)
        snprintf(temp, sizeof(temp), "\033[38;5;%d;48;5;%dm", palette_index(fg, 256), palette_index(bg, 256));
    else if (mode == 2) {
        int a = palette_index(fg, 16), b = palette_index(bg, 16);
        snprintf(temp, sizeof(temp), "\033[%d;%dm", a < 8 ? 30 + a : 90 + a - 8, b < 8 ? 40 + b : 100 + b - 8);
    } else
        return;
    emit(out, temp);
}
static Buffer ansi_frame(PTBackend *r, int diff) {
    Buffer out = {0};
    Frame *f = r->current, *old = diff ? r->previous : NULL;
    unsigned char *dirty;
    int row, x, changed = 0, style = 0, under = 0;
    Color foreground = {0}, background = {0};
    if (!f) {
        out.failed = 1;
        return out;
    }
    if (f == old)
        return out;
    if (old && (old->cols != f->cols || old->rows != f->rows))
        old = NULL;
    dirty = (unsigned char *)malloc((size_t)f->cols);
    if (!dirty) {
        out.failed = 1;
        return out;
    }
    for (row = 0; row < f->rows; row++) {
        Cell *cells = f->cells + (size_t)row * f->cols, *prior = old ? old->cells + (size_t)row * f->cols : NULL;
        int again;
        for (x = 0; x < f->cols; x++)
            dirty[x] = (unsigned char)(!prior || !same_cell(cells[x], prior[x], r->color_mode == 3));
        do {
            again = 0;
            for (x = 0; x < f->cols; x++)
                if (dirty[x]) {
                    int j;
                    for (j = 0; j < (prior ? 2 : 1); j++) {
                        Cell c = j ? prior[x] : cells[x];
                        int paired = c.width == 0 ? x - 1 : x + 1;
                        if (c.width != 1 && paired >= 0 && paired < f->cols && !dirty[paired]) {
                            dirty[paired] = 1;
                            again = 1;
                        }
                    }
                }
        } while (again);
        x = 0;
        while (x < f->cols) {
            char cursor[40];
            if (!dirty[x]) {
                x++;
                continue;
            }
            if (!changed) {
                emit(&out, "\033[?2026h");
                changed = 1;
            }
            snprintf(cursor, sizeof(cursor), "\033[%d;%dH", row + 1, x + 1);
            emit(&out, cursor);
            while (x < f->cols && dirty[x]) {
                Cell c = cells[x++];
                if (!c.width)
                    continue;
                if (!style || !same_color(c.fg, foreground) || !same_color(c.bg, background)) {
                    sgr(&out, c.fg, c.bg, r->color_mode);
                    foreground = c.fg;
                    background = c.bg;
                    style = 1;
                }
                if (c.underline != under) {
                    emit(&out, c.underline ? "\033[4m" : "\033[24m");
                    under = c.underline;
                }
                emit(&out, c.text);
            }
        }
    }
    if (changed)
        emit(&out, "\033[0m\033[?2026l");
    append(&out, "", 0);
    free(dirty);
    return out;
}

static cJSON *event(PTBackend *r, const char *kind) {
    cJSON *e;
    if (r->input_failed)
        return NULL;
    if (cJSON_GetArraySize(r->events) >= 4095) {
        r->input_failed = 1;
        cJSON_Delete(r->events);
        r->events = cJSON_CreateArray();
        e = cJSON_CreateObject();
        cJSON_AddStringToObject(e, "kind", "error");
        cJSON_AddStringToObject(e, "text", "Native terminal input queue exceeded 4096 events");
        cJSON_AddItemToArray(r->events, e);
        return NULL;
    }
    e = cJSON_CreateObject();
    if (e) {
        cJSON_AddStringToObject(e, "kind", kind);
        cJSON_AddItemToArray(r->events, e);
    }
    return e;
}
static void key_event(PTBackend *r, const char *key, int shift, int ctrl, int kind) {
    cJSON *e;
    if (ctrl && !strcmp(key, "q") && kind != 3) {
        event(r, "close");
        return;
    }
    e = event(r, kind == 3 ? "key_up" : "key_down");
    if (e) {
        cJSON_AddStringToObject(e, "key", key);
        cJSON_AddBoolToObject(e, "shift", shift);
        cJSON_AddBoolToObject(e, "ctrl", ctrl);
    }
    if (!kind) {
        e = event(r, "key_up");
        if (e) {
            cJSON_AddStringToObject(e, "key", key);
            cJSON_AddBoolToObject(e, "shift", shift);
            cJSON_AddBoolToObject(e, "ctrl", ctrl);
        }
    }
}
static void io_error(PTBackend *r, const char *text) {
    cJSON *e;
    if (r->io_failed)
        return;
    e = event(r, "error");
    if (e)
        cJSON_AddStringToObject(e, "text", text);
    r->io_failed = 1;
}
static void text_event(PTBackend *r, const char *text, int paste) {
    cJSON *e = event(r, "text");
    if (e) {
        cJSON_AddStringToObject(e, "text", text);
        if (paste)
            cJSON_AddBoolToObject(e, "paste", 1);
    }
}
static void paste_event(PTBackend *r) {
    /* Reserve the complete event envelope, including the JSON string's quotes.
       Raw JSON preserves embedded NULs that cJSON strings cannot represent. */
    const size_t content_budget = PX_MAX_PAYLOAD - (sizeof("{\"kind\":\"text\",\"text\":\"\",\"paste\":true}") - 1);
    const char *at = r->paste.data ? r->paste.data : "", *end = at + r->paste.size;
    Buffer normalized = {0};
    cJSON *value, *e;
    size_t content_size = 0;
    int oversized = 0;
    if (r->paste.failed) {
        resource_error(r, "Cannot allocate terminal paste");
        return;
    }
    emit(&normalized, "\"");
    while (at < end && !normalized.failed) {
        uint32_t cp;
        char encoded[12];
        size_t size;
        if (!*at) {
            cp = 0;
            at++;
        } else
            cp = decode(&at);
        if (cp == '\r') {
            cp = '\n';
            if (at < end && *at == '\n')
                at++;
        }
        if (cp == '\b' || cp == '\f' || cp == '\n' || cp == '\t' || cp == '"' || cp == '\\') {
            encoded[0] = '\\';
            encoded[1] = cp == '\b' ? 'b' : cp == '\f' ? 'f' : cp == '\n' ? 'n' : cp == '\t' ? 't' : (char)cp;
            size = 2;
        } else if (cp < 32) {
            snprintf(encoded, sizeof(encoded), "\\u%04x", (unsigned)cp);
            size = 6;
        } else
            size = (size_t)encode(cp, encoded);
        if (size > content_budget - content_size) {
            oversized = 1;
            break;
        }
        append(&normalized, encoded, size);
        content_size += size;
    }
    emit(&normalized, "\"");
    if (oversized)
        resource_error(r, "Terminal paste exceeds the 16 MiB event budget after JSON escaping");
    else if (normalized.failed || !(value = cJSON_CreateRaw(normalized.data)))
        resource_error(r, "Cannot allocate terminal paste");
    else {
        e = event(r, "text");
        if (e) {
            cJSON_AddItemToObject(e, "text", value);
            cJSON_AddBoolToObject(e, "paste", 1);
        } else
            cJSON_Delete(value);
    }
    free(normalized.data);
}
static const char *key_name(uint32_t code) {
    switch (code) {
    case 8:
    case 127:
        return "Backspace";
    case 9:
        return "Tab";
    case 10:
    case 13:
        return "Enter";
    case 27:
        return "Escape";
    case 32:
        return "Space";
    case 57348:
        return "Insert";
    case 57349:
        return "Delete";
    case 57350:
        return "ArrowLeft";
    case 57351:
        return "ArrowRight";
    case 57352:
        return "ArrowUp";
    case 57353:
        return "ArrowDown";
    case 57354:
        return "PageUp";
    case 57355:
        return "PageDown";
    case 57356:
        return "Home";
    case 57357:
        return "End";
    default:
        return NULL;
    }
}
static void plain_codepoint(PTBackend *r, uint32_t cp) {
    char text[16] = {0}, key[16] = {0};
    const char *named = key_name(cp);
    const PTCase *c = case_of(cp);
    if (cp == 0)
        return;
    if (cp < 27 && !named) {
        key[0] = (char)(cp + 96);
        key_event(r, key, 0, 1, 0);
        return;
    }
    if (cp < 32 || cp == 127) {
        if (named)
            key_event(r, named, 0, 0, 0);
        return;
    }
    text[encode(cp, text)] = 0;
    key_event(r, named ? named : c ? c->lower : text, c ? c->is_upper : 0, 0, 0);
    text_event(r, text, 0);
}
static void consume_input(PTBackend *r, size_t n) {
    memmove(r->input, r->input + n, r->input_size - n);
    r->input_size -= n;
    if (r->input)
        r->input[r->input_size] = 0;
}
static void input_append(PTBackend *r, const unsigned char *data, size_t size) {
    size_t cap;
    unsigned char *next;
    if (size > PT_MAX_TEXT * 2 - r->input_size) {
        r->input_size = 0;
        resource_error(r, "Terminal input exceeded its bounded buffer");
        return;
    }
    if (!r->input_size)
        r->escape_at = now_seconds();
    if (r->input_size + size + 1 > r->input_cap) {
        cap = r->input_size + size + 1;
        next = (unsigned char *)realloc(r->input, cap);
        if (!next) {
            resource_error(r, "Could not allocate terminal input");
            return;
        }
        r->input = next;
        r->input_cap = cap;
    }
    memcpy(r->input + r->input_size, data, size);
    r->input_size += size;
    r->input[r->input_size] = 0;
}
static const unsigned char *find_bytes(const unsigned char *data, size_t size, const char *needle, size_t length) {
    size_t i;
    for (i = 0; i + length <= size; i++)
        if (!memcmp(data + i, needle, length))
            return data + i;
    return NULL;
}
static const char *arrow_key(char final) {
    switch (final) {
    case 'A':
        return "ArrowUp";
    case 'B':
        return "ArrowDown";
    case 'C':
        return "ArrowRight";
    case 'D':
        return "ArrowLeft";
    case 'H':
        return "Home";
    case 'F':
        return "End";
    case 'P':
        return "F1";
    case 'Q':
        return "F2";
    case 'R':
        return "F3";
    case 'S':
        return "F4";
    default:
        return NULL;
    }
}
static const char *tilde_key(int code) {
    switch (code) {
    case 1:
    case 7:
        return "Home";
    case 2:
        return "Insert";
    case 3:
        return "Delete";
    case 4:
    case 8:
        return "End";
    case 5:
        return "PageUp";
    case 6:
        return "PageDown";
    case 11:
        return "F1";
    case 12:
        return "F2";
    case 13:
        return "F3";
    case 14:
        return "F4";
    case 15:
        return "F5";
    case 17:
        return "F6";
    case 18:
        return "F7";
    case 19:
        return "F8";
    case 20:
        return "F9";
    case 21:
        return "F10";
    case 23:
        return "F11";
    case 24:
        return "F12";
    default:
        return NULL;
    }
}
static void sequence(PTBackend *r, const unsigned char *bytes, size_t size) {
    char seq[1100], final, *parts[4] = {0}, *p;
    int count = 0, mods = 0, kind = 0;
    const char *key;
    unsigned code = 0, x = 0, y = 0;
    int consumed = 0;
    if (size >= sizeof(seq))
        return;
    memcpy(seq, bytes, size);
    seq[size] = 0;
    final = seq[size - 1];
    if (sscanf(seq, "\033[<%u;%u;%u%n", &code, &x, &y, &consumed) == 3 && consumed == (int)size - 1 &&
        (final == 'M' || final == 'm')) {
        cJSON *e;
        const char *type;
        r->pointer_x = ((double)x - .5) * 8;
        r->pointer_y = ((double)y - .5) * 16;
        type = code & 64 ? "wheel" : code & 32 ? "pointer_move" : final == 'm' ? "pointer_up" : "pointer_down";
        e = event(r, type);
        if (e) {
            cJSON_AddNumberToObject(e, "x", r->pointer_x);
            cJSON_AddNumberToObject(e, "y", r->pointer_y);
            cJSON_AddBoolToObject(e, "shift", (code & (code & 64 ? 6 : 4)) != 0);
            cJSON_AddBoolToObject(e, "ctrl", (code & 16) != 0);
            if (code & 64)
                cJSON_AddNumberToObject(e, "delta", code & 1 ? 1 : -1);
            else
                cJSON_AddNumberToObject(e, "button", (code & 3) < 3 ? (code & 3) + 1 : 1);
        }
        return;
    }
    if (!strcmp(seq, "\033[I")) {
        event(r, "repaint");
        return;
    }
    if (!strcmp(seq, "\033[O")) {
        event(r, "blur");
        return;
    }
    if (size >= 4 && seq[1] == '[' && seq[2] == '?' && final == 'u' && r->active && !r->keyboard_enabled) {
        emit(&r->output, "\033[>3u");
        r->keyboard_enabled = 1;
    }
    if (seq[1] == '_' || seq[1] == ']' || seq[1] == 'P' || seq[2] == '?' || final == 't' || final == 'c' ||
        (size >= 2 && seq[size - 2] == '$'))
        return;
    if (seq[1] == 'O') {
        key = arrow_key(final);
        key_event(r, key ? key : "Escape", 0, 0, 0);
        return;
    }
    if (final == 'Z') {
        key_event(r, "Tab", 1, 0, 0);
        return;
    }
    seq[size - 1] = 0;
    p = seq + 2;
    parts[count++] = p;
    for (; *p && count < 4; p++)
        if (*p == ';') {
            *p = 0;
            parts[count++] = p + 1;
        }
    if (count > 1) {
        char *colon = strchr(parts[1], ':');
        mods = atoi(parts[1]) - 1;
        if (mods < 0)
            mods = 0;
        if (colon)
            kind = atoi(colon + 1);
    }
    if (final == 'u') {
        char text[8] = {0}, small[40] = {0};
        char *end = NULL;
        unsigned long value = strtoul(parts[0], &end, 10);
        if (value > 0x10ffff || (value >= 0xd800 && value <= 0xdfff) || end == parts[0])
            return;
        code = (uint32_t)value;
        key = key_name(code);
        if (code >= 57364 && code < 57399) {
            snprintf(small, sizeof(small), "F%u", code - 57364 + 1);
            key = small;
        }
        if (!key) {
            const PTCase *c = case_of(code);
            if (c)
                key = c->lower;
            else {
                small[encode(code, small)] = 0;
                key = small;
            }
        }
        key_event(r, key, mods & 1, (mods & 12) != 0, kind);
        if (kind != 3 && !(mods & 14)) {
            if (count > 2 && *parts[2]) {
                Buffer t = {0};
                char *q = parts[2];
                while (*q) {
                    unsigned long cp = strtoul(q, &end, 10);
                    int n;
                    if (end == q || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))
                        break;
                    n = encode((uint32_t)cp, text);
                    append(&t, text, (size_t)n);
                    q = *end == ':' ? end + 1 : end;
                    if (!*q)
                        break;
                }
                if (t.size)
                    text_event(r, t.data, 0);
                free(t.data);
            } else if ((code >= 32 && code < 57344) || code > 63743) {
                char *shifted = strchr(parts[0], ':');
                if ((mods & 1) && shifted && shifted[1])
                    code = (uint32_t)strtoul(shifted + 1, NULL, 10);
                if (code <= 0x10ffff) {
                    const PTCase *c = case_of(code);
                    text[encode(code, text)] = 0;
                    text_event(r, (mods & 1) && c ? c->upper : text, 0);
                }
            }
        }
        return;
    }
    key = arrow_key(final);
    if (key) {
        key_event(r, key, mods & 1, (mods & 12) != 0, kind);
        return;
    }
    if (final == '~') {
        int value = atoi(parts[0]);
        if (value == 27 && count == 3) {
            char replacement[80];
            int n = snprintf(replacement, sizeof(replacement), "\033[%s;%su", parts[2], parts[1]);
            if (n > 0 && (size_t)n < sizeof(replacement))
                sequence(r, (unsigned char *)replacement, (size_t)n);
            return;
        }
        key = tilde_key(value);
        if (key)
            key_event(r, key, mods & 1, (mods & 12) != 0, kind);
    }
}
static void parse_input(PTBackend *r, int flush) {
    unsigned budget = 0;
    while (r->input_size && !r->input_failed && budget++ < 65536) {
        size_t n = 0;
        if (r->in_paste) {
            const unsigned char *end = find_bytes(r->input, r->input_size, "\033[201~", 6);
            size_t take = end                 ? (size_t)(end - r->input)
                          : r->input_size > 5 ? r->input_size - 5
                                              : 0,
                   room = PT_MAX_TEXT - r->paste.size;
            if (take > room) {
                r->paste_overflow = 1;
                append(&r->paste, r->input, room);
            } else
                append(&r->paste, r->input, take);
            consume_input(r, take);
            if (!end)
                break;
            consume_input(r, 6);
            if (r->paste_overflow)
                resource_error(r, "Terminal paste exceeds 8 MiB");
            else
                paste_event(r);
            free(r->paste.data);
            memset(&r->paste, 0, sizeof(r->paste));
            r->in_paste = r->paste_overflow = 0;
            continue;
        }
        if (r->input[0] != 27) {
            const char *start = (const char *)r->input, *at = start;
            uint32_t cp;
            unsigned char first = r->input[0];
            int need = first < 128                      ? 1
                       : first >= 0xc2 && first <= 0xdf ? 2
                       : first >= 0xe0 && first <= 0xef ? 3
                       : first >= 0xf0 && first <= 0xf4 ? 4
                                                        : 1;
            if (r->input_size < (size_t)need && !flush)
                break;
            cp = decode(&at);
            if (!cp) {
                consume_input(r, 1);
                continue;
            }
            plain_codepoint(r, cp);
            consume_input(r, (size_t)(at - start));
            r->escape_at = now_seconds();
            continue;
        }
        if (r->input_size >= 6 && !memcmp(r->input, "\033[200~", 6)) {
            r->in_paste = 1;
            r->paste_overflow = 0;
            consume_input(r, 6);
            continue;
        }
        if (r->input_size == 1) {
            if (flush || now_seconds() - r->escape_at >= .035) {
                key_event(r, "Escape", 0, 0, 0);
                consume_input(r, 1);
            }
            break;
        }
        if (r->input[1] == '_' || r->input[1] == ']' || r->input[1] == 'P') {
            size_t i;
            for (i = 2; i < r->input_size; i++)
                if ((r->input[1] == ']' && r->input[i] == 7) ||
                    (r->input[i] == 27 && i + 1 < r->input_size && r->input[i + 1] == '\\')) {
                    n = i + (r->input[i] == 7 ? 1 : 2);
                    break;
                }
            if (!n)
                break;
            consume_input(r, n);
            continue;
        }
        if (r->input[1] == '[') {
            size_t i;
            for (i = 2; i < r->input_size; i++)
                if (r->input[i] >= 0x40 && r->input[i] <= 0x7e) {
                    n = i + 1;
                    break;
                }
            if (!n) {
                if (r->input_size > 1024)
                    consume_input(r, r->input_size);
                break;
            }
        } else if (r->input[1] == 'O') {
            if (r->input_size < 3)
                break;
            n = 3;
        } else {
            consume_input(r, 1);
            continue;
        }
        sequence(r, r->input, n);
        consume_input(r, n);
    }
}

#ifdef _WIN32
static DWORD WINAPI console_reader(LPVOID context) {
    PTBackend *r = (PTBackend *)context;
    WCHAR pending = 0;
    while (!InterlockedCompareExchange(&r->stop_reader, 0, 0)) {
        WCHAR text[4096];
        char utf8[16384];
        DWORD count = 0;
        int offset = pending ? 1 : 0, n;
        if (pending)
            text[0] = pending;
        if (!ReadConsoleW(r->input_handle, text + offset, 4095 - (DWORD)offset, &count, NULL)) {
            if (!InterlockedCompareExchange(&r->stop_reader, 0, 0))
                InterlockedExchange(&r->read_failed, 1);
            break;
        }
        count += (DWORD)offset;
        pending = 0;
        if (count && text[count - 1] >= 0xd800 && text[count - 1] <= 0xdbff) {
            pending = text[count - 1];
            count--;
        }
        n = WideCharToMultiByte(CP_UTF8, 0, text, (int)count, utf8, sizeof(utf8), NULL, NULL);
        if (n > 0) {
            for (;;) {
                int done = 0;
                if (InterlockedCompareExchange(&r->stop_reader, 0, 0))
                    return 0;
                EnterCriticalSection(&r->input_lock);
                if (r->incoming.size + (size_t)n <= 1024u * 1024u) {
                    append(&r->incoming, utf8, (size_t)n);
                    done = 1;
                }
                LeaveCriticalSection(&r->input_lock);
                if (done)
                    break;
                Sleep(1);
            }
        }
    }
    return 0;
}
#endif
#ifdef _WIN32
/* Only this worker writes the console. Its copied chunk keeps model/transport
   progress independent of a slow or stalled terminal and makes cancellation
   bounded without exposing the renderer's reallocating output buffer. */
static DWORD WINAPI console_writer(LPVOID context) {
    PTBackend *r = (PTBackend *)context;
    for (;;) {
        WaitForSingleObject(r->writer_wake, INFINITE);
        if (InterlockedCompareExchange(&r->stop_writer, 0, 0))
            break;
        r->writer_written = 0;
        r->writer_ok = WriteFile(r->output_handle, r->writer_bytes, r->writer_count, &r->writer_written, NULL) != 0;
        SetEvent(r->writer_done);
    }
    return 0;
}
static int stop_worker(HANDLE worker, volatile LONG *flag, HANDLE wake) {
    int i;
    if (!worker)
        return 1;
    InterlockedExchange(flag, 1);
    if (wake)
        SetEvent(wake);
    for (i = 0; i < 100; i++) {
        CancelSynchronousIo(worker);
        if (WaitForSingleObject(worker, 10) == WAIT_OBJECT_0) {
            CloseHandle(worker);
            return 1;
        }
    }
    return 0;
}
#endif
static void queue_title(PTBackend *r) {
    const char *at = r->title;
    int count = 0;
    if (!r->active)
        return;
    emit(&r->output, "\033]2;");
    while (*at && count < 256) {
        uint32_t cp = decode(&at);
        char encoded[4];
        int n;
        if (cp < 32 || (cp >= 127 && cp <= 159))
            continue;
        n = encode(cp, encoded);
        append(&r->output, encoded, (size_t)n);
        count++;
    }
    emit(&r->output, "\007");
}
static int color_mode(const char *requested) {
    if (!strcmp(requested, "none"))
        return 3;
    if (!strcmp(requested, "16"))
        return 2;
    if (!strcmp(requested, "256"))
        return 1;
    if (!strcmp(requested, "truecolor"))
        return 0;
    if (strcmp(requested, "auto"))
        return -1;
    {
        const char *no = getenv("NO_COLOR"), *term = getenv("TERM"), *ct = getenv("COLORTERM"),
                   *wt = getenv("WT_SESSION"), *program = getenv("TERM_PROGRAM");
        if ((no && *no) || (term && !strcmp(term, "dumb")))
            return 3;
        if ((ct && (!strcmp(ct, "truecolor") || !strcmp(ct, "24bit"))) || (wt && *wt) ||
            (program &&
             (!strcmp(program, "iTerm.app") || !strcmp(program, "WezTerm") || !strcmp(program, "ghostty"))) ||
            (term && (!strncmp(term, "xterm-kitty", 11) || !strncmp(term, "xterm-ghostty", 13))))
            return 0;
        return term && strstr(term, "256color") ? 1 : 2;
    }
}
static int flush_terminal(PTBackend *r, char *error, int error_size) {
    size_t budget = 65536;
    if (r->headless)
        return 0;
    while (r->sent < r->output.size && budget) {
        size_t amount = r->output.size - r->sent;
        if (amount > budget)
            amount = budget;
#ifdef _WIN32
        if (r->write_failed || !r->writer) {
            fail(error, error_size, "Terminal output was disconnected");
            return -1;
        }
        if (r->writer_busy) {
            if (WaitForSingleObject(r->writer_done, 0) != WAIT_OBJECT_0)
                return 0;
            r->writer_busy = 0;
            if (!r->writer_ok || !r->writer_written) {
                r->write_failed = 1;
                fail(error, error_size, "Terminal output was disconnected");
                return -1;
            }
            r->sent += r->writer_written;
            r->terminal_bytes_written += r->writer_written;
            budget -= r->writer_written;
            continue;
        }
        memcpy(r->writer_bytes, r->output.data + r->sent, amount);
        r->writer_count = (DWORD)amount;
        r->writer_busy = 1;
        SetEvent(r->writer_wake);
        return 0;
#else
        ssize_t written = write(r->out_fd, r->output.data + r->sent, amount);
        if (written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
            break;
        if (written < 0 && errno == EINTR)
            continue;
        if (written <= 0) {
            fail(error, error_size, "Terminal output was disconnected");
            return -1;
        }
        r->sent += (size_t)written;
        r->terminal_bytes_written += (size_t)written;
        budget -= (size_t)written;
#endif
    }
    if (r->sent == r->output.size) {
        free(r->output.data);
        memset(&r->output, 0, sizeof(r->output));
        r->sent = 0;
    }
    return 0;
}
int pt_present(PTBackend *r, char *error, int error_size) {
    Buffer encoded;
    if (!r || !r->current) {
        fail(error, error_size, "Terminal has no frame");
        return 0;
    }
    if (!r->headless) {
        if (flush_terminal(r, error, error_size) < 0)
            return 0;
        if (r->output.size)
            return 1;
    }
    if (r->current == r->previous)
        return 1;
    encoded = ansi_frame(r, 1);
    if (encoded.failed) {
        free(encoded.data);
        fail(error, error_size, "Cannot allocate terminal ANSI frame");
        return 0;
    }
    frame_release(r->previous);
    r->previous = r->current;
    r->previous->refs++;
    if (encoded.size) {
        r->ansi_frames_encoded++;
        r->ansi_bytes_encoded += encoded.size;
    }
    if (r->headless) {
        free(encoded.data);
        return 1;
    }
    r->output = encoded;
    return flush_terminal(r, error, error_size) == 0;
}
static int resize_terminal(PTBackend *r, int cols, int rows) {
    if (cols < 1 || rows < 1 || cols > 16384 || rows > 16384 || (size_t)cols * rows > PT_MAX_CELLS)
        return 0;
    if (cols != r->cols || rows != r->rows) {
        cJSON *e, *viewport;
        r->cols = cols;
        r->rows = rows;
        frame_release(r->previous);
        r->previous = NULL;
        e = event(r, "viewport");
        if (e) {
            viewport = cJSON_CreateObject();
            cJSON_AddNumberToObject(viewport, "width", cols * 8);
            cJSON_AddNumberToObject(viewport, "height", rows * 16);
            cJSON_AddNumberToObject(viewport, "scale", 1);
            cJSON_AddItemToObject(e, "viewport", viewport);
        }
    }
    return 1;
}
static void terminal_size(PTBackend *r) {
    if (r->headless)
        return;
#ifdef _WIN32
    {
        CONSOLE_SCREEN_BUFFER_INFO info;
        if (GetConsoleScreenBufferInfo(r->output_handle, &info))
            resize_terminal(r, info.srWindow.Right - info.srWindow.Left + 1,
                            info.srWindow.Bottom - info.srWindow.Top + 1);
    }
#else
    {
        struct winsize size;
        if (ioctl(r->out_fd, TIOCGWINSZ, &size) == 0 && size.ws_col && size.ws_row)
            resize_terminal(r, size.ws_col, size.ws_row);
    }
#endif
}
PTBackend *pt_open(const cJSON *config, char *error, int error_size) {
    PTBackend *r = (PTBackend *)calloc(1, sizeof(*r));
    int cols, rows;
    if (!r) {
        fail(error, error_size, "Cannot allocate terminal backend");
        return NULL;
    }
    r->headless = boolean(item(config, "headless"), boolean(item(config, "hidden"), 0));
    r->color_mode = color_mode(str(item(config, "color"), "auto"));
    r->events = cJSON_CreateArray();
    r->title = copy_string(str(item(config, "title"), "Pysual"));
    cols = bounded_int(num(item(config, "columns"), floor(num(item(config, "width"), 800) / 8)));
    rows = bounded_int(num(item(config, "rows"), floor(num(item(config, "height"), 576) / 16)));
    if (cols < 1)
        cols = 1;
    if (rows < 1)
        rows = 1;
#ifndef _WIN32
    r->in_fd = r->out_fd = -1;
#endif
    if (r->color_mode < 0 || !r->events || !r->title || !resize_terminal(r, cols, rows)) {
        fail(error, error_size, "Invalid terminal color mode or viewport budget");
        pt_close(r);
        return NULL;
    }
    if (!r->headless) {
#ifdef _WIN32
        AttachConsole(ATTACH_PARENT_PROCESS);
        r->input_handle = CreateFileW(L"CONIN$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                                      OPEN_EXISTING, 0, NULL);
        r->output_handle = CreateFileW(L"CONOUT$", GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                       NULL, OPEN_EXISTING, 0, NULL);
        if (r->input_handle == INVALID_HANDLE_VALUE || r->output_handle == INVALID_HANDLE_VALUE ||
            !GetConsoleMode(r->input_handle, &r->input_mode) || !GetConsoleMode(r->output_handle, &r->output_mode)) {
            fail(error, error_size,
                 "The terminal backend requires an attached interactive Windows console; use headless for snapshots");
            pt_close(r);
            return NULL;
        }
        r->output_cp = GetConsoleOutputCP();
        r->active = 1;
        if (!SetConsoleMode(r->input_handle, (r->input_mode & ~(0x1u | 0x2u | 0x4u | 0x40u)) | 0x80u | 0x200u) ||
            !SetConsoleMode(r->output_handle, r->output_mode | 0x1u | 0x4u) || !SetConsoleOutputCP(65001)) {
            fail(error, error_size, "Could not enable Windows VT terminal modes");
            pt_close(r);
            return NULL;
        }
        InitializeCriticalSection(&r->input_lock);
        r->lock_ready = 1;
        r->reader = CreateThread(NULL, 0, console_reader, r, 0, NULL);
        r->writer_wake = CreateEventW(NULL, FALSE, FALSE, NULL);
        r->writer_done = CreateEventW(NULL, FALSE, FALSE, NULL);
        if (r->writer_wake && r->writer_done)
            r->writer = CreateThread(NULL, 0, console_writer, r, 0, NULL);
        if (!r->reader || !r->writer) {
            fail(error, error_size, "Could not start the cancellable console I/O workers");
            pt_close(r);
            return NULL;
        }
#else
        {
            struct termios raw;
            r->in_fd = r->out_fd = open("/dev/tty", O_RDWR | O_NOCTTY | O_NONBLOCK);
            if (r->in_fd < 0 || tcgetattr(r->in_fd, &r->saved) < 0) {
                fail(error, error_size, "The terminal backend needs a controlling TTY; use headless for snapshots");
                pt_close(r);
                return NULL;
            }
            raw = r->saved;
            raw.c_iflag &= (tcflag_t) ~(IGNBRK | BRKINT | PARMRK | ISTRIP | INLCR | IGNCR | ICRNL | IXON);
            raw.c_oflag &= (tcflag_t)~OPOST;
            raw.c_lflag &= (tcflag_t) ~(ECHO | ECHONL | ICANON | ISIG | IEXTEN);
            raw.c_cflag &= (tcflag_t) ~(CSIZE | PARENB);
            raw.c_cflag |= CS8;
            raw.c_cc[VMIN] = 0;
            raw.c_cc[VTIME] = 0;
            if (tcsetattr(r->in_fd, TCSANOW, &raw) < 0) {
                fail(error, error_size, "Could not enter terminal raw mode");
                pt_close(r);
                return NULL;
            }
            r->active = 1;
        }
#endif
        terminal_size(r);
        emit(&r->output, "\033[22;0t\033[?1049h\033[?25l\033[?7l\033[?1003h\033[?1006h\033[?1004h\033[?2004h\033["
                         "2J\033[H\033[18t\033[?u");
        queue_title(r);
        if (flush_terminal(r, error, error_size) < 0) {
            pt_close(r);
            return NULL;
        }
    }
    cJSON_Delete(r->events);
    r->events = cJSON_CreateArray();
    return r;
}
void pt_close(PTBackend *r) {
    ImageResource *im;
    if (!r)
        return;
    if (r->active) {
        char ignored[128];
        double deadline = now_seconds() + .25;
        if (r->keyboard_enabled)
            emit(&r->output, "\033[<u");
        emit(&r->output, "\033[?2026l\033[?1016l\033[?1003l\033[?1002l\033[?1000l\033[?1006l\033[?1004l\033[?2004l\033["
                         "0m\033[?7h\033[?25h\033[?1049l\033[23;0t");
        while (r->output.size && now_seconds() < deadline) {
            if (flush_terminal(r, ignored, sizeof(ignored)) < 0)
                break;
#ifdef _WIN32
            Sleep(1);
#else
            {
                struct timespec pause = {0, 1000000};
                nanosleep(&pause, NULL);
            }
#endif
        }
#ifdef _WIN32
        {
            int reader_stopped = stop_worker(r->reader, &r->stop_reader, NULL),
                writer_stopped = stop_worker(r->writer, &r->stop_writer, r->writer_wake);
            SetConsoleMode(r->input_handle, r->input_mode);
            SetConsoleMode(r->output_handle, r->output_mode);
            SetConsoleOutputCP(r->output_cp);
            /* A failed OS cancellation must not free a context still in use. The
           enclosing native process exits immediately after backend close. */
            if (!reader_stopped || !writer_stopped)
                return;
            r->reader = r->writer = NULL;
        }
#else
        tcsetattr(r->in_fd, TCSANOW, &r->saved);
#endif
    }
#ifdef _WIN32
    if (r->writer_wake)
        CloseHandle(r->writer_wake);
    if (r->writer_done)
        CloseHandle(r->writer_done);
    if (r->lock_ready)
        DeleteCriticalSection(&r->input_lock);
    free(r->incoming.data);
    if (r->input_handle && r->input_handle != INVALID_HANDLE_VALUE)
        CloseHandle(r->input_handle);
    if (r->output_handle && r->output_handle != INVALID_HANDLE_VALUE)
        CloseHandle(r->output_handle);
#else
    if (r->in_fd >= 0)
        close(r->in_fd);
#endif
    im = r->images;
    while (im) {
        ImageResource *next = im->next;
        release_image(im);
        im = next;
    }
    segments_clear(r);
    cJSON_Delete(r->commands);
    cJSON_Delete(r->events);
    frame_release(r->current);
    frame_release(r->previous);
    free(r->input);
    free(r->paste.data);
    free(r->output.data);
    free(r->title);
    free(r);
}
cJSON *pt_poll(PTBackend *r) {
    cJSON *out;
    if (!r)
        return cJSON_CreateArray();
    if (!r->headless && !r->io_failed) {
        terminal_size(r);
#ifdef _WIN32
        if (InterlockedCompareExchange(&r->read_failed, 0, 0))
            io_error(r, "Native terminal input was disconnected");
        EnterCriticalSection(&r->input_lock);
        if (r->incoming.size) {
            input_append(r, (unsigned char *)r->incoming.data, r->incoming.size);
            r->incoming.size = 0;
            if (r->incoming.data)
                r->incoming.data[0] = 0;
        }
        LeaveCriticalSection(&r->input_lock);
#else
        {
            unsigned char data[65536];
            int attempts;
            for (attempts = 0; attempts < 4; attempts++) {
                ssize_t n = read(r->in_fd, data, sizeof(data));
                if (n > 0)
                    input_append(r, data, (size_t)n);
                else {
                    if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
                        io_error(r, "Native terminal input was disconnected");
                    break;
                }
            }
        }
#endif
        {
            char error[128];
            if (flush_terminal(r, error, sizeof(error)) < 0)
                io_error(r, error);
            else if (!r->output.size && frame_current(r) && r->current != r->previous)
                pt_present(r, error, sizeof(error));
        }
    }
    parse_input(r, 0);
    out = r->events;
    r->events = cJSON_CreateArray();
    return out;
}
static cJSON *pair(double x, double y) {
    cJSON *a = cJSON_CreateArray();
    cJSON_AddItemToArray(a, cJSON_CreateNumber(x));
    cJSON_AddItemToArray(a, cJSON_CreateNumber(y));
    return a;
}
cJSON *pt_info(PTBackend *r) {
    cJSON *j = cJSON_CreateObject();
    if (!r)
        return j;
    cJSON_AddStringToObject(j, "renderer", "native-cells");
    cJSON_AddStringToObject(j, "backend", "terminal");
    cJSON_AddNumberToObject(j, "width", r->cols * 8);
    cJSON_AddNumberToObject(j, "height", r->rows * 16);
    cJSON_AddNumberToObject(j, "scale", 1);
    cJSON_AddNumberToObject(j, "resource_revision", (double)r->resource_revision);
    cJSON_AddNumberToObject(j, "columns", r->cols);
    cJSON_AddNumberToObject(j, "rows", r->rows);
    cJSON_AddNumberToObject(j, "text_row_height", 16);
    cJSON_AddItemToObject(j, "pixel_size", pair(r->cols * 8, r->rows * 16));
    cJSON_AddBoolToObject(j, "visible", !r->headless);
    cJSON_AddBoolToObject(j, "vsync", 0);
    cJSON_AddBoolToObject(j, "scene_patches", 1);
    cJSON_AddBoolToObject(j, "cache_scene", 1);
#ifdef PT_WITH_SDL_IMAGE
    cJSON_AddBoolToObject(j, "images", 1);
#else
    cJSON_AddBoolToObject(j, "images", 0);
#endif
    cJSON_AddNumberToObject(j, "retained_scene_budget", PT_SCENE_BUDGET);
#define PT_STAT(name) cJSON_AddNumberToObject(j, #name, (double)r->name)
    PT_STAT(retained_scene_bytes);
    PT_STAT(image_bytes);
    PT_STAT(image_count);
    PT_STAT(segment_count);
    PT_STAT(command_count);
    PT_STAT(commands_touched);
    PT_STAT(commands_replayed);
    PT_STAT(last_commands_touched);
    PT_STAT(last_commands_replayed);
    PT_STAT(segments_updated);
    PT_STAT(segments_removed);
    PT_STAT(segments_tested);
    PT_STAT(last_segments_tested);
    PT_STAT(scene_patch_updates);
    PT_STAT(unchanged_segments);
    PT_STAT(cell_frames_composed);
    PT_STAT(cell_cache_hits);
    PT_STAT(ansi_frames_encoded);
    PT_STAT(ansi_bytes_encoded);
    PT_STAT(terminal_bytes_written);
#undef PT_STAT
    cJSON_AddNumberToObject(j, "terminal_output_pending_bytes", (double)(r->output.size - r->sent));
    return j;
}
static cJSON *rgb(Color c) {
    cJSON *a = cJSON_CreateArray();
    cJSON_AddItemToArray(a, cJSON_CreateNumber(c.r));
    cJSON_AddItemToArray(a, cJSON_CreateNumber(c.g));
    cJSON_AddItemToArray(a, cJSON_CreateNumber(c.b));
    return a;
}
static int snapshot_fits(const Frame *f) {
    /* Include the reply envelope, dimensions, row quotes/commas and the largest
       fixed cell fields. Every escaped text byte occurs in both cells and rows. */
    const size_t cell_bytes = sizeof("{\"text\":\"\",\"foreground\":[255,255,255],\"background\":[255,255,255],"
                                     "\"width\":2,\"underline\":false},") -
                              1;
    size_t i, count, bytes;
    if (!f)
        return 1;
    count = (size_t)f->cols * f->rows;
    bytes = 128 + (size_t)f->rows * 3 + count * cell_bytes;
    if (bytes > PX_MAX_PAYLOAD)
        return 0;
    for (i = 0; i < count; ++i) {
        const unsigned char *p = (const unsigned char *)f->cells[i].text;
        for (; *p; ++p) {
            bytes += 2 * (*p < 32 ? 6 : *p == '"' || *p == '\\' ? 2 : 1);
            if (bytes > PX_MAX_PAYLOAD)
                return 0;
        }
    }
    return 1;
}
static cJSON *snapshot(PTBackend *r, char *error, int error_size) {
    Frame *f = r->current;
    cJSON *j, *cells, *rows;
    int x, y;
    if (!snapshot_fits(f)) {
        fail(error, error_size, "Terminal snapshot exceeds the 16 MiB reply budget; use a smaller viewport");
        return NULL;
    }
    j = cJSON_CreateObject();
    cells = cJSON_CreateArray();
    rows = cJSON_CreateArray();
    cJSON_AddNumberToObject(j, "columns", r->cols);
    cJSON_AddNumberToObject(j, "rows", r->rows);
    cJSON_AddItemToObject(j, "cells", cells);
    cJSON_AddItemToObject(j, "rows_text", rows);
    if (!f)
        return j;
    for (y = 0; y < f->rows; y++) {
        Buffer text = {0};
        for (x = 0; x < f->cols; x++) {
            Cell c = f->cells[(size_t)y * f->cols + x];
            cJSON *v = cJSON_CreateObject();
            cJSON_AddStringToObject(v, "text", c.text);
            cJSON_AddItemToObject(v, "foreground", rgb(c.fg));
            cJSON_AddItemToObject(v, "background", rgb(c.bg));
            cJSON_AddNumberToObject(v, "width", c.width);
            cJSON_AddBoolToObject(v, "underline", c.underline);
            cJSON_AddItemToArray(cells, v);
            emit(&text, c.text);
        }
        append(&text, "", 0);
        cJSON_AddItemToArray(rows, cJSON_CreateString(text.data ? text.data : ""));
        free(text.data);
    }
    return j;
}
cJSON *pt_call(PTBackend *r, const cJSON *request, char *error, int error_size) {
    const char *op = str(item(request, "op"), "");
    if (!r) {
        fail(error, error_size, "Terminal is closed");
        return NULL;
    }
    if (!strcmp(op, "reload_image")) {
        const char *source = str(item(request, "source"), NULL);
        ImageResource **at = &r->images;
        if (!source || strlen(source) > PT_MAX_IMAGE_SOURCE) {
            fail(error, error_size, "Invalid terminal image source");
            return NULL;
        }
        while (*at) {
            ImageResource *image = *at;
            if (!strcmp(image->source, source)) {
                *at = image->next;
                r->image_bytes -= image->bytes;
                --r->image_count;
                release_image(image);
                break;
            }
            at = &image->next;
        }
        /* Keep the last snapshot and ANSI baseline until the scheduled redraw. */
        r->images_dirty = 1;
        ++r->resource_revision;
        return pt_info(r);
    }
    if (!strcmp(op, "measure")) {
        double w, h;
        const char *text = str(item(request, "text"), NULL);
        if (!text || strlen(text) > PT_MAX_TEXT) {
            fail(error, error_size, "Invalid terminal text");
            return NULL;
        }
        measure_text(text, &w, &h);
        return pair(w, h);
    }
    if (!strcmp(op, "measure_many")) {
        const cJSON *texts = item(request, "texts"), *text;
        cJSON *array;
        int count = 0, index = 0;
        if (!cJSON_IsArray(texts)) {
            fail(error, error_size, "Invalid terminal text batch");
            return NULL;
        }
        count = cJSON_GetArraySize(texts);
        if (count < 0 || count > 4096) {
            fail(error, error_size, "Terminal text batch exceeds 4096 strings");
            return NULL;
        }
        array = cJSON_CreateArray();
        if (!array) {
            fail(error, error_size, "Cannot allocate text metrics");
            return NULL;
        }
        cJSON_ArrayForEach(text, texts) {
            double w, h;
            const char *value = cJSON_IsString(text) ? text->valuestring : NULL;
            if (!value || strlen(value) > PT_MAX_TEXT) {
                cJSON_Delete(array);
                fail(error, error_size, "Invalid terminal text");
                return NULL;
            }
            measure_text(value, &w, &h);
            cJSON_AddItemToArray(array, pair(w, h));
            ++index;
        }
        if (index != count) {
            cJSON_Delete(array);
            fail(error, error_size, "Invalid terminal text batch");
            return NULL;
        }
        return array;
    }
    if (!strcmp(op, "snapshot"))
        return snapshot(r, error, error_size);
    if (!strcmp(op, "info"))
        return pt_info(r);
    if (!strcmp(op, "frame") || !strcmp(op, "ansi")) {
        Buffer encoded = ansi_frame(r, boolean(item(request, "diff"), 1));
        cJSON *value;
        if (encoded.failed) {
            free(encoded.data);
            fail(error, error_size, "No terminal frame or ANSI allocation failed");
            return NULL;
        }
        value = cJSON_CreateString(encoded.data ? encoded.data : "");
        free(encoded.data);
        return value;
    }
    if (!strcmp(op, "feed_input")) {
        const char *hex = str(item(request, "hex"), NULL), *text = str(item(request, "data"), "");
        if (hex) {
            Buffer bytes = {0};
            size_t i, n = strlen(hex);
            if (n % 2) {
                fail(error, error_size, "Input hex must have complete bytes");
                return NULL;
            }
            for (i = 0; i < n; i += 2) {
                int a = hex_digit(hex[i]), b = hex_digit(hex[i + 1]);
                unsigned char ch;
                if (a < 0 || b < 0) {
                    free(bytes.data);
                    fail(error, error_size, "Invalid input hex");
                    return NULL;
                }
                ch = (unsigned char)(a * 16 + b);
                append(&bytes, &ch, 1);
            }
            input_append(r, (unsigned char *)bytes.data, bytes.size);
            free(bytes.data);
        } else
            input_append(r, (const unsigned char *)text, strlen(text));
        parse_input(r, boolean(item(request, "flush"), 0));
        return cJSON_CreateNull();
    }
    if (!strcmp(op, "set_size")) {
        terminal_size(r);
        return pt_info(r);
    }
    if (!strcmp(op, "text_input"))
        return cJSON_CreateNull();
    if (!strcmp(op, "set_title")) {
        const char *title = str(item(request, "title"), "");
        char *copy = copy_string(title);
        if (!copy) {
            fail(error, error_size, "Cannot allocate title");
            return NULL;
        }
        free(r->title);
        r->title = copy;
        queue_title(r);
        return cJSON_CreateNull();
    }
    if (!strcmp(op, "configure")) {
        const cJSON *v = item(request, "color"), *cv = item(request, "columns"), *rv = item(request, "rows");
        int mode = v ? color_mode(str(v, "")) : r->color_mode, cols = r->cols, rows = r->rows;
        if (mode < 0) {
            fail(error, error_size, "Invalid color mode");
            return NULL;
        }
        if (cv || rv) {
            double c = num(cv, cols), h = num(rv, rows);
            if (!r->headless || (cv && !cJSON_IsNumber(cv)) || (rv && !cJSON_IsNumber(rv)) || !isfinite(c) ||
                !isfinite(h) || c < 1 || h < 1 || c > 16384 || h > 16384 || floor(c) != c || floor(h) != h ||
                c * h > PT_MAX_CELLS) {
                fail(error, error_size, "Invalid or terminal-owned viewport");
                return NULL;
            }
            cols = (int)c;
            rows = (int)h;
        }
        if (mode != r->color_mode) {
            r->color_mode = mode;
            frame_release(r->previous);
            r->previous = NULL;
        }
        resize_terminal(r, cols, rows);
        return cJSON_CreateNull();
    }
    if (!strcmp(op, "capture")) {
        const char *path = str(item(request, "path"), NULL);
        Buffer frame;
        FILE *file;
        if (!path) {
            fail(error, error_size, "Capture needs a path");
            return NULL;
        }
        frame = ansi_frame(r, 0);
        if (frame.failed) {
            free(frame.data);
            fail(error, error_size, "Cannot encode capture");
            return NULL;
        }
        file = capture_file(path);
        if (!file) {
            free(frame.data);
            fail(error, error_size, "Cannot open terminal capture path");
            return NULL;
        }
        if (fwrite(frame.data, 1, frame.size, file) != frame.size) {
            fclose(file);
            free(frame.data);
            fail(error, error_size, "Cannot write terminal capture");
            return NULL;
        }
        fclose(file);
        free(frame.data);
        return cJSON_CreateNull();
    }
    fail(error, error_size,
         !strncmp(op, "clipboard_", 10) ? "Native terminal clipboard service is unavailable; use terminal paste"
                                        : "Unsupported native terminal operation");
    return NULL;
}
