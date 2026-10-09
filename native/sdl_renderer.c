/* Native SDL3 painter. The retained command tree contains data only. */
#define SDL_MAIN_HANDLED
#include <SDL3/SDL.h>
#include <SDL3/SDL_main.h>
#include <SDL3_ttf/SDL_ttf.h>
#include <SDL3_image/SDL_image.h>
#include "px_backend.h"
#include "clipboard.h"
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PX_MAX_COMMANDS 200000
#define PX_MAX_TEXTURE_PIXELS (16u * 1024u * 1024u)
#define PX_MAX_IMAGE_BYTES (8u * 1024u * 1024u)
#define PX_IMAGE_BUDGET (32u * 1024u * 1024u)
#define PX_MAX_STRING (16u * 1024u * 1024u)
#define PX_CACHE_ENTRIES 512
#define PX_TEXTURE_BUCKETS 1024
#define PX_FONT_ENTRIES 32
#define PX_SCENE_BUDGET (64u * 1024u * 1024u)
typedef struct {
    double x, y, w, h;
} Box;
typedef struct {
    unsigned char r, g, b, a;
    int set;
} Color;
typedef struct Texture {
    char *key;
    SDL_Texture *texture;
    int w, h, kind;
    size_t bytes;
    const char *source_identity; /* Valid only until the next immutable scene commit. */
    uint64_t used;
    uint32_t hash;
    struct Texture *hash_next;
    struct Texture *next;
} Texture;
typedef struct {
    TTF_Font *font;
    int pixels, mono;
    uint64_t used;
} Font;
typedef struct {
    Color fill, foreground, border, fill_end, border_end, bevel_light, bevel_dark;
    Color highlight, inner_border, glow, pattern_color, shadow;
    double radius, border_width, glow_width, bevel_width, pattern_spacing;
    double shadow_blur, shadow_x, shadow_y;
    int horizontal, bevel, pattern;
} Style;
enum {
    CMD_UNKNOWN = 0,
    CMD_BEGIN,
    CMD_CLIP,
    CMD_RECT,
    CMD_TEXT,
    CMD_LINE,
    CMD_LINES,
    CMD_IMAGE,
    CMD_IMAGE_NINE,
    CMD_GRADIENT,
    CMD_STYLED,
    CMD_MARKER,
    CMD_CARET,
    CMD_FOCUS,
    CMD_SPRITE,
    CMD_ANIMATED
};
typedef struct DrawCommand {
    int op, flag, flag2, point_count;
    Box box;
    Color fill, stroke, tint;
    double a, b, c, from, to, duration, edges[4];
    const char *text, *extra;
    Style style;
    const cJSON *clip;
    double *points;
    int frame_w, frame_h, frame_count;
} DrawCommand;
typedef struct Segment {
    char *id;
    Box bounds, current_bounds;
    cJSON *commands, *previous;
    DrawCommand *decoded;
    size_t decoded_count;
    double *epochs, transition_started, transition_duration, animation_until;
    size_t count, bytes, previous_bytes;
    int looping, animation_pending;
    struct Segment *active_next, *active_previous;
    int active;
} Segment;
typedef struct {
    Segment *segment;
    size_t position;
} SegmentSlot;
struct PXRenderer {
    SDL_Window *window;
    SDL_Renderer *renderer;
    SDL_WindowID window_id;
    int initialized, ttf_initialized, automatic_scale, window_w, window_h, pixel_w, pixel_h;
    double scale, width, height;
    uint64_t revision, clock;
    char *font_dir;
    Font fonts[PX_FONT_ENTRIES];
    Texture *textures;
    Texture *buckets[PX_TEXTURE_BUCKETS];
    SDL_Texture *scene_cache;
    size_t scene_cache_bytes;
    uint64_t scene_cache_hits, image_decode_attempts;
    int cache_scene, scene_cache_valid;
    size_t cache_bytes[5];
    unsigned int cache_entries;
    cJSON *commands, *previous;
    DrawCommand *decoded;
    size_t decoded_count;
    double transition_seconds, transition_duration, transition_started, commit_started, animation_until;
    double *animation_started;
    size_t command_count;
    Segment **segments;
    SegmentSlot *segment_table;
    size_t segment_capacity;
    size_t segment_count, segment_index_bytes, retained_scene_bytes;
    Segment *active_segments;
    size_t active_segment_count;
    int segmented, damage_enabled, footprints_stale;
    SDL_Rect damage;
    Box dirty[64];
    size_t dirty_count;
    Color background;
    uint64_t commands_touched, commands_replayed, segments_updated, segments_removed, scene_patch_updates,
        last_commands_touched, last_commands_replayed;
    uint64_t segments_tested, last_segments_tested, animation_segments_tested;
    uint64_t unchanged_segments;
    char failed_images[128][80];
    uint64_t failed_used[128];
    cJSON *resource_events;
    int has_sprites, animation_pending, animation_settled, reduce_motion, vsync, clip_enabled;
    SDL_Rect clip;
    char error[512];
};

static void discard_scene_cache(PXRenderer *r) {
    if (r->scene_cache)
        SDL_DestroyTexture(r->scene_cache);
    r->scene_cache = NULL;
    r->scene_cache_bytes = 0;
    r->scene_cache_valid = 0;
}

static double px_now(void) {
    return (double)SDL_GetPerformanceCounter() / SDL_GetPerformanceFrequency();
}
static double min2(double a, double b) {
    return a < b ? a : b;
}
static double max2(double a, double b) {
    return a > b ? a : b;
}
static double clamp(double x, double lo, double hi) {
    return min2(hi, max2(lo, x));
}
/* Python round uses ties-to-even; lround would change half-pixel edges. */
static int rnd(double x) {
    double f = floor(x), d = x - f;
    return (int)(d < 0.5 ? f : d > 0.5 ? f + 1 : fmod(fabs(f), 2.0) == 0 ? f : f + 1);
}
static const cJSON *at(const cJSON *v, int i) {
    return cJSON_GetArrayItem(v, i);
}
static const cJSON *get(const cJSON *v, const char *key) {
    return cJSON_GetObjectItemCaseSensitive(v, key);
}
static double number(const cJSON *v, double fallback) {
    return cJSON_IsNumber(v) ? v->valuedouble : fallback;
}
static double num(const cJSON *v, const char *key, double fallback) {
    return number(get(v, key), fallback);
}
static const char *string(const cJSON *v, const char *fallback) {
    return cJSON_IsString(v) ? v->valuestring : fallback;
}
static int boolean(const cJSON *v, int fallback) {
    return cJSON_IsBool(v) ? cJSON_IsTrue(v) : fallback;
}
static char *copy_string(const char *s) {
    size_t n = strlen(s) + 1;
    char *p = (char *)malloc(n);
    if (p)
        memcpy(p, s, n);
    return p;
}
static int fail(PXRenderer *r, const char *message) {
    snprintf(r->error, sizeof(r->error), "%s: %s", message, SDL_GetError());
    return 0;
}
static void output_error(PXRenderer *r, char *error, int size) {
    if (error && size > 0)
        snprintf(error, (size_t)size, "%s", r->error[0] ? r->error : "Native renderer failed");
}
static int hex(char c) {
    return c >= '0' && c <= '9'   ? c - '0'
           : c >= 'a' && c <= 'f' ? c - 'a' + 10
           : c >= 'A' && c <= 'F' ? c - 'A' + 10
                                  : -1;
}
static Color color(const char *s) {
    Color c = {0, 0, 0, 0, 0};
    int v[8], n, i;
    if (!s || !*s)
        return c;
    if (*s == '#')
        ++s;
    n = (int)strlen(s);
    if (n != 3 && n != 4 && n != 6 && n != 8)
        return c;
    for (i = 0; i < n; ++i)
        if ((v[i] = hex(s[i])) < 0)
            return c;
    if (n <= 4) {
        c.r = (unsigned char)(v[0] * 17);
        c.g = (unsigned char)(v[1] * 17);
        c.b = (unsigned char)(v[2] * 17);
        c.a = (unsigned char)(n == 4 ? v[3] * 17 : 255);
    } else {
        c.r = (unsigned char)(v[0] * 16 + v[1]);
        c.g = (unsigned char)(v[2] * 16 + v[3]);
        c.b = (unsigned char)(v[4] * 16 + v[5]);
        c.a = (unsigned char)(n == 8 ? v[6] * 16 + v[7] : 255);
    }
    c.set = 1;
    return c;
}
static Color json_color(const cJSON *v) {
    return color(string(v, ""));
}
static int draw_color(PXRenderer *r, Color c) {
    return SDL_SetRenderDrawColor(r->renderer, c.r, c.g, c.b, c.a) || fail(r, "Set draw color");
}
static Box box_json(const cJSON *v) {
    Box b = {number(at(v, 0), 0), number(at(v, 1), 0), number(at(v, 2), 0), number(at(v, 3), 0)};
    return b;
}
static Box inset(Box b, double n) {
    b.x += n;
    b.y += n;
    b.w = max2(0, b.w - 2 * n);
    b.h = max2(0, b.h - 2 * n);
    return b;
}
static SDL_Rect pixel_box(PXRenderer *r, Box b) {
    SDL_Rect p;
    p.x = rnd(b.x * r->scale);
    p.y = rnd(b.y * r->scale);
    p.w = b.w > 0 ? rnd((b.x + b.w) * r->scale) - p.x : 0;
    p.h = b.h > 0 ? rnd((b.y + b.h) * r->scale) - p.y : 0;
    return p;
}
static int set_clip(PXRenderer *r, const SDL_Rect *clip) {
    SDL_Rect restricted;
    if (r->damage_enabled) {
        if (clip) {
            if (!SDL_GetRectIntersection(clip, &r->damage, &restricted))
                restricted = (SDL_Rect){0, 0, 0, 0};
        } else
            restricted = r->damage;
        clip = &restricted;
    }
    if (!SDL_SetRenderClipRect(r->renderer, clip))
        return fail(r, "Set clip");
    r->clip_enabled = clip != NULL;
    if (clip)
        r->clip = *clip;
    return 1;
}
static int clip_box(PXRenderer *r, const cJSON *value) {
    Box b;
    SDL_Rect p;
    if (!value || cJSON_IsNull(value))
        return set_clip(r, NULL);
    b = box_json(value);
    p.x = (int)floor(b.x * r->scale + 1e-7);
    p.y = (int)floor(b.y * r->scale + 1e-7);
    p.w = b.w > 0 ? (int)ceil((b.x + b.w) * r->scale - 1e-7) - p.x : 0;
    p.h = b.h > 0 ? (int)ceil((b.y + b.h) * r->scale - 1e-7) - p.y : 0;
    return set_clip(r, &p);
}
static int fill_pixels(PXRenderer *r, double x, double y, double w, double h, Color c) {
    SDL_FRect b = {(float)x, (float)y, (float)w, (float)h};
    if (w <= 0 || h <= 0 || !c.set || !c.a)
        return 1;
    return draw_color(r, c) && (SDL_RenderFillRect(r->renderer, &b) || fail(r, "Fill rectangle"));
}
static int visible_pixels(PXRenderer *r, SDL_Rect b) {
    SDL_Rect clip = r->clip_enabled ? r->clip : (SDL_Rect){0, 0, r->pixel_w, r->pixel_h};
    return b.w > 0 && b.h > 0 && b.x < clip.x + clip.w && b.y < clip.y + clip.h && b.x + b.w > clip.x &&
           b.y + b.h > clip.y;
}
static uint32_t texture_hash(const char *key) {
    uint32_t hash = 2166136261u;
    while (*key) {
        hash ^= (unsigned char)*key++;
        hash *= 16777619u;
    }
    return hash;
}
static Texture *cache_find(PXRenderer *r, const char *key) {
    uint32_t hash = texture_hash(key);
    Texture *t;
    for (t = r->buckets[hash & (PX_TEXTURE_BUCKETS - 1)]; t; t = t->hash_next)
        if (t->hash == hash && strcmp(t->key, key) == 0) {
            t->used = ++r->clock;
            return t;
        }
    return NULL;
}
static void cache_remove(PXRenderer *r, Texture *t) {
    Texture **p = &r->textures;
    Texture **bucket = &r->buckets[t->hash & (PX_TEXTURE_BUCKETS - 1)];
    while (*p && *p != t)
        p = &(*p)->next;
    if (*p) {
        *p = t->next;
        r->cache_bytes[t->kind] -= t->bytes;
        --r->cache_entries;
    }
    while (*bucket && *bucket != t)
        bucket = &(*bucket)->hash_next;
    if (*bucket)
        *bucket = t->hash_next;
    SDL_DestroyTexture(t->texture);
    free(t->key);
    free(t);
}
static Texture *cache_add(PXRenderer *r, const char *key, int kind, SDL_Surface *surface) {
    size_t bytes, limit = kind == 1 ? 8u * 1024u * 1024u : PX_IMAGE_BUDGET;
    Texture *t, *old;
    if (!surface) {
        fail(r, "Create image/text surface");
        return NULL;
    }
    bytes = (size_t)surface->w * (size_t)surface->h * 4 + strlen(key) + 1;
    if (surface->w < 1 || surface->h < 1 || (size_t)surface->w * (size_t)surface->h > PX_MAX_TEXTURE_PIXELS ||
        bytes > limit) {
        SDL_DestroySurface(surface);
        snprintf(r->error, sizeof(r->error), "Texture exceeds native resource budget");
        return NULL;
    }
    while (r->cache_entries >= PX_CACHE_ENTRIES || r->cache_bytes[kind] + bytes > limit) {
        old = NULL;
        for (t = r->textures; t; t = t->next)
            if ((r->cache_entries >= PX_CACHE_ENTRIES || t->kind == kind) && (!old || t->used < old->used))
                old = t;
        if (!old)
            break;
        cache_remove(r, old);
    }
    t = (Texture *)calloc(1, sizeof(*t));
    if (!t) {
        SDL_DestroySurface(surface);
        return NULL;
    }
    t->key = copy_string(key);
    t->w = surface->w;
    t->h = surface->h;
    t->texture = SDL_CreateTextureFromSurface(r->renderer, surface);
    SDL_DestroySurface(surface);
    if (!t->key || !t->texture) {
        if (t->texture)
            SDL_DestroyTexture(t->texture);
        free(t->key);
        free(t);
        fail(r, "Upload texture");
        return NULL;
    }
    if (!SDL_SetTextureBlendMode(t->texture, SDL_BLENDMODE_BLEND) ||
        !SDL_SetTextureScaleMode(t->texture, SDL_SCALEMODE_LINEAR)) {
        SDL_DestroyTexture(t->texture);
        free(t->key);
        free(t);
        fail(r, "Configure texture");
        return NULL;
    }
    t->bytes = bytes;
    t->kind = kind;
    t->used = ++r->clock;
    t->hash = texture_hash(key);
    t->hash_next = r->buckets[t->hash & (PX_TEXTURE_BUCKETS - 1)];
    r->buckets[t->hash & (PX_TEXTURE_BUCKETS - 1)] = t;
    t->next = r->textures;
    r->textures = t;
    r->cache_bytes[kind] += bytes;
    ++r->cache_entries;
    return t;
}
static int copy_texture(PXRenderer *r, Texture *t, Box destination, const Box *source, Color tint) {
    SDL_FRect d = {(float)destination.x, (float)destination.y, (float)destination.w, (float)destination.h}, s;
    if (destination.w <= 0 || destination.h <= 0)
        return 1;
    if (!SDL_SetTextureColorMod(t->texture, tint.r, tint.g, tint.b) || !SDL_SetTextureAlphaMod(t->texture, tint.a))
        return fail(r, "Tint texture");
    if (source) {
        s.x = (float)source->x;
        s.y = (float)source->y;
        s.w = (float)source->w;
        s.h = (float)source->h;
    }
    return SDL_RenderTexture(r->renderer, t->texture, source ? &s : NULL, &d) || fail(r, "Copy texture");
}
static int nine_texture(PXRenderer *r, Texture *t, SDL_Rect p, const double edges[4], Color tint) {
    double sx[4] = {0, edges[0], t->w - edges[2], t->w}, sy[4] = {0, edges[1], t->h - edges[3], t->h};
    double dx[4] = {p.x, p.x + edges[0], p.x + p.w - edges[2], p.x + p.w},
           dy[4] = {p.y, p.y + edges[1], p.y + p.h - edges[3], p.y + p.h};
    int x, y;
    for (y = 0; y < 3; ++y)
        for (x = 0; x < 3; ++x) {
            Box s = {sx[x], sy[y], sx[x + 1] - sx[x], sy[y + 1] - sy[y]},
                d = {dx[x], dy[y], dx[x + 1] - dx[x], dy[y + 1] - dy[y]};
            if (s.w > 0 && s.h > 0 && d.w > 0 && d.h > 0 && !copy_texture(r, t, d, &s, tint))
                return 0;
        }
    return 1;
}
static double rounded_distance(double x, double y, double w, double h, double radius) {
    double dx = fabs(x - w / 2) - (w / 2 - radius), dy = fabs(y - h / 2) - (h / 2 - radius);
    return hypot(max2(dx, 0), max2(dy, 0)) + min2(max2(dx, dy), 0) - radius;
}
static Texture *mask_texture(PXRenderer *r, int w, int h, double radius, double border, int blur) {
    char key[160];
    Texture *t;
    SDL_Surface *surface;
    int x, y;
    double tail = 0.5 * erfc(3 / sqrt(2.0));
    snprintf(key, sizeof(key), "mask:%d:%d:%.12g:%.12g:%d", w, h, radius, border, blur);
    if ((t = cache_find(r, key)))
        return t;
    if (w < 1 || h < 1 || w > 8192 || h > 8192 || (size_t)w * h > PX_MAX_TEXTURE_PIXELS) {
        snprintf(r->error, sizeof(r->error), "Mask exceeds native resource budget");
        return NULL;
    }
    surface = SDL_CreateSurface(w, h, SDL_PIXELFORMAT_RGBA32);
    if (!surface) {
        fail(r, "Allocate mask");
        return NULL;
    }
    for (y = 0; y < h; ++y) {
        unsigned char *row = (unsigned char *)surface->pixels + y * surface->pitch;
        for (x = 0; x < w; ++x) {
            double outer, inner = 0, d;
            if (blur >= 0) {
                double bw = w - 2.0 * blur, bh = h - 2.0 * blur;
                d = rounded_distance(x + 0.5 - blur, y + 0.5 - blur, bw, bh, min2(radius, min2(bw, bh) / 2));
                outer = blur ? clamp((0.5 * erfc(3 * (d / blur) / sqrt(2.0)) - tail) / (1 - 2 * tail), 0, 1)
                             : (d <= 0 ? 1 : 0);
            } else {
                d = rounded_distance(x + 0.5, y + 0.5, w, h, radius);
                outer = clamp(0.5 - d, 0, 1);
                if (border > 0 && border < min2(w, h) / 2)
                    inner = clamp(0.5 - rounded_distance(x + 0.5 - border, y + 0.5 - border, w - 2 * border,
                                                         h - 2 * border, max2(0, radius - border)),
                                  0, 1);
            }
            row[4 * x] = row[4 * x + 1] = row[4 * x + 2] = 255;
            row[4 * x + 3] = (unsigned char)rnd(255 * max2(0, outer - inner));
        }
    }
    return cache_add(r, key, blur >= 0 ? 4 : 0, surface);
}
static double span_inset(double local, double height, double edge, double corner) {
    double dy = max2(0, max2(edge + corner - local, local - (height - edge - corner)));
    return edge + corner - sqrt(max2(0, corner * corner - dy * dy));
}
static int rounded_spans(PXRenderer *r, SDL_Rect p, double radius, double border, Color tint) {
    int row, top = (int)max2(0, p.y), bottom = (int)min2(r->pixel_h, p.y + p.h);
    if (r->clip_enabled) {
        top = (int)max2(top, r->clip.y);
        bottom = (int)min2(bottom, r->clip.y + r->clip.h);
    }
    for (row = top; row < bottom; ++row) {
        double local = row - p.y + .5;
        int outer = (int)ceil(span_inset(local, p.h, 0, radius) - .5);
        if (border > 0 && border < local && local < p.h - border) {
            int inner = (int)ceil(span_inset(local, p.h, border, max2(0, radius - border)) - .5),
                span = (int)max2(0, inner - outer);
            if (!fill_pixels(r, p.x + outer, row, span, 1, tint) ||
                !fill_pixels(r, p.x + p.w - inner, row, span, 1, tint))
                return 0;
        } else if (!fill_pixels(r, p.x + outer, row, p.w - 2 * outer, 1, tint))
            return 0;
    }
    return 1;
}
static int rounded_layer(PXRenderer *r, SDL_Rect p, double radius, double border, Color tint) {
    int w = p.w, h = p.h, compact = 0;
    double edges[4] = {0};
    Texture *t;
    if (!tint.set || !tint.a || !visible_pixels(r, p))
        return 1;
    radius = clamp(radius, 0, min2(w, h) / 2);
    border = clamp(border, 0, min2(w, h) / 2);
    if ((uint64_t)w * h >= 65536) {
        int edge = (int)ceil(max2(radius, border) + 1);
        edges[0] = edges[2] = min2(w / 2, edge);
        edges[1] = edges[3] = min2(h / 2, edge);
        w = (int)min2(w, edges[0] + edges[2] + 1);
        h = (int)min2(h, edges[1] + edges[3] + 1);
        compact = w != p.w || h != p.h;
    }
    /* Match the reference renderer's bounded fallback for enormous corners. */
    {
        double edge = ceil(max2(radius, border) + 1);
        int circle = w == h && radius == w / 2.0;
        if (w > 4096 || h > 4096 || (uint64_t)w * h > 4u * 1024u * 1024u ||
            (!circle && min2(w / 2, edge) * min2(h / 2, edge) > 65536))
            return rounded_spans(r, p, radius, border, tint);
    }
    t = mask_texture(r, w, h, radius, border, -1);
    if (!t)
        return 0;
    if (compact)
        return nine_texture(r, t, p, edges, tint);
    return copy_texture(r, t, (Box){p.x, p.y, p.w, p.h}, NULL, tint);
}
static int draw_rect(PXRenderer *r, Box b, Color fill, double radius, Color border, double width) {
    SDL_Rect p = pixel_box(r, b);
    int thickness;
    if (!visible_pixels(r, p))
        return 1;
    if (radius > 0)
        return rounded_layer(r, p, radius * r->scale, 0, fill) &&
               (!border.set || width <= 0 || rounded_layer(r, p, radius * r->scale, width * r->scale, border));
    if (!fill_pixels(r, p.x, p.y, p.w, p.h, fill))
        return 0;
    if (!border.set || width <= 0)
        return 1;
    thickness = (int)min2(max2(1, rnd(width * r->scale)), ceil(min2(p.w, p.h) / 2));
    if (thickness * 2 >= min2(p.w, p.h))
        return fill_pixels(r, p.x, p.y, p.w, p.h, border);
    return fill_pixels(r, p.x, p.y, p.w, thickness, border) &&
           fill_pixels(r, p.x, p.y + p.h - thickness, p.w, thickness, border) &&
           fill_pixels(r, p.x, p.y + thickness, thickness, p.h - 2 * thickness, border) &&
           fill_pixels(r, p.x + p.w - thickness, p.y + thickness, thickness, p.h - 2 * thickness, border);
}
static Color mix(Color a, Color b, double f, int premultiplied) {
    Color c = b;
    double alpha = a.a + (b.a - a.a) * f;
    if (premultiplied && alpha > 0) {
        c.r = (unsigned char)rnd((a.r * a.a * (1 - f) + b.r * b.a * f) / alpha);
        c.g = (unsigned char)rnd((a.g * a.a * (1 - f) + b.g * b.a * f) / alpha);
        c.b = (unsigned char)rnd((a.b * a.a * (1 - f) + b.b * b.a * f) / alpha);
    } else {
        c.r = (unsigned char)rnd(a.r + (b.r - a.r) * f);
        c.g = (unsigned char)rnd(a.g + (b.g - a.g) * f);
        c.b = (unsigned char)rnd(a.b + (b.b - a.b) * f);
    }
    c.a = (unsigned char)rnd(alpha);
    c.set = a.set || b.set;
    return c;
}
static int gradient(PXRenderer *r, Box b, Color first, Color last, int horizontal, double radius, double border) {
    SDL_Rect previous = r->clip, clip = r->clip_enabled ? r->clip : (SDL_Rect){0, 0, r->pixel_w, r->pixel_h};
    int was_clipped = r->clip_enabled;
    double start = horizontal ? b.x : b.y, length = horizontal ? b.w : b.h;
    int low = (int)floor(start * r->scale), high = (int)ceil((start + length) * r->scale),
        count = (int)min2(96, high - low), i;
    if (!visible_pixels(r, pixel_box(r, b)) || count <= 0)
        return 1;
    for (i = 0; i < count; ++i) {
        int a = low + (high - low) * i / count, z = low + (high - low) * (i + 1) / count;
        SDL_Rect band = clip;
        Color c = mix(first, last, (double)i / max2(1, count - 1), 0), empty = {0};
        if (horizontal) {
            int left = (int)max2(clip.x, a), right = (int)min2(clip.x + clip.w, z);
            band.x = left;
            band.w = (int)max2(0, right - left);
        } else {
            int top = (int)max2(clip.y, a), bottom = (int)min2(clip.y + clip.h, z);
            band.y = top;
            band.h = (int)max2(0, bottom - top);
        }
        if (band.w > 0 && band.h > 0 &&
            (!set_clip(r, &band) || !draw_rect(r, b, border > 0 ? empty : c, radius, border > 0 ? c : empty, border))) {
            set_clip(r, was_clipped ? &previous : NULL);
            return 0;
        }
    }
    return set_clip(r, was_clipped ? &previous : NULL);
}
static int line(PXRenderer *r, double x1, double y1, double x2, double y2, Color c, double width) {
    double thickness, length, dx, dy;
    SDL_Vertex v[4];
    int indices[6] = {0, 1, 2, 0, 2, 3};
    int i;
    double xy[8];
    if (width <= 0 || !c.set || !c.a)
        return 1;
    x1 *= r->scale;
    y1 *= r->scale;
    x2 *= r->scale;
    y2 *= r->scale;
    thickness = max2(1, width * r->scale);
    if (!draw_color(r, c))
        return 0;
    if (thickness <= 1)
        return SDL_RenderLine(r->renderer, (float)x1, (float)y1, (float)x2, (float)y2) || fail(r, "Draw line");
    length = hypot(x2 - x1, y2 - y1);
    if (!length)
        return fill_pixels(r, rnd(x1 - thickness / 2), rnd(y1 - thickness / 2), ceil(thickness), ceil(thickness), c);
    dx = (y2 - y1) * thickness / (2 * length);
    dy = (x1 - x2) * thickness / (2 * length);
    xy[0] = x1 + dx;
    xy[1] = y1 + dy;
    xy[2] = x2 + dx;
    xy[3] = y2 + dy;
    xy[4] = x2 - dx;
    xy[5] = y2 - dy;
    xy[6] = x1 - dx;
    xy[7] = y1 - dy;
    for (i = 0; i < 4; ++i) {
        v[i].position = (SDL_FPoint){(float)xy[2 * i], (float)xy[2 * i + 1]};
        v[i].color = (SDL_FColor){c.r / 255.0f, c.g / 255.0f, c.b / 255.0f, c.a / 255.0f};
        v[i].tex_coord = (SDL_FPoint){0, 0};
    }
    return SDL_RenderGeometry(r->renderer, NULL, v, 4, indices, 6) || fail(r, "Draw thick line");
}
static TTF_Font *font(PXRenderer *r, double size, int mono) {
    int pixels = (int)max2(1, rnd(size * r->scale)), i, slot = -1;
    uint64_t oldest = UINT64_MAX;
    char path[4096];
    if (pixels > 2048) {
        snprintf(r->error, sizeof(r->error), "Font pixel size exceeds 2048");
        return NULL;
    }
    for (i = 0; i < PX_FONT_ENTRIES; ++i) {
        if (r->fonts[i].font && r->fonts[i].pixels == pixels && r->fonts[i].mono == mono) {
            r->fonts[i].used = ++r->clock;
            return r->fonts[i].font;
        }
        if (!r->fonts[i].font) {
            slot = i;
            oldest = 0;
        } else if (oldest && r->fonts[i].used < oldest) {
            slot = i;
            oldest = r->fonts[i].used;
        }
    }
    if (slot < 0)
        slot = 0;
    if (r->fonts[slot].font)
        TTF_CloseFont(r->fonts[slot].font);
    snprintf(path, sizeof(path), "%s/%s", r->font_dir, mono ? "DejaVuSansMono.ttf" : "DejaVuSans.ttf");
    r->fonts[slot].font = TTF_OpenFont(path, (float)pixels);
    r->fonts[slot].pixels = pixels;
    r->fonts[slot].mono = mono;
    r->fonts[slot].used = ++r->clock;
    if (!r->fonts[slot].font)
        fail(r, "Open bundled font");
    return r->fonts[slot].font;
}
static int measure(PXRenderer *r, const char *text, double size, int mono, double *width, double *height) {
    TTF_Font *f = font(r, size, mono);
    const char *start = text, *end;
    int max_width = 0, lines = 0;
    if (!f)
        return 0;
    do {
        int w = 0, h = 0;
        end = strchr(start, '\n');
        if (!end)
            end = start + strlen(start);
        if (end > start && !TTF_GetStringSize(f, start, (size_t)(end - start), &w, &h))
            return fail(r, "Measure text");
        if (w > max_width)
            max_width = w;
        ++lines;
        if (!*end)
            break;
        start = end + 1;
    } while (1);
    *width = max_width / r->scale;
    *height = (double)TTF_GetFontHeight(f) * lines / r->scale;
    return 1;
}
static int clipped_text_line(PXRenderer *r, TTF_Font *f, const char *text, size_t length, int x, int y, int w, int h,
                             int pixels, int mono, Color tint) {
    SDL_Rect bounds = {x, y, w, h}, viewport = {0, 0, r->pixel_w, r->pixel_h}, visible;
    TTF_TextEngine *engine;
    TTF_Text *shaped;
    SDL_Surface *surface;
    Texture *t;
    char prefix[128], *key;
    int prefix_length, drawn;
    if (r->clip_enabled && !SDL_GetRectIntersection(&viewport, &r->clip, &viewport))
        return 1;
    if (!SDL_GetRectIntersection(&bounds, &viewport, &visible))
        return 1;
    /* Cache the source-relative pixel crop, not just the string: scrolling
       and retained damage can expose a different part of the same line. */
    prefix_length = snprintf(prefix, sizeof(prefix), "text-crop:%d:%d:%d:%d:%d:%d:", pixels, mono, visible.x - x,
                             visible.y - y, visible.w, visible.h);
    key = (char *)malloc((size_t)prefix_length + length + 1);
    if (!key)
        return 0;
    memcpy(key, prefix, (size_t)prefix_length);
    memcpy(key + prefix_length, text, length);
    key[prefix_length + length] = '\0';
    t = cache_find(r, key);
    if (!t) {
        if ((uint64_t)visible.w * visible.h * 4 + strlen(key) > 8u * 1024u * 1024u) {
            free(key);
            snprintf(r->error, sizeof(r->error), "Text texture exceeds native resource budget");
            return 0;
        }
        /* Shape the complete line, preserving its kerning, graphemes and
           fractional glyph positions, but rasterize only visible pixels.
           Slicing the UTF-8 string and subtracting integer text measurements
           loses that positioning information for accented/proportional text. */
        engine = TTF_CreateSurfaceTextEngine();
        shaped = engine ? TTF_CreateText(engine, f, text, length) : NULL;
        surface = shaped ? SDL_CreateSurface(visible.w, visible.h, SDL_PIXELFORMAT_ARGB8888) : NULL;
        /* Keep white RGB under zero alpha, as RenderText_Blended does. Blitting
           glyphs over transparent black would apply coverage twice on upload. */
        drawn = surface && SDL_FillSurfaceRect(surface, NULL, SDL_MapSurfaceRGBA(surface, 255, 255, 255, 0)) &&
                TTF_DrawSurfaceText(shaped, x - visible.x, y - visible.y, surface);
        if (shaped)
            TTF_DestroyText(shaped);
        if (engine)
            TTF_DestroySurfaceTextEngine(engine);
        if (!drawn) {
            SDL_DestroySurface(surface);
            free(key);
            return fail(r, "Render clipped text");
        }
        t = cache_add(r, key, 1, surface);
    }
    free(key);
    return t && copy_texture(r, t, (Box){visible.x, visible.y, visible.w, visible.h}, NULL, tint);
}

static int text_line(PXRenderer *r, const char *text, size_t length, double x, double y, Color tint, double size,
                     int mono) {
    char prefix[64], *key;
    int prefix_length;
    Texture *t;
    TTF_Font *f;
    if (!length || !tint.set || !tint.a)
        return 1;
    prefix_length = snprintf(prefix, sizeof(prefix), "text:%d:%d:", rnd(size * r->scale), mono);
    key = (char *)malloc((size_t)prefix_length + length + 1);
    if (!key)
        return 0;
    memcpy(key, prefix, (size_t)prefix_length);
    memcpy(key + prefix_length, text, length);
    key[prefix_length + length] = '\0';
    t = cache_find(r, key);
    if (!t) {
        int w = 0, h = 0;
        f = font(r, size, mono);
        if (!f) {
            free(key);
            return 0;
        }
        if (!TTF_GetStringSize(f, text, length, &w, &h)) {
            free(key);
            return fail(r, "Measure text texture");
        }
        if (w <= 0 || h <= 0) {
            free(key);
            return 1;
        }
        if (w > (r->clip_enabled ? r->clip.w : r->pixel_w) || h > (r->clip_enabled ? r->clip.h : r->pixel_h)) {
            free(key);
            return clipped_text_line(r, f, text, length, rnd(x * r->scale), rnd(y * r->scale), w, h,
                                     rnd(size * r->scale), mono, tint);
        }
        if ((uint64_t)w * h * 4 + strlen(key) > 8u * 1024u * 1024u) {
            free(key);
            snprintf(r->error, sizeof(r->error), "Text texture exceeds native resource budget");
            return 0;
        }
        t = cache_add(r, key, 1, TTF_RenderText_Blended(f, text, length, (SDL_Color){255, 255, 255, 255}));
    }
    free(key);
    if (!t)
        return 0;
    return copy_texture(r, t, (Box){rnd(x * r->scale), rnd(y * r->scale), t->w, t->h}, NULL, tint);
}
static int draw_text(PXRenderer *r, const char *text, double x, double y, Color tint, double size, int mono) {
    const char *start = text, *end;
    double width, height;
    if (!measure(r, "", size, mono, &width, &height))
        return 0;
    do {
        end = strchr(start, '\n');
        if (!end)
            end = start + strlen(start);
        if (!text_line(r, start, (size_t)(end - start), x, y, tint, size, mono))
            return 0;
        if (!*end)
            break;
        y += height;
        start = end + 1;
    } while (1);
    return 1;
}
static unsigned char *decode_base64(const char *s, size_t *length) {
    size_t n = strlen(s), out = 0, i;
    unsigned char *data;
    unsigned int accumulator = 0, bits = 0;
    if (n > 4u * ((PX_MAX_IMAGE_BYTES + 2u) / 3u) || n % 4)
        return NULL;
    data = (unsigned char *)malloc(n / 4 * 3 + 1);
    if (!data)
        return NULL;
    for (i = 0; i < n; ++i) {
        int v;
        char c = s[i];
        if (c == '=') {
            while (i < n)
                if (s[i++] != '=') {
                    free(data);
                    return NULL;
                }
            break;
        }
        v = c >= 'A' && c <= 'Z'   ? c - 'A'
            : c >= 'a' && c <= 'z' ? c - 'a' + 26
            : c >= '0' && c <= '9' ? c - '0' + 52
            : c == '+'             ? 62
            : c == '/'             ? 63
                                   : -1;
        if (v < 0) {
            free(data);
            return NULL;
        }
        accumulator = (accumulator << 6) | (unsigned)v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            data[out++] = (unsigned char)(accumulator >> bits);
        }
    }
    *length = out;
    return data;
}
static SDL_Surface *bounded_image(PXRenderer *r, const char *source, size_t key_bytes) {
    unsigned char *bytes = NULL;
    size_t size = 0;
    uint32_t width, height;
    SDL_Surface *surface = NULL;
    SDL_IOStream *io;
    if (!strncmp(source, "data:image/png;base64,", 22)) {
        bytes = decode_base64(source + 22, &size);
        if (!bytes) {
            snprintf(r->error, sizeof(r->error), "Invalid PNG data URI or encoded input exceeds 8 MiB");
            return NULL;
        }
    } else {
        Sint64 length;
        io = SDL_IOFromFile(source, "rb");
        if (!io) {
            fail(r, "Open PNG image");
            return NULL;
        }
        length = SDL_GetIOSize(io);
        if (length < 0 || length > PX_MAX_IMAGE_BYTES) {
            SDL_CloseIO(io);
            snprintf(r->error, sizeof(r->error), "PNG encoded input exceeds 8 MiB or has unknown size");
            return NULL;
        }
        /* Decode the exact bounded bytes inspected below, even if the file changes. */
        bytes = (unsigned char *)malloc((size_t)length + 1);
        if (bytes)
            size = SDL_ReadIO(io, bytes, (size_t)length + 1);
        SDL_CloseIO(io);
        if (!bytes || size != (size_t)length) {
            free(bytes);
            snprintf(r->error, sizeof(r->error), "Cannot read a stable bounded PNG image");
            return NULL;
        }
    }
    if (size > PX_MAX_IMAGE_BYTES) {
        snprintf(r->error, sizeof(r->error), "PNG encoded input exceeds 8 MiB");
        goto done;
    }
    if (size < 33 || memcmp(bytes, "\211PNG\r\n\032\n\0\0\0\rIHDR", 16)) {
        snprintf(r->error, sizeof(r->error), "Native window images require PNG data");
        goto done;
    }
    width = ((uint32_t)bytes[16] << 24) | ((uint32_t)bytes[17] << 16) | ((uint32_t)bytes[18] << 8) | bytes[19];
    height = ((uint32_t)bytes[20] << 24) | ((uint32_t)bytes[21] << 16) | ((uint32_t)bytes[22] << 8) | bytes[23];
    if (!width || !height || width > (PX_IMAGE_BUDGET - key_bytes) / 4 / height) {
        snprintf(r->error, sizeof(r->error), "PNG dimensions exceed the 32 MiB native image budget");
        goto done;
    }
    io = SDL_IOFromConstMem(bytes, size);
    if (io) {
        ++r->image_decode_attempts;
        surface = IMG_LoadPNG_IO(io);
        SDL_CloseIO(io);
    }
    if (!surface)
        fail(r, "Decode PNG image");
done:
    free(bytes);
    return surface;
}
static void image_key(const char *source, char key[80]) {
    const unsigned char *p = (const unsigned char *)source;
    uint64_t a = UINT64_C(14695981039346656037), b = UINT64_C(7809847782465536322);
    size_t n = 0;
    for (; *p; ++p, ++n) {
        a = (a ^ *p) * UINT64_C(1099511628211);
        b = (b + *p) * UINT64_C(14029467366897019727);
        b ^= b >> 29;
    }
    snprintf(key, 80, "image:%016llx:%016llx:%llu", (unsigned long long)a, (unsigned long long)b,
             (unsigned long long)n);
}
static void image_failed(PXRenderer *r, const char key[80]) {
    int i, slot = 0;
    uint64_t oldest = UINT64_MAX;
    cJSON *event;
    char message[600];
    for (i = 0; i < 128; ++i)
        if (r->failed_used[i] < oldest) {
            slot = i;
            oldest = r->failed_used[i];
        }
    snprintf(r->failed_images[slot], 80, "%s", key);
    r->failed_used[slot] = ++r->clock;
    if (!r->resource_events)
        r->resource_events = cJSON_CreateArray();
    if (cJSON_GetArraySize(r->resource_events) >= 32)
        cJSON_DeleteItemFromArray(r->resource_events, 0);
    event = cJSON_CreateObject();
    cJSON_AddStringToObject(event, "kind", "resource_error");
    snprintf(message, sizeof(message), "sdl3 image unavailable: %.512s", r->error[0] ? r->error : SDL_GetError());
    cJSON_AddStringToObject(event, "text", message);
    cJSON_AddItemToArray(r->resource_events, event);
    r->error[0] = '\0';
}
static Texture *image_texture(PXRenderer *r, const char *source) {
    char key[80];
    Texture *t;
    SDL_Surface *surface;
    int i;
    for (t = r->textures; t; t = t->next)
        if (t->source_identity == source) {
            t->used = ++r->clock;
            return t;
        }
    image_key(source, key);
    t = cache_find(r, key);
    if (t) {
        t->source_identity = source;
        return t;
    }
    for (i = 0; i < 128; ++i)
        if (r->failed_used[i] && !strcmp(key, r->failed_images[i])) {
            r->failed_used[i] = ++r->clock;
            return NULL;
        }
    surface = bounded_image(r, source, strlen(key) + 1);
    if (!surface) {
        image_failed(r, key);
        return NULL;
    }
    t = cache_add(r, key, 2, surface);
    if (!t)
        image_failed(r, key);
    else
        t->source_identity = source;
    return t;
}
static int draw_image(PXRenderer *r, const char *source, Box b, Color tint, const char *fit, const double *edges) {
    SDL_Rect p = pixel_box(r, b);
    Texture *t;
    Box d = {p.x, p.y, p.w, p.h}, crop = {0};
    if (!visible_pixels(r, p))
        return 1;
    t = image_texture(r, source);
    if (!t)
        return 1;
    if (edges)
        return nine_texture(r, t, p, edges, tint);
    if (strcmp(fit, "contain") == 0) {
        double scale = min2(b.w / t->w, b.h / t->h);
        Box fitted = {b.x + (b.w - t->w * scale) / 2, b.y + (b.h - t->h * scale) / 2, t->w * scale, t->h * scale};
        p = pixel_box(r, fitted);
        d = (Box){p.x, p.y, p.w, p.h};
    } else if (strcmp(fit, "cover") == 0) {
        double scale = max2(b.w / t->w, b.h / t->h);
        crop.w = b.w / scale;
        crop.h = b.h / scale;
        crop.x = (t->w - crop.w) / 2;
        crop.y = (t->h - crop.h) / 2;
    }
    return copy_texture(r, t, d, strcmp(fit, "cover") == 0 ? &crop : NULL, tint);
}

static Style style_json(const cJSON *j) {
    Style s = {0};
    const char *v;
#define READ_COLOR(name) s.name = json_color(get(j, #name))
    READ_COLOR(fill);
    READ_COLOR(foreground);
    READ_COLOR(border);
    READ_COLOR(fill_end);
    READ_COLOR(border_end);
    READ_COLOR(bevel_light);
    READ_COLOR(bevel_dark);
    READ_COLOR(highlight);
    READ_COLOR(inner_border);
    READ_COLOR(glow);
    READ_COLOR(pattern_color);
    READ_COLOR(shadow);
#undef READ_COLOR
    s.radius = num(j, "radius", 0);
    s.border_width = num(j, "border_width", 0);
    s.glow_width = num(j, "glow_width", 0);
    s.bevel_width = num(j, "bevel_width", 2);
    s.pattern_spacing = num(j, "pattern_spacing", 6);
    s.shadow_blur = num(j, "shadow_blur", 0);
    s.shadow_x = num(j, "shadow_x", 0);
    s.shadow_y = num(j, "shadow_y", 0);
    s.horizontal = strcmp(string(get(j, "gradient_axis"), "vertical"), "horizontal") == 0;
    v = string(get(j, "bevel"), "none");
    s.bevel = strcmp(v, "raised") == 0 ? 1 : strcmp(v, "sunken") == 0 ? 2 : 0;
    v = string(get(j, "pattern"), "none");
    s.pattern = strcmp(v, "scanlines") == 0 ? 1 : strcmp(v, "grid") == 0 ? 2 : 0;
    return s;
}
static Color faded_color(Color c) {
    c.a = 0;
    return c;
}
static Color blend_optional(Color a, Color b, double f) {
    if (!a.set)
        a = faded_color(b);
    if (!b.set)
        b = faded_color(a);
    return mix(a, b, f, 1);
}
static Style blend_style(Style a, Style b, double f) {
    Style s = b;
#define BLEND_COLOR(name) s.name = blend_optional(a.name, b.name, f)
    BLEND_COLOR(fill);
    BLEND_COLOR(foreground);
    BLEND_COLOR(border);
    BLEND_COLOR(bevel_light);
    BLEND_COLOR(bevel_dark);
    BLEND_COLOR(highlight);
    BLEND_COLOR(inner_border);
    BLEND_COLOR(glow);
    BLEND_COLOR(pattern_color);
    BLEND_COLOR(shadow);
#undef BLEND_COLOR
    s.fill_end = blend_optional(a.fill_end.set ? a.fill_end : a.fill, b.fill_end.set ? b.fill_end : b.fill, f);
    s.border_end =
        blend_optional(a.border_end.set ? a.border_end : a.border, b.border_end.set ? b.border_end : b.border, f);
#define BLEND_NUMBER(name) s.name = a.name + (b.name - a.name) * f
    BLEND_NUMBER(radius);
    BLEND_NUMBER(border_width);
    BLEND_NUMBER(glow_width);
    BLEND_NUMBER(bevel_width);
    BLEND_NUMBER(shadow_blur);
    BLEND_NUMBER(shadow_x);
    BLEND_NUMBER(shadow_y);
#undef BLEND_NUMBER
    s.shadow_blur = rnd(s.shadow_blur);
    return s;
}
static cJSON *style_value(Style s) {
    cJSON *j = cJSON_CreateObject();
    char value[10];
#define WRITE_COLOR(name)                                                                                              \
    if (s.name.set) {                                                                                                  \
        snprintf(value, sizeof(value), "#%02x%02x%02x%02x", s.name.r, s.name.g, s.name.b, s.name.a);                   \
        cJSON_AddStringToObject(j, #name, value);                                                                      \
    }
    WRITE_COLOR(fill);
    WRITE_COLOR(foreground);
    WRITE_COLOR(border);
    WRITE_COLOR(fill_end);
    WRITE_COLOR(border_end);
    WRITE_COLOR(bevel_light);
    WRITE_COLOR(bevel_dark);
    WRITE_COLOR(highlight);
    WRITE_COLOR(inner_border);
    WRITE_COLOR(glow);
    WRITE_COLOR(pattern_color);
    WRITE_COLOR(shadow);
#undef WRITE_COLOR
#define WRITE_NUMBER(name) cJSON_AddNumberToObject(j, #name, s.name)
    WRITE_NUMBER(radius);
    WRITE_NUMBER(border_width);
    WRITE_NUMBER(glow_width);
    WRITE_NUMBER(bevel_width);
    WRITE_NUMBER(pattern_spacing);
    WRITE_NUMBER(shadow_blur);
    WRITE_NUMBER(shadow_x);
    WRITE_NUMBER(shadow_y);
#undef WRITE_NUMBER
    cJSON_AddStringToObject(j, "gradient_axis", s.horizontal ? "horizontal" : "vertical");
    cJSON_AddStringToObject(j, "bevel", s.bevel == 1 ? "raised" : s.bevel == 2 ? "sunken" : "none");
    cJSON_AddStringToObject(j, "pattern", s.pattern == 1 ? "scanlines" : s.pattern == 2 ? "grid" : "none");
    return j;
}
static double transition_fraction(PXRenderer *r, double now) {
    double f;
    if (!r->previous || r->transition_duration <= 0)
        return 1;
    f = clamp((now - r->transition_started) / r->transition_duration, 0, 1);
    return rnd((1 - pow(1 - f, 3)) * 64) / 64.0;
}
static int matching_style(const cJSON *a, const cJSON *b) {
    const char *op = string(at(a, 0), "");
    return a && b && (strcmp(op, "styled_rect") == 0 || strcmp(op, "marker") == 0) &&
           strcmp(op, string(at(b, 0), "")) == 0;
}
static Box blend_box(Box a, Box b, double f) {
    return (Box){a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f, a.w + (b.w - a.w) * f, a.h + (b.h - a.h) * f};
}
static unsigned color_channels(const cJSON *c) {
    const char *op = string(at(c, 0), "");
    if (!strcmp(op, "text") || !strcmp(op, "caret") || !strcmp(op, "image_nine"))
        return 1u << 4;
    if (!strcmp(op, "line"))
        return 1u << 5;
    if (!strcmp(op, "lines") || !strcmp(op, "segments") || !strcmp(op, "focus_ring"))
        return 1u << 2;
    if (!strcmp(op, "image"))
        return 1u << 3;
    if (!strcmp(op, "rect"))
        return (1u << 2) | (1u << 4);
    if (!strcmp(op, "gradient_rect"))
        return (1u << 2) | (1u << 3);
    return 0;
}
static unsigned matching_colors(const cJSON *a, const cJSON *b) {
    unsigned channels = color_channels(a);
    const cJSON *x, *y;
    int index = 0;
    if (!channels || !a || !b || cJSON_GetArraySize(a) != cJSON_GetArraySize(b))
        return 0;
    x = a->child;
    y = b->child;
    for (; x && y; x = x->next, y = y->next, ++index)
        if (!(channels & (1u << index)) && !cJSON_Compare(x, y, 1))
            return 0;
    return channels;
}
static Color command_color(const cJSON *c, const cJSON *old, int index, double fraction, unsigned channels) {
    Color target = json_color(at(c, index));
    return channels & (1u << index) ? blend_optional(json_color(at(old, index)), target, fraction) : target;
}
static cJSON *color_value(Color c) {
    char text[10];
    if (!c.set)
        return cJSON_CreateString("");
    snprintf(text, sizeof(text), "#%02x%02x%02x%02x", c.r, c.g, c.b, c.a);
    return cJSON_CreateString(text);
}
static int shadow(PXRenderer *r, Box b, double radius, Color tint, double blur, double ox, double oy,
                  const cJSON *effect_clip) {
    SDL_Rect saved = r->clip, p;
    int was_clipped = r->clip_enabled, pixels = (int)ceil(blur * r->scale), w, h, compact = 0, ok;
    double edges[4] = {0};
    Texture *t;
    if (!tint.set || !tint.a)
        return 1;
    p.x = rnd((b.x + ox) * r->scale) - pixels;
    p.y = rnd((b.y + oy) * r->scale) - pixels;
    p.w = rnd(b.w * r->scale) + 2 * pixels;
    p.h = rnd(b.h * r->scale) + 2 * pixels;
    if (effect_clip && !clip_box(r, effect_clip))
        return 0;
    if (!visible_pixels(r, p)) {
        set_clip(r, was_clipped ? &saved : NULL);
        return 1;
    }
    w = p.w;
    h = p.h;
    radius = rnd(radius * r->scale);
    if ((uint64_t)w * h >= 65536) {
        int edge = (int)ceil(pixels + max2(radius, pixels));
        edges[0] = edges[2] = min2(w / 2, edge);
        edges[1] = edges[3] = min2(h / 2, edge);
        w = (int)min2(w, edges[0] + edges[2] + 1);
        h = (int)min2(h, edges[1] + edges[3] + 1);
        compact = w != p.w || h != p.h;
    }
    {
        double edge = ceil(pixels + max2(radius, pixels));
        if (w > 4096 || h > 4096 || (uint64_t)w * h > 4u * 1024u * 1024u ||
            min2(w / 2, edge) * min2(h / 2, edge) > 65536) {
            int i, steps = (int)min2(32, max2(1, pixels * 2)), bw = p.w - 2 * pixels, bh = p.h - 2 * pixels,
                   x = p.x + pixels, y = p.y + pixels;
            Color empty = {0};
            double tail = .5 * erfc(3 / sqrt(2.0));
            ok = 1;
            for (i = 0; i < steps && ok; ++i) {
                int spread = pixels - pixels * 2 * i / steps, next = pixels - pixels * 2 * (i + 1) / steps;
                double d = 1 - 2 * (i + .5) / steps;
                Color layer = tint;
                layer.a =
                    (unsigned char)rnd(tint.a * clamp((.5 * erfc(3 * d / sqrt(2.0)) - tail) / (1 - 2 * tail), 0, 1));
                if (spread > next && min2(bw, bh) + spread * 2 > 0)
                    ok = draw_rect(r,
                                   (Box){(x - spread) / r->scale, (y - spread) / r->scale, (bw + spread * 2) / r->scale,
                                         (bh + spread * 2) / r->scale},
                                   empty, max2(0, radius + spread) / r->scale, layer, (spread - next) / r->scale);
            }
            b.x += ox;
            b.y += oy;
            b = inset(b, pixels / r->scale);
            if (ok && b.w > 0 && b.h > 0)
                ok = draw_rect(r, b, tint, max2(0, radius - pixels) / r->scale, empty, 0);
            return set_clip(r, was_clipped ? &saved : NULL) && ok;
        }
    }
    t = mask_texture(r, w, h, radius, 0, pixels);
    ok =
        t && (compact ? nine_texture(r, t, p, edges, tint) : copy_texture(r, t, (Box){p.x, p.y, p.w, p.h}, NULL, tint));
    return set_clip(r, was_clipped ? &saved : NULL) && ok;
}
static int surface(PXRenderer *r, Box b, Style s, const cJSON *effect_clip) {
    Color empty = {0}, light, dark, glow;
    double radius = min2(s.radius, min2(b.w, b.h) / 2), width = s.border_width;
    double glow_width = min2(s.glow_width, min2(b.w, b.h) / 2);
    Box inside = inset(b, max2(max2(width, radius), 2));
    int i, axis;
    if (b.w <= 0 || b.h <= 0)
        return 1;
    if (!shadow(r, b, radius, s.shadow, s.shadow_blur, s.shadow_x, s.shadow_y, effect_clip))
        return 0;
    glow = s.glow;
    glow.a = (unsigned char)rnd(glow.a * .55);
    if (glow_width > 0 && !shadow(r, b, radius, glow, glow_width * 1.5, 0, 0, effect_clip))
        return 0;
    if (s.fill.set) {
        if (s.fill_end.set && (s.fill.r != s.fill_end.r || s.fill.g != s.fill_end.g || s.fill.b != s.fill_end.b ||
                               s.fill.a != s.fill_end.a)) {
            if (!gradient(r, b, s.fill, s.fill_end, s.horizontal, radius, 0))
                return 0;
        } else if (!draw_rect(r, b, s.fill, radius, empty, 0))
            return 0;
    }
    if (s.pattern && s.pattern_color.set) {
        double step = max2(2, s.pattern_spacing);
        for (axis = 0; axis < (s.pattern == 2 ? 2 : 1); ++axis) {
            double length = axis ? inside.w : inside.h;
            int count = (int)min2(128, max2(0, ceil(length / step)));
            for (i = 0; i < count; ++i) {
                Box band = axis ? (Box){inside.x + i * step, inside.y, 1, inside.h}
                                : (Box){inside.x, inside.y + i * step, inside.w, 1};
                if (!draw_rect(r, band, s.pattern_color, 0, empty, 0))
                    return 0;
            }
        }
    }
    glow = s.glow;
    glow.a = (unsigned char)rnd(glow.a * .16);
    if (glow_width > 0)
        for (i = 4; i > 0; --i)
            if (!draw_rect(r, inset(b, width), empty, max2(0, radius - width), glow, glow_width * i / 4.0))
                return 0;
    if (s.border.set && width > 0) {
        if (s.border_end.set && (s.border.r != s.border_end.r || s.border.g != s.border_end.g ||
                                 s.border.b != s.border_end.b || s.border.a != s.border_end.a)) {
            if (!gradient(r, b, s.border, s.border_end, s.horizontal, radius, width))
                return 0;
        } else if (!draw_rect(r, b, empty, radius, s.border, width))
            return 0;
    }
    if (s.inner_border.set && !draw_rect(r, inset(b, width + 3), empty, max2(0, radius - width - 3), s.inner_border, 1))
        return 0;
    if (s.bevel) {
        Box a = inset(b, radius);
        double thickness = min2(s.bevel_width, min2(b.w, b.h) / 2);
        light = s.bevel_light.set ? s.bevel_light : color("#ffffff");
        dark = s.bevel_dark.set ? s.bevel_dark : color("#404040");
        if (s.bevel == 2) {
            Color t = light;
            light = dark;
            dark = t;
        }
        if (!draw_rect(r, (Box){a.x, a.y, a.w, thickness}, light, 0, empty, 0) ||
            !draw_rect(r, (Box){a.x, a.y, thickness, a.h}, light, 0, empty, 0) ||
            !draw_rect(r, (Box){a.x, a.y + a.h - thickness, a.w, thickness}, dark, 0, empty, 0) ||
            !draw_rect(r, (Box){a.x + a.w - thickness, a.y, thickness, a.h}, dark, 0, empty, 0))
            return 0;
    }
    if (s.highlight.set && inside.w > 0 &&
        !draw_rect(r, (Box){inside.x, b.y + max2(2, width), inside.w, 1}, s.highlight, 0, empty, 0))
        return 0;
    return 1;
}
static int marker(PXRenderer *r, Box b, Style s, const char *shape, int checked, const cJSON *effect_clip) {
    double diameter = min2(b.w, b.h), unit = diameter / 24;
    Color empty = {0};
    int circle = strcmp(shape, "circle") == 0;
    if (circle)
        s.radius = diameter / 2;
    if (!surface(r, b, s, effect_clip))
        return 0;
    if (!checked)
        return 1;
    if (circle)
        return draw_rect(r, inset(b, diameter * .3), s.foreground, diameter * .2, empty, 0);
    return line(r, b.x + 4 * unit, b.y + 12 * unit, b.x + 9 * unit, b.y + 17 * unit, s.foreground,
                max2(1, 1.7 * unit)) &&
           line(r, b.x + 9 * unit, b.y + 17 * unit, b.x + 20 * unit, b.y + 6 * unit, s.foreground, max2(1, 1.7 * unit));
}
static int clip_logical(PXRenderer *r, int clear, Box b) {
    SDL_Rect p;
    if (clear)
        return set_clip(r, NULL);
    p.x = (int)floor(b.x * r->scale + 1e-7);
    p.y = (int)floor(b.y * r->scale + 1e-7);
    p.w = b.w > 0 ? (int)ceil((b.x + b.w) * r->scale - 1e-7) - p.x : 0;
    p.h = b.h > 0 ? (int)ceil((b.y + b.h) * r->scale - 1e-7) - p.y : 0;
    return set_clip(r, &p);
}
static int command_op(const char *op) {
    if (!strcmp(op, "begin"))
        return CMD_BEGIN;
    if (!strcmp(op, "clip"))
        return CMD_CLIP;
    if (!strcmp(op, "rect"))
        return CMD_RECT;
    if (!strcmp(op, "text"))
        return CMD_TEXT;
    if (!strcmp(op, "line"))
        return CMD_LINE;
    if (!strcmp(op, "lines") || !strcmp(op, "segments"))
        return CMD_LINES;
    if (!strcmp(op, "image"))
        return CMD_IMAGE;
    if (!strcmp(op, "image_nine"))
        return CMD_IMAGE_NINE;
    if (!strcmp(op, "gradient_rect"))
        return CMD_GRADIENT;
    if (!strcmp(op, "styled_rect"))
        return CMD_STYLED;
    if (!strcmp(op, "marker"))
        return CMD_MARKER;
    if (!strcmp(op, "caret"))
        return CMD_CARET;
    if (!strcmp(op, "focus_ring"))
        return CMD_FOCUS;
    if (!strcmp(op, "sprite"))
        return CMD_SPRITE;
    if (!strcmp(op, "animated_rect"))
        return CMD_ANIMATED;
    return CMD_UNKNOWN;
}
static void free_draw_commands(DrawCommand *commands, size_t count) {
    size_t i;
    if (!commands)
        return;
    for (i = 0; i < count; ++i)
        free(commands[i].points);
    free(commands);
}
static size_t draw_command_bytes(const DrawCommand *commands, size_t count) {
    size_t i, bytes = count * sizeof(*commands);
    for (i = 0; i < count; ++i)
        if (commands[i].points)
            bytes += (size_t)commands[i].point_count * 2 * sizeof(double);
    return bytes;
}
static DrawCommand *decode_commands(const cJSON *commands, size_t *count) {
    const cJSON *c;
    DrawCommand *decoded;
    size_t n = 0, i = 0;
    *count = 0;
    if (!cJSON_IsArray(commands))
        return NULL;
    n = (size_t)cJSON_GetArraySize(commands);
    if (!n)
        return NULL;
    decoded = (DrawCommand *)calloc(n, sizeof(*decoded));
    if (!decoded)
        return NULL;
    cJSON_ArrayForEach(c, commands) {
        DrawCommand *cmd = &decoded[i++];
        const cJSON *anim;
        cmd->op = command_op(string(at(c, 0), ""));
        switch (cmd->op) {
        case CMD_BEGIN:
            cmd->fill = json_color(at(c, 1));
            break;
        case CMD_CLIP:
            if (!at(c, 1) || cJSON_IsNull(at(c, 1)))
                cmd->flag = 1;
            else
                cmd->box = box_json(at(c, 1));
            break;
        case CMD_RECT:
            cmd->box = box_json(at(c, 1));
            cmd->fill = json_color(at(c, 2));
            cmd->a = number(at(c, 3), 0);
            cmd->stroke = json_color(at(c, 4));
            cmd->b = number(at(c, 5), 0);
            break;
        case CMD_TEXT:
            cmd->text = string(at(c, 1), "");
            cmd->a = number(at(c, 2), 0);
            cmd->b = number(at(c, 3), 0);
            cmd->fill = json_color(at(c, 4));
            cmd->c = number(at(c, 5), 14);
            cmd->flag = boolean(at(c, 6), 0);
            break;
        case CMD_LINE:
            cmd->a = number(at(c, 1), 0);
            cmd->b = number(at(c, 2), 0);
            cmd->from = number(at(c, 3), 0);
            cmd->to = number(at(c, 4), 0);
            cmd->fill = json_color(at(c, 5));
            cmd->c = number(at(c, 6), 1);
            break;
        case CMD_LINES: {
            const cJSON *points = at(c, 1), *p;
            int total = cJSON_GetArraySize(points), at_point = 0;
            cmd->fill = json_color(at(c, 2));
            cmd->a = number(at(c, 3), 1);
            cmd->point_count = total;
            cmd->flag = !strcmp(string(at(c, 0), ""), "segments");
            if (total > 0) {
                cmd->points = (double *)malloc((size_t)total * 2 * sizeof(double));
                if (!cmd->points) {
                    free_draw_commands(decoded, i);
                    return NULL;
                }
                for (p = points->child; p; p = p->next) {
                    cmd->points[at_point++] = number(at(p, 0), 0);
                    cmd->points[at_point++] = number(at(p, 1), 0);
                }
            }
            break;
        }
        case CMD_IMAGE:
            cmd->text = string(at(c, 1), "");
            cmd->box = box_json(at(c, 2));
            cmd->fill = json_color(at(c, 3));
            cmd->extra = string(at(c, 4), "stretch");
            break;
        case CMD_IMAGE_NINE: {
            int edge;
            cmd->text = string(at(c, 1), "");
            cmd->box = box_json(at(c, 2));
            for (edge = 0; edge < 4; ++edge)
                cmd->edges[edge] = number(at(at(c, 3), edge), 0);
            cmd->fill = json_color(at(c, 4));
            break;
        }
        case CMD_GRADIENT:
            cmd->box = box_json(at(c, 1));
            cmd->fill = json_color(at(c, 2));
            cmd->stroke = json_color(at(c, 3));
            cmd->flag = strcmp(string(at(c, 4), "vertical"), "horizontal") == 0;
            cmd->a = number(at(c, 5), 0);
            cmd->b = number(at(c, 6), 0);
            break;
        case CMD_STYLED:
            cmd->box = box_json(at(c, 1));
            cmd->style = style_json(at(c, 2));
            cmd->clip = at(c, 3);
            break;
        case CMD_MARKER:
            cmd->box = box_json(at(c, 1));
            cmd->style = style_json(at(c, 2));
            cmd->extra = string(at(c, 3), "square");
            cmd->flag = boolean(at(c, 4), 0);
            cmd->clip = at(c, 5);
            break;
        case CMD_CARET:
            cmd->a = number(at(c, 1), 0);
            cmd->b = number(at(c, 2), 0);
            cmd->c = number(at(c, 3), 0);
            cmd->fill = json_color(at(c, 4));
            break;
        case CMD_FOCUS:
            cmd->box = box_json(at(c, 1));
            cmd->fill = json_color(at(c, 2));
            cmd->a = number(at(c, 3), 0);
            break;
        case CMD_SPRITE:
            cmd->text = string(at(c, 1), "");
            cmd->box = box_json(at(c, 2));
            cmd->frame_w = (int)number(at(c, 3), 1);
            cmd->frame_h = (int)number(at(c, 4), 1);
            cmd->frame_count = (int)number(at(c, 5), 1);
            cmd->a = number(at(c, 6), 1);
            cmd->flag = boolean(at(c, 7), 0);
            cmd->fill = json_color(at(c, 8));
            break;
        case CMD_ANIMATED:
            anim = at(c, 6);
            cmd->box = box_json(at(c, 1));
            cmd->fill = json_color(at(c, 2));
            cmd->a = number(at(c, 3), 0);
            cmd->stroke = json_color(at(c, 4));
            cmd->b = number(at(c, 5), 0);
            cmd->extra = string(get(anim, "property"), "x");
            cmd->from = num(anim, "from", 0);
            cmd->to = num(anim, "to", 1);
            cmd->duration = num(anim, "duration", 1);
            cmd->flag = boolean(get(anim, "loop"), 0);
            cmd->flag2 = boolean(get(anim, "yoyo"), 0);
            break;
        default:
            break;
        }
    }
    *count = n;
    return decoded;
}
static int replay_decoded(PXRenderer *r, const DrawCommand *cmd, double now, double epoch) {
    Color empty = {0};
    switch (cmd->op) {
    case CMD_BEGIN:
        return set_clip(r, NULL) && draw_color(r, cmd->fill) &&
               (SDL_RenderClear(r->renderer) || fail(r, "Clear renderer"));
    case CMD_CLIP:
        return clip_logical(r, cmd->flag, cmd->box);
    case CMD_RECT:
        return draw_rect(r, cmd->box, cmd->fill, cmd->a, cmd->stroke, cmd->b);
    case CMD_TEXT:
        return draw_text(r, cmd->text ? cmd->text : "", cmd->a, cmd->b, cmd->fill, cmd->c, cmd->flag);
    case CMD_LINE:
        return line(r, cmd->a, cmd->b, cmd->from, cmd->to, cmd->fill, cmd->c);
    case CMD_LINES: {
        int i;
        for (i = 0; i + 1 < cmd->point_count; i += cmd->flag ? 2 : 1)
            if (!line(r, cmd->points[i * 2], cmd->points[i * 2 + 1], cmd->points[(i + 1) * 2],
                      cmd->points[(i + 1) * 2 + 1], cmd->fill, cmd->a))
                return 0;
        return 1;
    }
    case CMD_IMAGE:
        return draw_image(r, cmd->text ? cmd->text : "", cmd->box, cmd->fill, cmd->extra ? cmd->extra : "stretch",
                          NULL);
    case CMD_IMAGE_NINE:
        return draw_image(r, cmd->text ? cmd->text : "", cmd->box, cmd->fill, "stretch", cmd->edges);
    case CMD_GRADIENT:
        return gradient(r, cmd->box, cmd->fill, cmd->stroke, cmd->flag, cmd->a, cmd->b);
    case CMD_STYLED:
        return surface(r, cmd->box, cmd->style, cmd->clip);
    case CMD_MARKER:
        return marker(r, cmd->box, cmd->style, cmd->extra ? cmd->extra : "square", cmd->flag, cmd->clip);
    case CMD_CARET:
        return line(r, cmd->a, cmd->b, cmd->a, cmd->b + cmd->c, cmd->fill, 1);
    case CMD_FOCUS:
        return draw_rect(r, inset(cmd->box, 1), empty, max2(0, cmd->a - 1), cmd->fill, 2);
    case CMD_SPRITE: {
        int columns, index;
        double elapsed = r->reduce_motion ? 0 : max2(0, now - epoch), frame = floor(elapsed * cmd->a);
        Texture *t = image_texture(r, cmd->text ? cmd->text : "");
        SDL_Rect destination;
        Box source;
        if (!t)
            return 1;
        columns = cmd->frame_w > 0 ? t->w / cmd->frame_w : 0;
        if (!columns || cmd->frame_h > t->h || cmd->frame_count > columns * (t->h / cmd->frame_h)) {
            snprintf(r->error, sizeof(r->error), "Sprite frames exceed image bounds");
            return 0;
        }
        index = cmd->flag ? (int)fmod(frame, cmd->frame_count) : (int)min2(cmd->frame_count - 1, frame);
        source = (Box){(index % columns) * cmd->frame_w, (index / columns) * cmd->frame_h, cmd->frame_w, cmd->frame_h};
        destination = pixel_box(r, cmd->box);
        return copy_texture(r, t, (Box){destination.x, destination.y, destination.w, destination.h}, &source,
                            cmd->fill);
    }
    case CMD_ANIMATED: {
        double elapsed = max2(0, now - epoch), cycles = elapsed / cmd->duration, progress;
        Color fill = cmd->fill, border = cmd->stroke;
        Box b = cmd->box;
        if (r->reduce_motion)
            progress = 1;
        else if (!cmd->flag && cycles >= (cmd->flag2 ? 2 : 1))
            progress = cmd->flag2 ? 0 : 1;
        else {
            progress = fmod(cycles, cmd->flag2 ? 2 : 1);
            if (progress > 1)
                progress = 2 - progress;
        }
        progress = cmd->from + (cmd->to - cmd->from) * progress;
        if (!strcmp(cmd->extra ? cmd->extra : "x", "x"))
            b.x = progress;
        else if (cmd->extra && !strcmp(cmd->extra, "y"))
            b.y = progress;
        else {
            progress = clamp(progress, 0, 1);
            fill.a = (unsigned char)rnd(fill.a * progress);
            border.a = (unsigned char)rnd(border.a * progress);
        }
        return draw_rect(r, b, fill, cmd->a, border, cmd->b);
    }
    default:
        return 1;
    }
}

/* Validation is deliberately separate from committing a scene. Malformed edits
   cannot partially replace a retained frame, even when the socket remains open. */
static int valid_number(const cJSON *j, double low, double high) {
    return cJSON_IsNumber(j) && isfinite(j->valuedouble) && j->valuedouble >= low && j->valuedouble <= high;
}
static int valid_string(const cJSON *j) {
    return cJSON_IsString(j) && j->valuestring && strlen(j->valuestring) <= PX_MAX_STRING;
}

static int valid_color(const cJSON *j, int optional) {
    return (optional && (!j || cJSON_IsNull(j))) ||
           (valid_string(j) && ((optional && !j->valuestring[0]) || color(j->valuestring).set));
}
static int valid_box(const cJSON *j, int optional) {
    return (optional && (!j || cJSON_IsNull(j))) ||
           (cJSON_IsArray(j) && cJSON_GetArraySize(j) == 4 && valid_number(at(j, 0), -1000000, 1000000) &&
            valid_number(at(j, 1), -1000000, 1000000) && valid_number(at(j, 2), 0, 1000000) &&
            valid_number(at(j, 3), 0, 1000000));
}
static int valid_style(const cJSON *j) {
    const cJSON *v;
    const char *colors[] = {"fill",          "foreground", "border",    "fill_end",     "border_end",
                            "bevel_light",   "bevel_dark", "highlight", "inner_border", "glow",
                            "pattern_color", "shadow",     NULL};
    int i;
    if (!cJSON_IsObject(j))
        return 0;
    for (i = 0; colors[i]; ++i)
        if (!valid_color(get(j, colors[i]), 1))
            return 0;
    cJSON_ArrayForEach(v, j) {
        const char *key = v->string;
        int known = 0;
        if (cJSON_IsNull(v))
            continue;
        for (i = 0; colors[i]; ++i)
            if (strcmp(key, colors[i]) == 0)
                known = 1;
        if (known)
            continue;
        if (strcmp(key, "font_family") == 0) {
            const char *s = string(v, "");
            if (strcmp(s, "ui") && strcmp(s, "mono"))
                return 0;
        } else if (strcmp(key, "gradient_axis") == 0) {
            const char *s = string(v, "");
            if (strcmp(s, "vertical") && strcmp(s, "horizontal"))
                return 0;
        } else if (strcmp(key, "bevel") == 0) {
            const char *s = string(v, "");
            if (strcmp(s, "none") && strcmp(s, "raised") && strcmp(s, "sunken"))
                return 0;
        } else if (strcmp(key, "pattern") == 0) {
            const char *s = string(v, "");
            if (strcmp(s, "none") && strcmp(s, "scanlines") && strcmp(s, "grid"))
                return 0;
        } else if (strcmp(key, "shadow_x") == 0 || strcmp(key, "shadow_y") == 0) {
            if (!valid_number(v, -24, 24))
                return 0;
        } else if (strcmp(key, "shadow_blur") == 0) {
            if (!valid_number(v, 0, 24))
                return 0;
        } else if (strcmp(key, "bevel_width") == 0 || strcmp(key, "glow_width") == 0) {
            if (!valid_number(v, 0, 16))
                return 0;
        } else if (strcmp(key, "pattern_spacing") == 0) {
            if (!valid_number(v, 2, 1000000))
                return 0;
        } else if (strcmp(key, "font_size") == 0) {
            if (!valid_number(v, 0.01, 2048))
                return 0;
        } else if (strcmp(key, "radius") == 0 || strcmp(key, "border_width") == 0 || strcmp(key, "padding") == 0) {
            if (!valid_number(v, 0, 1000000))
                return 0;
        } else
            return 0;
    }
    return 1;
}
static int validate_command(const cJSON *c) {
    const char *op = string(at(c, 0), "");
    const cJSON *v;
    int n = cJSON_GetArraySize(c), i;
#define ARGNUM(i) valid_number(at(c, i), -1000000, 1000000)
    if (!cJSON_IsArray(c))
        return 0;
    if (strcmp(op, "begin") == 0)
        return n == 2 && valid_color(at(c, 1), 0);
    if (strcmp(op, "clip") == 0)
        return n == 2 && valid_box(at(c, 1), 1);
    if (strcmp(op, "rect") == 0)
        return n == 6 && valid_box(at(c, 1), 0) && valid_color(at(c, 2), 1) && valid_number(at(c, 3), 0, 1000000) &&
               valid_color(at(c, 4), 1) && valid_number(at(c, 5), 0, 1000000);
    if (strcmp(op, "text") == 0)
        return n == 7 && valid_string(at(c, 1)) && ARGNUM(2) && ARGNUM(3) && valid_color(at(c, 4), 0) &&
               valid_number(at(c, 5), .01, 2048) && cJSON_IsBool(at(c, 6));
    if (strcmp(op, "line") == 0)
        return n == 7 && ARGNUM(1) && ARGNUM(2) && ARGNUM(3) && ARGNUM(4) && valid_color(at(c, 5), 0) &&
               valid_number(at(c, 6), 0, 1000000);
    if (!strcmp(op, "lines") || !strcmp(op, "segments")) {
        if (n != 4 || !cJSON_IsArray(at(c, 1)) || cJSON_GetArraySize(at(c, 1)) > 200000 || !valid_color(at(c, 2), 0) ||
            !valid_number(at(c, 3), 0, 1000000) || (!strcmp(op, "segments") && cJSON_GetArraySize(at(c, 1)) % 2))
            return 0;
        cJSON_ArrayForEach(v, at(c, 1)) if (!cJSON_IsArray(v) || cJSON_GetArraySize(v) != 2 ||
                                            !valid_number(at(v, 0), -1000000, 1000000) ||
                                            !valid_number(at(v, 1), -1000000, 1000000)) return 0;
        return 1;
    }
    if (strcmp(op, "image") == 0) {
        const char *fit = string(at(c, 4), "");
        return n == 5 && valid_string(at(c, 1)) && valid_box(at(c, 2), 0) && valid_color(at(c, 3), 0) &&
               (!strcmp(fit, "stretch") || !strcmp(fit, "contain") || !strcmp(fit, "cover"));
    }
    if (strcmp(op, "image_nine") == 0) {
        if (n != 5 || !valid_string(at(c, 1)) || !valid_box(at(c, 2), 0) || !cJSON_IsArray(at(c, 3)) ||
            cJSON_GetArraySize(at(c, 3)) != 4 || !valid_color(at(c, 4), 0))
            return 0;
        for (i = 0; i < 4; ++i)
            if (!valid_number(at(at(c, 3), i), 0, 8192))
                return 0;
        return 1;
    }
    if (strcmp(op, "gradient_rect") == 0) {
        const char *axis = string(at(c, 4), "");
        return n == 7 && valid_box(at(c, 1), 0) && valid_color(at(c, 2), 0) && valid_color(at(c, 3), 0) &&
               (!strcmp(axis, "horizontal") || !strcmp(axis, "vertical")) && valid_number(at(c, 5), 0, 1000000) &&
               valid_number(at(c, 6), 0, 1000000);
    }
    if (strcmp(op, "styled_rect") == 0)
        return (n == 3 || n == 4) && valid_box(at(c, 1), 0) && valid_style(at(c, 2)) && valid_box(at(c, 3), 1);
    if (strcmp(op, "marker") == 0) {
        const char *shape = string(at(c, 3), "");
        return (n == 5 || n == 6) && valid_box(at(c, 1), 0) && valid_style(at(c, 2)) &&
               (!strcmp(shape, "square") || !strcmp(shape, "circle")) && cJSON_IsBool(at(c, 4)) &&
               valid_box(at(c, 5), 1);
    }
    if (strcmp(op, "caret") == 0)
        return n == 5 && ARGNUM(1) && ARGNUM(2) && valid_number(at(c, 3), 0, 1000000) && valid_color(at(c, 4), 0);
    if (strcmp(op, "focus_ring") == 0)
        return n == 4 && valid_box(at(c, 1), 0) && valid_color(at(c, 2), 0) && valid_number(at(c, 3), 0, 1000000);
    /* Sprite sheets are native time-driven, without Python timer callbacks. */
    if (strcmp(op, "sprite") == 0)
        return n == 9 && valid_string(at(c, 1)) && valid_box(at(c, 2), 0) && valid_number(at(c, 3), 1, 8192) &&
               valid_number(at(c, 4), 1, 8192) && valid_number(at(c, 5), 1, 4096) &&
               valid_number(at(c, 6), .01, 1000) && cJSON_IsBool(at(c, 7)) && valid_color(at(c, 8), 0) &&
               floor(number(at(c, 3), 0)) == number(at(c, 3), 0) && floor(number(at(c, 4), 0)) == number(at(c, 4), 0) &&
               floor(number(at(c, 5), 0)) == number(at(c, 5), 0);
    if (strcmp(op, "animated_rect") == 0) {
        const cJSON *a = at(c, 6);
        const char *property = string(get(a, "property"), "");
        return n == 7 && valid_box(at(c, 1), 0) && valid_color(at(c, 2), 1) && valid_number(at(c, 3), 0, 1000000) &&
               valid_color(at(c, 4), 1) && valid_number(at(c, 5), 0, 1000000) && cJSON_IsObject(a) &&
               (!strcmp(property, "x") || !strcmp(property, "y") || !strcmp(property, "opacity")) &&
               valid_number(get(a, "from"), -1000000, 1000000) && valid_number(get(a, "to"), -1000000, 1000000) &&
               valid_number(get(a, "duration"), .001, 86400) && (!get(a, "loop") || cJSON_IsBool(get(a, "loop"))) &&
               (!get(a, "yoyo") || cJSON_IsBool(get(a, "yoyo")));
    }
    return 0;
#undef ARGNUM
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
        if (bytes > PX_SCENE_BUDGET)
            return PX_SCENE_BUDGET + 1;
    }
    return bytes;
}
static int boxes_intersect(Box a, Box b) {
    return a.w > 0 && a.h > 0 && b.w > 0 && b.h > 0 && a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h &&
           b.y < a.y + a.h;
}
static Box box_union(Box a, Box b) {
    double x, y;
    if (a.w <= 0 || a.h <= 0)
        return b;
    if (b.w <= 0 || b.h <= 0)
        return a;
    x = min2(a.x, b.x);
    y = min2(a.y, b.y);
    return (Box){x, y, max2(a.x + a.w, b.x + b.w) - x, max2(a.y + a.h, b.y + b.h) - y};
}
static Box box_intersection(Box a, Box b) {
    double x = max2(a.x, b.x), y = max2(a.y, b.y);
    return (Box){x, y, max2(0, min2(a.x + a.w, b.x + b.w) - x), max2(0, min2(a.y + a.h, b.y + b.h) - y)};
}
static void dirty_add(PXRenderer *r, Box b) {
    size_t i;
    if (b.w <= 0 || b.h <= 0)
        return;
    /* Round outward and include a physical pixel for raster edge coverage. */
    b = inset(b, -1 / r->scale);
    b = box_intersection(b, (Box){0, 0, r->width, r->height});
    if (b.w <= 0 || b.h <= 0)
        return;
    for (i = 0; i < r->dirty_count;) {
        if (boxes_intersect(b, r->dirty[i])) {
            b = box_union(b, r->dirty[i]);
            r->dirty[i] = r->dirty[--r->dirty_count];
            i = 0;
        } else
            ++i;
    }
    if (r->dirty_count == 64) {
        for (i = 0; i < r->dirty_count; ++i)
            b = box_union(b, r->dirty[i]);
        r->dirty_count = 0;
    }
    r->dirty[r->dirty_count++] = b;
}
static void segment_free(Segment *s) {
    if (!s)
        return;
    free(s->id);
    free(s->epochs);
    free_draw_commands(s->decoded, s->decoded_count);
    cJSON_Delete(s->commands);
    cJSON_Delete(s->previous);
    free(s);
}
static void segments_clear(PXRenderer *r) {
    size_t i;
    for (i = 0; i < r->segment_count; ++i)
        segment_free(r->segments[i]);
    free(r->segments);
    r->segments = NULL;
    r->segment_count = 0;
    free(r->segment_table);
    r->segment_table = NULL;
    r->segment_capacity = 0;
    r->segment_index_bytes = 0;
    r->segmented = 0;
    r->dirty_count = 0;
    r->retained_scene_bytes = 0;
    r->footprints_stale = 0;
    r->active_segments = NULL;
    r->active_segment_count = 0;
}
static size_t segment_hash(const char *id) {
    size_t hash = (size_t)2166136261u;
    while (*id) {
        hash ^= (unsigned char)*id++;
        hash *= (size_t)16777619u;
    }
    return hash;
}
static SegmentSlot *segment_slot(SegmentSlot *table, size_t capacity, const char *id) {
    size_t at;
    if (!table || !capacity)
        return NULL;
    at = segment_hash(id) & (capacity - 1);
    while (table[at].segment && strcmp(table[at].segment->id, id))
        at = (at + 1) & (capacity - 1);
    return table + at;
}
static size_t segment_capacity(size_t count) {
    size_t capacity = 8;
    while (capacity < count * 2 + 1)
        capacity *= 2;
    return capacity;
}
static double segment_fraction(Segment *s, double now) {
    double f;
    if (!s->previous || s->transition_duration <= 0)
        return 1;
    f = clamp((now - s->transition_started) / s->transition_duration, 0, 1);
    return rnd((1 - pow(1 - f, 3)) * 64) / 64.0;
}
static int segment_active(Segment *s, double now) {
    (void)now;
    /* Keep one final redraw pending so a missed deadline cannot strand the
       penultimate sprite or transition frame in the scene cache. */
    return s->looping || s->animation_pending || s->previous != NULL;
}
static void segment_deactivate(PXRenderer *r, Segment *s) {
    if (!s || !s->active)
        return;
    if (s->active_previous)
        s->active_previous->active_next = s->active_next;
    else
        r->active_segments = s->active_next;
    if (s->active_next)
        s->active_next->active_previous = s->active_previous;
    s->active_next = s->active_previous = NULL;
    s->active = 0;
    --r->active_segment_count;
}
static void segment_activate(PXRenderer *r, Segment *s) {
    if (s->active || !segment_active(s, 0))
        return;
    s->active_next = r->active_segments;
    if (r->active_segments)
        r->active_segments->active_previous = s;
    r->active_segments = s;
    s->active = 1;
    ++r->active_segment_count;
}
/* Only touched command lists are inspected. Include native animation endpoints
   even when the caller supplied the control's current bounds. */
static Box command_bounds(PXRenderer *r, const cJSON *c, Box clip) {
    const char *op = string(at(c, 0), "");
    const cJSON *v;
    Box b = {0};
    if (!strcmp(op, "text")) {
        double w, h;
        if (measure(r, string(at(c, 1), ""), number(at(c, 5), 16), boolean(at(c, 6), 0), &w, &h))
            b = inset((Box){number(at(c, 2), 0), number(at(c, 3), 0), w, h},
                      -max2(2 / r->scale, number(at(c, 5), 16) * .15));
        else
            b = clip;
    } else if (!strcmp(op, "line")) {
        double x = number(at(c, 1), 0), y = number(at(c, 2), 0), xx = number(at(c, 3), 0), yy = number(at(c, 4), 0),
               pad = max2(1 / r->scale, number(at(c, 6), 0));
        b = (Box){min2(x, xx) - pad, min2(y, yy) - pad, fabs(xx - x) + 2 * pad, fabs(yy - y) + 2 * pad};
    } else if (!strcmp(op, "lines") || !strcmp(op, "segments")) {
        double pad = max2(1 / r->scale, number(at(c, 3), 0));
        cJSON_ArrayForEach(v, at(c, 1)) b =
            box_union(b, (Box){number(at(v, 0), 0) - pad, number(at(v, 1), 0) - pad, 2 * pad, 2 * pad});
    } else if (!strcmp(op, "caret"))
        b = (Box){number(at(c, 1), 0) - 1 / r->scale, number(at(c, 2), 0), 2 / r->scale, number(at(c, 3), 0)};
    else if (!strcmp(op, "image") || !strcmp(op, "image_nine") || !strcmp(op, "sprite"))
        b = box_json(at(c, 2));
    else if (strcmp(op, "clip")) {
        b = box_json(at(c, 1));
        if (!strcmp(op, "animated_rect")) {
            Box a = b, z = b;
            const cJSON *animation = at(c, 6);
            const char *property = string(get(animation, "property"), "");
            if (!strcmp(property, "x")) {
                a.x = num(animation, "from", 0);
                z.x = num(animation, "to", 1);
            }
            if (!strcmp(property, "y")) {
                a.y = num(animation, "from", 0);
                z.y = num(animation, "to", 1);
            }
            b = box_union(b, box_union(a, z));
        }
        if (!strcmp(op, "styled_rect") || !strcmp(op, "marker")) {
            Style style = style_json(at(c, 2));
            const cJSON *effect_clip = at(c, !strcmp(op, "marker") ? 5 : 3);
            double pad = max2(style.shadow_blur, style.glow_width * 1.5);
            Box effect = inset(b, -pad);
            effect = box_union(effect, (Box){effect.x + style.shadow_x, effect.y + style.shadow_y, effect.w, effect.h});
            /* Match shadow(): an omitted argument inherits the body clip,
               whereas explicit JSON null removes it for the effect. */
            if (!effect_clip)
                effect = box_intersection(effect, clip);
            else if (!cJSON_IsNull(effect_clip))
                effect = box_intersection(effect, box_json(effect_clip));
            return box_union(box_intersection(b, clip), effect);
        }
    }
    return box_intersection(b, clip);
}
static Box implicit_clip(void) {
    /* Unclipped retained commands outside today's viewport may become visible
       after resizing. Metadata must retain their logical footprint. */
    return (Box){-3000000, -3000000, 6000000, 6000000};
}
static Box commands_bounds(PXRenderer *r, const cJSON *commands) {
    const cJSON *c;
    Box bounds = {0}, clip = implicit_clip();
    cJSON_ArrayForEach(c, commands) {
        if (!strcmp(string(at(c, 0), ""), "clip"))
            clip = cJSON_IsNull(at(c, 1)) ? implicit_clip() : box_json(at(c, 1));
        else
            bounds = box_union(bounds, command_bounds(r, c, clip));
    }
    return bounds;
}
static Segment *segment_create(PXRenderer *r, const cJSON *entry, Segment *old, double now) {
    const cJSON *commands = get(entry, "commands"), *c, *prior;
    Segment *s = (Segment *)calloc(1, sizeof(*s));
    Box clip = implicit_clip();
    size_t index = 0;
    int changed = 0;
    if (!s)
        return NULL;
    s->id = copy_string(string(get(entry, "id"), ""));
    /* Bounds supplied by the control can include its entire ancestor clip.
       Retain the actual painted footprint; clip-only segments paint no pixels. */
    s->bounds = (Box){0, 0, 0, 0};
    s->count = (size_t)cJSON_GetArraySize(commands);
    s->commands = cJSON_Duplicate(commands, 1);
    s->epochs = (double *)calloc(s->count ? s->count : 1, sizeof(double));
    if (!s->id || !s->commands || !s->epochs)
        goto failed;
    s->bytes =
        sizeof(*s) + strlen(s->id) + 1 + (s->count ? s->count : 1) * sizeof(double) + json_heap_bytes(s->commands);
    prior = old ? old->commands->child : NULL;
    s->animation_until = now;
    cJSON_ArrayForEach(c, commands) {
        const char *op = string(at(c, 0), "");
        double epoch = prior && cJSON_Compare(c, prior, 1) ? old->epochs[index] : now;
        s->epochs[index++] = epoch;
        if (!strcmp(op, "clip"))
            clip = cJSON_IsNull(at(c, 1)) ? implicit_clip() : box_json(at(c, 1));
        else
            s->bounds = box_union(s->bounds, command_bounds(r, c, clip));
        if (!strcmp(op, "sprite")) {
            s->looping |= boolean(at(c, 7), 0);
            s->animation_until = max2(s->animation_until, epoch + number(at(c, 5), 1) / number(at(c, 6), 1));
        } else if (!strcmp(op, "animated_rect")) {
            const cJSON *a = at(c, 6);
            s->looping |= boolean(get(a, "loop"), 0);
            s->animation_until =
                max2(s->animation_until, epoch + num(a, "duration", 1) * (boolean(get(a, "yoyo"), 0) ? 2 : 1));
        }
        if (prior)
            prior = prior->next;
    }
    s->current_bounds = s->bounds;
    if (old && r->transition_seconds > 0 && !r->reduce_motion) {
        cJSON *p;
        double f = segment_fraction(old, now);
        s->previous = cJSON_Duplicate(old->commands, 1);
        if (!s->previous)
            goto failed;
        prior = old->previous ? old->previous->child : NULL;
        for (p = s->previous->child; p; p = p->next) {
            if (f < 1 && matching_style(p, prior)) {
                Box b = blend_box(box_json(at(prior, 1)), box_json(at(p, 1)), f);
                double values[4] = {b.x, b.y, b.w, b.h};
                if (!cJSON_ReplaceItemInArray(p, 1, cJSON_CreateDoubleArray(values, 4)) ||
                    !cJSON_ReplaceItemInArray(
                        p, 2, style_value(blend_style(style_json(at(prior, 2)), style_json(at(p, 2)), f))))
                    goto failed;
            } else if (f < 1) {
                unsigned channels = matching_colors(p, prior);
                int i;
                for (i = 1; i < 8; ++i)
                    if (channels & (1u << i))
                        if (!cJSON_ReplaceItemInArray(p, i, color_value(command_color(p, prior, i, f, channels))))
                            goto failed;
            }
            if (prior)
                prior = prior->next;
        }
        prior = s->previous->child;
        cJSON_ArrayForEach(c, s->commands) {
            if (prior && !cJSON_Compare(c, prior, 1) && (matching_style(c, prior) || matching_colors(c, prior)))
                changed = 1;
            if (prior)
                prior = prior->next;
        }
        if (!changed) {
            cJSON_Delete(s->previous);
            s->previous = NULL;
        } else
            s->bounds = box_union(s->bounds, old->bounds);
    }
    s->transition_started = now;
    s->transition_duration = r->transition_seconds;
    s->animation_pending = s->looping || s->animation_until > now;
    s->previous_bytes = json_heap_bytes(s->previous);
    if (s->count) {
        s->decoded = decode_commands(s->commands, &s->decoded_count);
        if (!s->decoded)
            goto failed;
        s->bytes += draw_command_bytes(s->decoded, s->decoded_count);
    }
    return s;
failed:
    segment_free(s);
    return NULL;
}
int px_renderer_patch(PXRenderer *r, const cJSON *patch, char *error, int error_size) {
    const cJSON *upsert = get(patch, "upsert"), *remove = get(patch, "remove"), *order = get(patch, "order"), *entry,
                *c, *background = get(patch, "background");
    Segment **next = NULL, **created = NULL, **prior = NULL, **ordered = NULL;
    SegmentSlot *incoming = NULL, *table = NULL, *slot;
    unsigned char *removed = NULL, *seen = NULL;
    size_t count = 0, made = 0, i, j, total, retained, touched = 0, remove_count = 0, unchanged = 0,
           updates = (size_t)cJSON_GetArraySize(upsert), capacity = 0, incoming_capacity, index_bytes;
    double now = px_now();
    int reordered = 0, local;
    Color bg;
    if (!r)
        return 0;
    bg = r->segmented ? r->background : color("#000000");
    total = r->segmented ? r->command_count : 0;
    retained = r->segmented ? r->retained_scene_bytes - r->segment_index_bytes : 0;
    r->error[0] = '\0';
    if (!cJSON_IsObject(patch) || (upsert && !cJSON_IsArray(upsert)) || (remove && !cJSON_IsArray(remove)) ||
        (order && !cJSON_IsArray(order)) || (background && !valid_color(background, 0)) || updates > PX_MAX_COMMANDS ||
        cJSON_GetArraySize(remove) > PX_MAX_COMMANDS || cJSON_GetArraySize(order) > PX_MAX_COMMANDS)
        goto invalid;
    if (background)
        bg = json_color(background);
    local = r->segmented && !order && !cJSON_GetArraySize(remove);
    created = (Segment **)calloc(updates + 1, sizeof(*created));
    prior = (Segment **)calloc(updates + 1, sizeof(*prior));
    incoming_capacity = segment_capacity(updates);
    incoming = (SegmentSlot *)calloc(incoming_capacity, sizeof(*incoming));
    if (!created || !prior || !incoming)
        goto allocation;
    if (cJSON_GetArraySize(remove)) {
        removed = (unsigned char *)calloc(r->segment_count + 1, 1);
        if (!removed)
            goto allocation;
    }
    cJSON_ArrayForEach(entry, remove) {
        if (!valid_string(entry) || !*entry->valuestring || strlen(entry->valuestring) > 256)
            goto invalid;
        slot = segment_slot(r->segment_table, r->segment_capacity, entry->valuestring);
        if (!slot || !slot->segment)
            continue;
        if (removed[slot->position])
            goto invalid;
        removed[slot->position] = 1;
        ++remove_count;
        total -= slot->segment->count;
        retained -= slot->segment->bytes + slot->segment->previous_bytes;
    }
    cJSON_ArrayForEach(entry, upsert) {
        const cJSON *id = get(entry, "id"), *list = get(entry, "commands");
        if (!cJSON_IsObject(entry) || !valid_string(id) || !*id->valuestring || strlen(id->valuestring) > 256 ||
            !valid_box(get(entry, "bounds"), 0) || !cJSON_IsArray(list))
            goto invalid;
        slot = segment_slot(incoming, incoming_capacity, id->valuestring);
        if (slot->segment)
            goto invalid;
        cJSON_ArrayForEach(c, list) if (++touched > PX_MAX_COMMANDS || !validate_command(c) ||
                                        !strcmp(string(at(c, 0), ""), "begin")) goto invalid;
        if (json_heap_bytes(list) > PX_SCENE_BUDGET)
            goto budget;
        slot = segment_slot(r->segment_table, r->segment_capacity, id->valuestring);
        prior[made] = slot ? slot->segment : NULL;
        if (prior[made] && removed && removed[slot->position])
            goto invalid;
        if (!prior[made])
            local = 0;
        if (prior[made] && cJSON_Compare(list, prior[made]->commands, 1)) {
            /* Retargeting an unchanged list must not restart its transition or
               repaint a transparent container's caller-provided bounds. */
            slot = segment_slot(incoming, incoming_capacity, id->valuestring);
            slot->segment = prior[made];
            ++unchanged;
            continue;
        }
        created[made] = segment_create(r, entry, prior[made], now);
        if (!created[made])
            goto allocation;
        slot = segment_slot(incoming, incoming_capacity, id->valuestring);
        slot->segment = created[made];
        slot->position = made;
        total += created[made]->count;
        if (prior[made])
            total -= prior[made]->count;
        retained += created[made]->bytes + created[made]->previous_bytes;
        if (prior[made])
            retained -= prior[made]->bytes + prior[made]->previous_bytes;
        ++made;
    }
    if (total > PX_MAX_COMMANDS)
        goto invalid;
    if (retained > PX_SCENE_BUDGET)
        goto budget;
    if (!local) {
        next = (Segment **)calloc(r->segment_count + updates + 1, sizeof(*next));
        capacity = segment_capacity(r->segment_count + updates);
        table = (SegmentSlot *)calloc(capacity, sizeof(*table));
        if (!next || !table)
            goto allocation;
        for (i = 0; i < r->segment_count; ++i) {
            Segment *s = r->segments[i];
            if (removed && removed[i])
                continue;
            slot = segment_slot(incoming, incoming_capacity, s->id);
            if (slot->segment)
                s = slot->segment;
            next[count] = s;
            slot = segment_slot(table, capacity, s->id);
            slot->segment = s;
            slot->position = count++;
        }
        for (i = 0; i < made; ++i)
            if (!prior[i]) {
                next[count] = created[i];
                slot = segment_slot(table, capacity, created[i]->id);
                slot->segment = created[i];
                slot->position = count++;
            }
        if (count > PX_MAX_COMMANDS)
            goto invalid;
        if (order) {
            size_t last_position = 0;
            int has_previous = 0;
            if ((size_t)cJSON_GetArraySize(order) != count)
                goto invalid;
            ordered = (Segment **)calloc(count + 1, sizeof(*ordered));
            seen = (unsigned char *)calloc(count + 1, 1);
            if (!ordered || !seen)
                goto allocation;
            i = 0;
            cJSON_ArrayForEach(entry, order) {
                SegmentSlot *old_slot;
                if (!valid_string(entry))
                    goto invalid;
                slot = segment_slot(table, capacity, entry->valuestring);
                if (!slot || !slot->segment || seen[slot->position])
                    goto invalid;
                seen[slot->position] = 1;
                ordered[i++] = slot->segment;
                old_slot = segment_slot(r->segment_table, r->segment_capacity, entry->valuestring);
                if (old_slot && old_slot->segment) {
                    if (has_previous && old_slot->position < last_position)
                        reordered = 1;
                    last_position = old_slot->position;
                    has_previous = 1;
                }
            }
            free(next);
            next = ordered;
            ordered = NULL;
            for (i = 0; i < count; ++i)
                segment_slot(table, capacity, next[i]->id)->position = i;
        }
        if (!count) {
            free(next);
            next = NULL;
            free(table);
            table = NULL;
            capacity = 0;
        }
    }
    /* Count the retained index capacities, not the temporary staging arrays. */
    index_bytes = local ? r->segment_index_bytes
                  : count
                      ? (order ? count + 1 : r->segment_count + updates + 1) * sizeof(*next) + capacity * sizeof(*table)
                      : 0;
    retained += index_bytes;
    if (retained > PX_SCENE_BUDGET)
        goto budget;
    /* All validation/allocation finishes before existing pointers are changed.
       Ordinary local edits replace k indexed entries; structural edits copy only
       ordered pointers and rebuild their ID index once, never unchanged JSON. */
    if (!r->segmented || bg.r != r->background.r || bg.g != r->background.g || bg.b != r->background.b ||
        bg.a != r->background.a)
        r->scene_cache_valid = 0;
    if (reordered)
        for (i = 0; i < count; ++i)
            dirty_add(r, next[i]->bounds);
    for (i = 0; i < made; ++i) {
        dirty_add(r, created[i]->bounds);
        if (prior[i])
            dirty_add(r, prior[i]->bounds);
    }
    if (removed)
        for (i = 0; i < r->segment_count; ++i)
            if (removed[i])
                dirty_add(r, r->segments[i]->bounds);
    if (made || remove_count || r->commands) {
        Texture *texture;
        for (texture = r->textures; texture; texture = texture->next)
            texture->source_identity = NULL;
    }
    if (local) {
        for (i = 0; i < made; ++i) {
            slot = segment_slot(r->segment_table, r->segment_capacity, created[i]->id);
            r->segments[slot->position] = created[i];
            slot->segment = created[i];
        }
    } else {
        if (removed)
            for (i = 0; i < r->segment_count; ++i)
                if (removed[i]) {
                    segment_deactivate(r, r->segments[i]);
                    segment_free(r->segments[i]);
                }
        free(r->segments);
        free(r->segment_table);
        r->segments = next;
        next = NULL;
        r->segment_count = count;
        r->segment_table = table;
        table = NULL;
        r->segment_capacity = capacity;
    }
    for (j = 0; j < made; ++j) {
        segment_deactivate(r, prior[j]);
        segment_free(prior[j]);
        segment_activate(r, created[j]);
    }
    cJSON_Delete(r->commands);
    r->commands = NULL;
    free_draw_commands(r->decoded, r->decoded_count);
    r->decoded = NULL;
    r->decoded_count = 0;
    cJSON_Delete(r->previous);
    r->previous = NULL;
    free(r->animation_started);
    r->animation_started = NULL;
    r->segmented = 1;
    r->background = bg;
    r->command_count = total;
    r->segment_index_bytes = index_bytes;
    r->retained_scene_bytes = retained;
    r->has_sprites = 0;
    r->animation_until = 0;
    r->animation_pending = r->animation_settled = 0;
    r->commands_touched += touched;
    r->last_commands_touched = touched;
    r->segments_updated += made;
    r->segments_removed += remove_count;
    ++r->scene_patch_updates;
    r->unchanged_segments += unchanged;
    free(created);
    free(prior);
    free(incoming);
    free(removed);
    free(seen);
    return 1;
invalid:
    snprintf(r->error, sizeof(r->error), "Invalid retained segment patch");
    goto failed;
budget:
    snprintf(r->error, sizeof(r->error), "Retained scene exceeds 64 MiB memory budget");
    goto failed;
allocation:
    snprintf(r->error, sizeof(r->error), "Cannot allocate retained segment patch");
failed:
    for (i = 0; i < made; ++i)
        segment_free(created[i]);
    free(created);
    free(prior);
    free(incoming);
    free(removed);
    free(seen);
    free(next);
    free(table);
    free(ordered);
    output_error(r, error, error_size);
    return 0;
}
void px_renderer_reset_stats(PXRenderer *r) {
    if (!r)
        return;
    r->commands_touched = r->commands_replayed = r->segments_updated = r->segments_removed = 0;
    r->scene_patch_updates = r->last_commands_touched = r->last_commands_replayed = r->scene_cache_hits = 0;
    r->segments_tested = r->last_segments_tested = r->animation_segments_tested = r->unchanged_segments = 0;
    r->image_decode_attempts = 0;
}
void px_renderer_last_work(PXRenderer *r, uint64_t *touched, uint64_t *replayed) {
    if (touched)
        *touched = r ? r->last_commands_touched : 0;
    if (replayed)
        *replayed = r ? r->last_commands_replayed : 0;
}
uint64_t px_renderer_last_segment_tests(PXRenderer *r) {
    return r ? r->last_segments_tested : 0;
}
int px_renderer_commit(PXRenderer *r, const cJSON *commands, char *error, int error_size) {
    const cJSON *c, *old, *prior;
    cJSON *copy, *snapshot = NULL, *p;
    int count = 0, changed = 0, sprites = 0, index = 0;
    double now = px_now(), fraction, animation_end = now, *epochs;
    size_t retained;
    if (!r || !cJSON_IsArray(commands)) {
        if (error && error_size > 0)
            snprintf(error, (size_t)error_size, "commands must be an array");
        return 0;
    }
    cJSON_ArrayForEach(c, commands) {
        const char *op = string(at(c, 0), "");
        if (++count > PX_MAX_COMMANDS || !validate_command(c)) {
            snprintf(r->error, sizeof(r->error), "Invalid draw command at index %d (%s)", count - 1, op);
            output_error(r, error, error_size);
            return 0;
        }
    }
    retained = json_heap_bytes(commands) + (size_t)max2(1, count) * sizeof(double);
    if (retained > PX_SCENE_BUDGET) {
        snprintf(r->error, sizeof(r->error), "Retained scene exceeds 64 MiB memory budget");
        output_error(r, error, error_size);
        return 0;
    }
    copy = cJSON_Duplicate(commands, 1);
    if (!copy) {
        snprintf(r->error, sizeof(r->error), "Cannot allocate retained commands");
        output_error(r, error, error_size);
        return 0;
    }
    epochs = (double *)calloc((size_t)max2(1, count), sizeof(double));
    if (!epochs) {
        cJSON_Delete(copy);
        snprintf(r->error, sizeof(r->error), "Cannot allocate animation timestamps");
        output_error(r, error, error_size);
        return 0;
    }
    old = r->commands ? r->commands->child : NULL;
    cJSON_ArrayForEach(c, commands) {
        const char *op = string(at(c, 0), "");
        double epoch = old && r->animation_started && index < (int)r->command_count && cJSON_Compare(c, old, 1)
                           ? r->animation_started[index]
                           : now;
        epochs[index++] = epoch;
        if (!strcmp(op, "sprite")) {
            if (boolean(at(c, 7), 0))
                sprites = 1;
            animation_end = max2(animation_end, epoch + number(at(c, 5), 1) / number(at(c, 6), 1));
        } else if (!strcmp(op, "animated_rect")) {
            const cJSON *a = at(c, 6);
            if (boolean(get(a, "loop"), 0))
                sprites = 1;
            animation_end = max2(animation_end, epoch + num(a, "duration", 1) * (boolean(get(a, "yoyo"), 0) ? 2 : 1));
        }
        if (old)
            old = old->next;
    }
    if (r->commands && r->transition_seconds > 0 && !r->reduce_motion) {
        snapshot = cJSON_Duplicate(r->commands, 1);
        if (!snapshot) {
            cJSON_Delete(copy);
            free(epochs);
            snprintf(r->error, sizeof(r->error), "Cannot allocate transition snapshot");
            output_error(r, error, error_size);
            return 0;
        }
        fraction = transition_fraction(r, now);
        prior = r->previous ? r->previous->child : NULL;
        for (p = snapshot->child; p; p = p->next) {
            if (fraction < 1 && matching_style(p, prior)) {
                Box b = blend_box(box_json(at(prior, 1)), box_json(at(p, 1)), fraction);
                double xywh[4] = {b.x, b.y, b.w, b.h};
                cJSON *value = style_value(blend_style(style_json(at(prior, 2)), style_json(at(p, 2)), fraction));
                cJSON_ReplaceItemInArray(p, 2, value);
                cJSON_ReplaceItemInArray(p, 1, cJSON_CreateDoubleArray(xywh, 4));
            } else if (fraction < 1) {
                unsigned channels = matching_colors(p, prior);
                int channel;
                for (channel = 1; channel < 8; ++channel)
                    if (channels & (1u << channel))
                        cJSON_ReplaceItemInArray(p, channel,
                                                 color_value(command_color(p, prior, channel, fraction, channels)));
            }
            if (prior)
                prior = prior->next;
        }
        old = snapshot->child;
        for (c = copy->child; c; c = c->next) {
            if ((matching_style(c, old) || matching_colors(c, old)) && !cJSON_Compare(c, old, 1))
                changed = 1;
            if (old)
                old = old->next;
        }
    }
    if (changed)
        retained += json_heap_bytes(snapshot);
    if (retained > PX_SCENE_BUDGET) {
        cJSON_Delete(copy);
        cJSON_Delete(snapshot);
        free(epochs);
        snprintf(r->error, sizeof(r->error), "Retained scene exceeds 64 MiB memory budget");
        output_error(r, error, error_size);
        return 0;
    }
    {
        DrawCommand *decoded = NULL;
        size_t decoded_count = 0;
        if (count > 0) {
            decoded = decode_commands(copy, &decoded_count);
            if (!decoded) {
                cJSON_Delete(copy);
                cJSON_Delete(snapshot);
                free(epochs);
                snprintf(r->error, sizeof(r->error), "Cannot allocate decoded commands");
                output_error(r, error, error_size);
                return 0;
            }
        }
        retained += draw_command_bytes(decoded, decoded_count);
        if (retained > PX_SCENE_BUDGET) {
            free_draw_commands(decoded, decoded_count);
            cJSON_Delete(copy);
            cJSON_Delete(snapshot);
            free(epochs);
            snprintf(r->error, sizeof(r->error), "Retained scene exceeds 64 MiB memory budget");
            output_error(r, error, error_size);
            return 0;
        }
        {
            Texture *t;
            for (t = r->textures; t; t = t->next)
                t->source_identity = NULL;
        }
        free_draw_commands(r->decoded, r->decoded_count);
        r->decoded = decoded;
        r->decoded_count = decoded_count;
    }
    cJSON_Delete(r->commands);
    cJSON_Delete(r->previous);
    r->commands = copy;
    segments_clear(r);
    r->retained_scene_bytes = retained;
    r->commands_touched += (uint64_t)count;
    r->last_commands_touched = (uint64_t)count;
    r->scene_cache_valid = 0;
    r->previous = changed ? snapshot : NULL;
    if (!changed)
        cJSON_Delete(snapshot);
    free(r->animation_started);
    r->animation_started = epochs;
    r->command_count = (size_t)count;
    r->has_sprites = sprites;
    r->animation_until = animation_end;
    r->animation_pending = animation_end > now || changed;
    r->animation_settled = 0;
    r->transition_duration = r->transition_seconds;
    r->transition_started = now;
    r->commit_started = now;
    return 1;
}
int px_renderer_animating(PXRenderer *r) {
    if (r && r->segmented)
        return r->animation_pending || (!r->reduce_motion && r->active_segments != NULL);
    /* A deadline expiring does not display its endpoint. Keep full-frame work
       pending until a settled draw has actually been presented. */
    return r && (r->animation_pending || (!r->reduce_motion && r->has_sprites));
}

static int execute(PXRenderer *r, const cJSON *c, const cJSON *old, double fraction, double now, double epoch) {
    const char *op = string(at(c, 0), "");
    Color empty = {0};
    Box b;
    const cJSON *p;
    unsigned channels = fraction < 1 ? matching_colors(c, old) : 0;
#define COLOR(i) command_color(c, old, i, fraction, channels)
    if (!strcmp(op, "begin"))
        return set_clip(r, NULL) && draw_color(r, COLOR(1)) &&
               (SDL_RenderClear(r->renderer) || fail(r, "Clear renderer"));
    if (!strcmp(op, "clip"))
        return clip_box(r, at(c, 1));
    if (!strcmp(op, "rect"))
        return draw_rect(r, box_json(at(c, 1)), COLOR(2), number(at(c, 3), 0), COLOR(4), number(at(c, 5), 0));
    if (!strcmp(op, "text"))
        return draw_text(r, string(at(c, 1), ""), number(at(c, 2), 0), number(at(c, 3), 0), COLOR(4),
                         number(at(c, 5), 14), boolean(at(c, 6), 0));
    if (!strcmp(op, "line"))
        return line(r, number(at(c, 1), 0), number(at(c, 2), 0), number(at(c, 3), 0), number(at(c, 4), 0), COLOR(5),
                    number(at(c, 6), 1));
    if (!strcmp(op, "lines") || !strcmp(op, "segments")) {
        p = at(c, 1)->child;
        while (p && p->next) {
            if (!line(r, number(at(p, 0), 0), number(at(p, 1), 0), number(at(p->next, 0), 0), number(at(p->next, 1), 0),
                      COLOR(2), number(at(c, 3), 1)))
                return 0;
            p = !strcmp(op, "segments") ? p->next->next : p->next;
        }
        return 1;
    }
    if (!strcmp(op, "image"))
        return draw_image(r, string(at(c, 1), ""), box_json(at(c, 2)), COLOR(3), string(at(c, 4), "stretch"), NULL);
    if (!strcmp(op, "image_nine")) {
        double edges[4];
        int i;
        for (i = 0; i < 4; ++i)
            edges[i] = number(at(at(c, 3), i), 0);
        return draw_image(r, string(at(c, 1), ""), box_json(at(c, 2)), COLOR(4), "stretch", edges);
    }
    if (!strcmp(op, "gradient_rect"))
        return gradient(r, box_json(at(c, 1)), COLOR(2), COLOR(3), !strcmp(string(at(c, 4), "vertical"), "horizontal"),
                        number(at(c, 5), 0), number(at(c, 6), 0));
    if (!strcmp(op, "styled_rect") || !strcmp(op, "marker")) {
        Style s = style_json(at(c, 2));
        b = box_json(at(c, 1));
        if (fraction < 1 && matching_style(c, old)) {
            s = blend_style(style_json(at(old, 2)), s, fraction);
            b = blend_box(box_json(at(old, 1)), b, fraction);
        }
        return !strcmp(op, "marker") ? marker(r, b, s, string(at(c, 3), "square"), boolean(at(c, 4), 0), at(c, 5))
                                     : surface(r, b, s, at(c, 3));
    }
    if (!strcmp(op, "caret"))
        return line(r, number(at(c, 1), 0), number(at(c, 2), 0), number(at(c, 1), 0),
                    number(at(c, 2), 0) + number(at(c, 3), 0), COLOR(4), 1);
    if (!strcmp(op, "focus_ring"))
        return draw_rect(r, inset(box_json(at(c, 1)), 1), empty, max2(0, number(at(c, 3), 0) - 1), COLOR(2), 2);
    if (!strcmp(op, "sprite")) {
        int fw = (int)number(at(c, 3), 1), fh = (int)number(at(c, 4), 1), count = (int)number(at(c, 5), 1), index,
            columns;
        double elapsed = r->reduce_motion ? 0 : max2(0, now - epoch), frame = floor(elapsed * number(at(c, 6), 1));
        Texture *t = image_texture(r, string(at(c, 1), ""));
        SDL_Rect destination = pixel_box(r, box_json(at(c, 2)));
        Box source;
        if (!t)
            return 1;
        columns = t->w / fw;
        if (!columns || fh > t->h || count > columns * (t->h / fh)) {
            snprintf(r->error, sizeof(r->error), "Sprite frames exceed image bounds");
            return 0;
        }
        index = boolean(at(c, 7), 0) ? (int)fmod(frame, count) : (int)min2(count - 1, frame);
        source = (Box){(index % columns) * fw, (index / columns) * fh, fw, fh};
        return copy_texture(r, t, (Box){destination.x, destination.y, destination.w, destination.h}, &source, COLOR(8));
    }
    if (!strcmp(op, "animated_rect")) {
        const cJSON *a = at(c, 6);
        const char *property = string(get(a, "property"), "x");
        double elapsed = max2(0, now - epoch), duration = num(a, "duration", 1), cycles = elapsed / duration, progress;
        int yoyo = boolean(get(a, "yoyo"), 0), loop = boolean(get(a, "loop"), 0);
        Color fill = COLOR(2), border = COLOR(4);
        b = box_json(at(c, 1));
        if (r->reduce_motion)
            progress = 1;
        else if (!loop && cycles >= (yoyo ? 2 : 1))
            progress = yoyo ? 0 : 1;
        else {
            progress = fmod(cycles, yoyo ? 2 : 1);
            if (progress > 1)
                progress = 2 - progress;
        }
        progress = num(a, "from", 0) + (num(a, "to", 1) - num(a, "from", 0)) * progress;
        if (!strcmp(property, "x"))
            b.x = progress;
        else if (!strcmp(property, "y"))
            b.y = progress;
        else {
            progress = clamp(progress, 0, 1);
            fill.a = (unsigned char)rnd(fill.a * progress);
            border.a = (unsigned char)rnd(border.a * progress);
        }
        return draw_rect(r, b, fill, number(at(c, 3), 0), border, number(at(c, 5), 0));
    }
    return 1;
#undef COLOR
}
static int render_commands(PXRenderer *r, char *error, int error_size) {
    const cJSON *c, *old;
    double now, fraction;
    size_t index = 0;
    if (!r)
        return 0;
    r->error[0] = '\0';
    now = px_now();
    fraction = transition_fraction(r, now);
    if (!set_clip(r, NULL)) {
        output_error(r, error, error_size);
        return 0;
    }
    if (!r->commands || !r->commands->child || strcmp(string(at(r->commands->child, 0), ""), "begin") != 0) {
        Color black = color("#000000");
        if (!draw_color(r, black) || !SDL_RenderClear(r->renderer)) {
            fail(r, "Clear renderer");
            output_error(r, error, error_size);
            return 0;
        }
        if (!r->commands || !r->commands->child) {
            r->animation_settled = 1;
            return 1;
        }
    }
    if (fraction >= 1 && !r->previous && r->decoded) {
        for (index = 0; index < r->decoded_count; ++index) {
            double epoch = r->animation_started ? r->animation_started[index] : r->commit_started;
            if (!replay_decoded(r, &r->decoded[index], now, epoch)) {
                output_error(r, error, error_size);
                return 0;
            }
            ++r->commands_replayed;
            ++r->last_commands_replayed;
        }
    } else {
        old = r->previous ? r->previous->child : NULL;
        cJSON_ArrayForEach(c, r->commands) {
            double epoch = r->animation_started ? r->animation_started[index] : r->commit_started;
            if (!execute(r, c, old, fraction, now, epoch)) {
                output_error(r, error, error_size);
                return 0;
            }
            if (old)
                old = old->next;
            ++index;
            ++r->commands_replayed;
            ++r->last_commands_replayed;
        }
    }
    if (fraction >= 1 && r->previous) {
        r->retained_scene_bytes -= json_heap_bytes(r->previous);
        cJSON_Delete(r->previous);
        r->previous = NULL;
    }
    r->animation_settled = r->reduce_motion || (now >= r->animation_until && !r->previous);
    return 1;
}
static Box align_region(PXRenderer *r, Box region, SDL_Rect *pixels) {
    pixels->x = (int)floor(region.x * r->scale);
    pixels->y = (int)floor(region.y * r->scale);
    pixels->w = (int)ceil((region.x + region.w) * r->scale) - pixels->x;
    pixels->h = (int)ceil((region.y + region.h) * r->scale) - pixels->y;
    /* Fractional logical boxes can share an outward-rounded physical pixel. */
    return (Box){pixels->x / r->scale, pixels->y / r->scale, pixels->w / r->scale, pixels->h / r->scale};
}
static int erase_damage(PXRenderer *r) {
    SDL_FRect whole = {0, 0, (float)r->pixel_w, (float)r->pixel_h};
    /* SDL_RenderClear ignores clips. Fill with replacement blending to erase
       old alpha pixels inside the damage rectangle without touching its neighbors. */
    return set_clip(r, NULL) && SDL_SetRenderDrawBlendMode(r->renderer, SDL_BLENDMODE_NONE) &&
           draw_color(r, r->background) && SDL_RenderFillRect(r->renderer, &whole) &&
           SDL_SetRenderDrawBlendMode(r->renderer, SDL_BLENDMODE_BLEND);
}
static int replay_segment(PXRenderer *r, Segment *s, double now) {
    const cJSON *c, *old = s->previous ? s->previous->child : NULL;
    size_t index = 0;
    double fraction = r->reduce_motion ? 1 : segment_fraction(s, now);
    if (!set_clip(r, NULL))
        return 0;
    /* Settled segments replay decoded commands. Transitions still walk JSON so
       color and style blending stay on the existing path. */
    if (fraction >= 1 && !s->previous && s->decoded) {
        for (index = 0; index < s->decoded_count; ++index) {
            if (!replay_decoded(r, &s->decoded[index], now, s->epochs[index]))
                return 0;
            ++r->commands_replayed;
            ++r->last_commands_replayed;
        }
        return 1;
    }
    cJSON_ArrayForEach(c, s->commands) {
        if (!execute(r, c, old, fraction, now, s->epochs[index++]))
            return 0;
        ++r->commands_replayed;
        ++r->last_commands_replayed;
        if (old)
            old = old->next;
    }
    return 1;
}
static int render_regions(PXRenderer *r, const Box *regions, size_t count, double now, char *error, int error_size) {
    size_t i, region_index;
    Box aligned[64];
    SDL_Rect pixels[64];
    if (!count)
        return 1;
    if (count > 64)
        count = 64;
    /* Two regions redraw as their union: one clear and one segment pass. */
    if (count == 2) {
        aligned[0] = box_union(regions[0], regions[1]);
        regions = aligned;
        count = 1;
    }
    for (i = 0; i < count; ++i) {
        aligned[i] = align_region(r, regions[i], &pixels[i]);
        r->damage = pixels[i];
        r->damage_enabled = 1;
        if (!erase_damage(r))
            goto failed;
    }
    for (i = 0; i < r->segment_count; ++i) {
        Segment *s = r->segments[i];
        ++r->segments_tested;
        ++r->last_segments_tested;
        for (region_index = 0; region_index < count; ++region_index) {
            if (!boxes_intersect(s->bounds, aligned[region_index]))
                continue;
            r->damage = pixels[region_index];
            r->damage_enabled = 1;
            if (!replay_segment(r, s, now))
                goto failed;
        }
    }
    r->damage_enabled = 0;
    return set_clip(r, NULL);
failed:
    r->damage_enabled = 0;
    SDL_SetRenderDrawBlendMode(r->renderer, SDL_BLENDMODE_BLEND);
    output_error(r, error, error_size);
    return 0;
}
static int render_segments(PXRenderer *r, Box region, double now, char *error, int error_size) {
    return render_regions(r, &region, 1, now, error, error_size);
}
static int draw_segments(PXRenderer *r, char *error, int error_size) {
    double now = px_now();
    size_t bytes = (size_t)r->pixel_w * (size_t)r->pixel_h * 4, i;
    int ok = 1, cached = r->cache_scene && bytes && bytes <= 32u * 1024u * 1024u;
    if (r->footprints_stale) {
        for (i = 0; i < r->segment_count; ++i) {
            Segment *s = r->segments[i];
            s->current_bounds = commands_bounds(r, s->commands);
            s->bounds = box_union(s->current_bounds, commands_bounds(r, s->previous));
        }
        r->footprints_stale = 0;
    }
    if (cached && !r->scene_cache) {
        r->scene_cache =
            SDL_CreateTexture(r->renderer, SDL_PIXELFORMAT_RGBA8888, SDL_TEXTUREACCESS_TARGET, r->pixel_w, r->pixel_h);
        if (!r->scene_cache || !SDL_SetTextureBlendMode(r->scene_cache, SDL_BLENDMODE_NONE) ||
            !SDL_SetTextureScaleMode(r->scene_cache, SDL_SCALEMODE_NEAREST)) {
            discard_scene_cache(r);
            cached = 0;
        } else
            r->scene_cache_bytes = bytes;
    }
    if (!cached) {
        r->scene_cache_valid = 0;
        ok = render_segments(r, (Box){0, 0, r->width, r->height}, now, error, error_size);
    } else {
        if (!r->scene_cache_valid) {
            r->dirty_count = 0;
            dirty_add(r, (Box){0, 0, r->width, r->height});
        }
        if (!r->reduce_motion) {
            Segment *s;
            for (s = r->active_segments; s; s = s->active_next) {
                ++r->animation_segments_tested;
                dirty_add(r, s->bounds);
            }
        }
        if (r->dirty_count) {
            if (!SDL_SetRenderTarget(r->renderer, r->scene_cache)) {
                discard_scene_cache(r);
                ok = render_segments(r, (Box){0, 0, r->width, r->height}, now, error, error_size);
                goto complete_draw;
            }
            ok = render_regions(r, r->dirty, r->dirty_count, now, error, error_size);
            if (!SDL_SetRenderTarget(r->renderer, NULL)) {
                fail(r, "Restore window render target");
                output_error(r, error, error_size);
                return 0;
            }
            r->scene_cache_valid = ok;
        } else
            ++r->scene_cache_hits;
        if (ok && (!set_clip(r, NULL) || !SDL_RenderTexture(r->renderer, r->scene_cache, NULL, NULL))) {
            fail(r, "Copy retained segments");
            output_error(r, error, error_size);
            ok = 0;
        }
    }
complete_draw:
    if (ok) {
        r->dirty_count = 0;
        /* Offscreen active segments produce no dirty region, but their clocks
           and snapshots still expire. Visible segments have already repainted
           their final state above. Visit only the active list, including when
           this draw merely copied the unchanged scene cache. */
        Segment *s = r->active_segments;
        while (s) {
            Segment *next_active = s->active_next;
            if (r->reduce_motion || segment_fraction(s, now) >= 1) {
                r->retained_scene_bytes -= s->previous_bytes;
                cJSON_Delete(s->previous);
                s->previous = NULL;
                s->previous_bytes = 0;
                s->bounds = s->current_bounds;
            }
            if (r->reduce_motion || (!s->looping && now >= s->animation_until))
                s->animation_pending = 0;
            if (!segment_active(s, now)) {
                /* Capture can draw the endpoint without presenting the window. */
                r->animation_pending = 1;
                segment_deactivate(r, s);
            }
            s = next_active;
        }
    }
    return ok;
}
int px_renderer_draw(PXRenderer *r, char *error, int error_size) {
    size_t bytes;
    int ok;
    if (!r)
        return 0;
    r->last_commands_replayed = 0;
    r->last_segments_tested = 0;
    if (r->segmented)
        return draw_segments(r, error, error_size);
    /* This cache is independent of the bounded text/image LRUs. A complete
       static scene only
     * requires one native copy per subsequent presentation.
       Animated frames always execute
     * their retained commands at native time. */
    bytes = (size_t)r->pixel_w * (size_t)r->pixel_h * 4;
    if (!r->cache_scene || px_renderer_animating(r) || !bytes || bytes > 32u * 1024u * 1024u) {
        r->scene_cache_valid = 0;
        return render_commands(r, error, error_size);
    }
    if (!r->scene_cache) {
        r->scene_cache =
            SDL_CreateTexture(r->renderer, SDL_PIXELFORMAT_RGBA8888, SDL_TEXTUREACCESS_TARGET, r->pixel_w, r->pixel_h);
        if (!r->scene_cache)
            return render_commands(r, error, error_size);
        /* The scene already includes its background and alpha compositing.
           Copy these
         * pixels exactly instead of multiplying alpha a second time. */
        if (!SDL_SetTextureBlendMode(r->scene_cache, SDL_BLENDMODE_NONE) ||
            !SDL_SetTextureScaleMode(r->scene_cache, SDL_SCALEMODE_NEAREST)) {
            discard_scene_cache(r);
            return render_commands(r, error, error_size);
        }
        r->scene_cache_bytes = bytes;
    }
    if (!r->scene_cache_valid) {
        if (!SDL_SetRenderTarget(r->renderer, r->scene_cache)) {
            discard_scene_cache(r);
            return render_commands(r, error, error_size);
        }
        ok = render_commands(r, error, error_size);
        if (!SDL_SetRenderTarget(r->renderer, NULL)) {
            fail(r, "Restore window render target");
            output_error(r, error, error_size);
            return 0;
        }
        if (!ok)
            return 0;
        r->scene_cache_valid = 1;
    } else {
        ++r->scene_cache_hits;
    }
    if (!set_clip(r, NULL) || !SDL_RenderTexture(r->renderer, r->scene_cache, NULL, NULL)) {
        fail(r, "Copy retained scene");
        output_error(r, error, error_size);
        return 0;
    }
    return 1;
}
int px_renderer_present(PXRenderer *r, char *error, int error_size) {
    if (r && SDL_RenderPresent(r->renderer)) {
        if (r->segmented || r->animation_settled)
            r->animation_pending = 0;
        return 1;
    }
    if (r) {
        fail(r, "Present frame");
        output_error(r, error, error_size);
    }
    return 0;
}

static void clear_textures(PXRenderer *r) {
    discard_scene_cache(r);
    while (r->textures)
        cache_remove(r, r->textures);
}
static int refresh_viewport(PXRenderer *r) {
    int w, h, pw, ph;
    double previous = r->scale, detected;
    if (!SDL_GetWindowSize(r->window, &w, &h) || !SDL_GetRenderOutputSize(r->renderer, &pw, &ph))
        return fail(r, "Read drawable size");
    if (w <= 0 || h <= 0 || pw <= 0 || ph <= 0)
        return 1;
    if (pw != r->pixel_w || ph != r->pixel_h)
        discard_scene_cache(r);
    r->window_w = w;
    r->window_h = h;
    r->pixel_w = pw;
    r->pixel_h = ph;
    if (r->automatic_scale) {
        detected = SDL_GetWindowDisplayScale(r->window);
        r->scale = isfinite(detected) && detected > 0 ? detected : (double)pw / w;
    }
    r->scale = clamp(r->scale, .125, 16);
    r->width = pw / r->scale;
    r->height = ph / r->scale;
    if (previous != r->scale) {
        r->footprints_stale = 1;
        r->scene_cache_valid = 0;
        ++r->revision;
    }
    return 1;
}
static int resize_window(PXRenderer *r, double width, double height, int fit) {
    double sx = r->window_w * r->scale / r->pixel_w, sy = r->window_h * r->scale / r->pixel_h;
    int w = (int)max2(1, rnd(width * sx)), h = (int)max2(1, rnd(height * sy));
    SDL_Rect bounds = {0};
    int top = 0, left = 0, bottom = 0, right = 0, position = 0;
    if (fit && SDL_GetDisplayUsableBounds(SDL_GetDisplayForWindow(r->window), &bounds) && bounds.w > 0 &&
        bounds.h > 0) {
        SDL_GetWindowBordersSize(r->window, &top, &left, &bottom, &right);
        w = (int)max2(1, min2(w, bounds.w - left - right));
        h = (int)max2(1, min2(h, bounds.h - top - bottom));
        position = 1;
    }
    if (!SDL_SetWindowSize(r->window, w, h))
        return fail(r, "Set window size");
    if (position)
        SDL_SetWindowPosition(r->window, bounds.x + left + (bounds.w - left - right - w) / 2,
                              bounds.y + top + (bounds.h - top - bottom - h) / 2);
    if (!SDL_SyncWindow(r->window))
        return fail(r, "Synchronize window size");
    return refresh_viewport(r);
}
static int set_vsync(PXRenderer *r, int enabled) {
    int actual;
    if (!SDL_SetRenderVSync(r->renderer, enabled) || !SDL_GetRenderVSync(r->renderer, &actual))
        return fail(r, "Configure VSync");
    if (actual != enabled) {
        snprintf(r->error, sizeof(r->error), "Renderer did not apply requested VSync mode");
        return 0;
    }
    r->vsync = actual;
    return 1;
}
static cJSON *viewport_value(PXRenderer *r) {
    double safe[4] = {0};
    cJSON *j = cJSON_CreateObject();
    cJSON_AddNumberToObject(j, "width", r->width);
    cJSON_AddNumberToObject(j, "height", r->height);
    cJSON_AddNumberToObject(j, "scale", r->scale);
    cJSON_AddItemToObject(j, "safe_area", cJSON_CreateDoubleArray(safe, 4));
    cJSON_AddNumberToObject(j, "keyboard_occlusion", 0);
    return j;
}
cJSON *px_renderer_info(PXRenderer *r) {
    cJSON *j;
    double pixels[2], window[2];
    int vsync = 0;
    const char *name;
    if (!r)
        return NULL;
    refresh_viewport(r);
    j = cJSON_CreateObject();
    pixels[0] = r->pixel_w;
    pixels[1] = r->pixel_h;
    window[0] = r->window_w;
    window[1] = r->window_h;
    cJSON_AddNumberToObject(j, "width", r->width);
    cJSON_AddNumberToObject(j, "height", r->height);
    cJSON_AddNumberToObject(j, "scale", r->scale);
    cJSON_AddItemToObject(j, "viewport", viewport_value(r));
    cJSON_AddItemToObject(j, "pixel_size", cJSON_CreateDoubleArray(pixels, 2));
    cJSON_AddItemToObject(j, "window_size", cJSON_CreateDoubleArray(window, 2));
    cJSON_AddNumberToObject(j, "resource_revision", (double)r->revision);
    name = SDL_GetRendererName(r->renderer);
    cJSON_AddStringToObject(j, "renderer", name ? name : "unknown");
    if (SDL_GetRenderVSync(r->renderer, &vsync))
        cJSON_AddNumberToObject(j, "vsync", vsync);
    else
        cJSON_AddNullToObject(j, "vsync");
    cJSON_AddBoolToObject(j, "visible",
                          (SDL_GetWindowFlags(r->window) & (SDL_WINDOW_HIDDEN | SDL_WINDOW_MINIMIZED)) == 0);
    cJSON_AddBoolToObject(j, "animating", px_renderer_animating(r));
    cJSON_AddNumberToObject(j, "texture_entries", r->cache_entries);
    cJSON_AddNumberToObject(j, "image_decode_attempts", (double)r->image_decode_attempts);
    cJSON_AddNumberToObject(
        j, "texture_bytes",
        (double)(r->cache_bytes[0] + r->cache_bytes[1] + r->cache_bytes[2] + r->cache_bytes[3] + r->cache_bytes[4]));
    cJSON_AddBoolToObject(j, "cache_scene", r->cache_scene);
    cJSON_AddNumberToObject(j, "scene_cache_bytes", (double)r->scene_cache_bytes);
    cJSON_AddNumberToObject(j, "scene_cache_hits", (double)r->scene_cache_hits);
    cJSON_AddStringToObject(j, "backend", "window");
    cJSON_AddBoolToObject(j, "scene_patches", 1);
    cJSON_AddNumberToObject(j, "retained_scene_bytes", (double)r->retained_scene_bytes);
    cJSON_AddNumberToObject(j, "retained_scene_budget", PX_SCENE_BUDGET);
    cJSON_AddNumberToObject(j, "segment_count", (double)r->segment_count);
    cJSON_AddNumberToObject(j, "command_count", (double)r->command_count);
    cJSON_AddNumberToObject(j, "commands_touched", (double)r->commands_touched);
    cJSON_AddNumberToObject(j, "last_commands_touched", (double)r->last_commands_touched);
    cJSON_AddNumberToObject(j, "commands_replayed", (double)r->commands_replayed);
    cJSON_AddNumberToObject(j, "last_commands_replayed", (double)r->last_commands_replayed);
    cJSON_AddNumberToObject(j, "segments_updated", (double)r->segments_updated);
    cJSON_AddNumberToObject(j, "segments_removed", (double)r->segments_removed);
    cJSON_AddNumberToObject(j, "scene_patch_updates", (double)r->scene_patch_updates);
    cJSON_AddNumberToObject(j, "active_segment_count", (double)r->active_segment_count);
    cJSON_AddNumberToObject(j, "segments_tested", (double)r->segments_tested);
    cJSON_AddNumberToObject(j, "last_segments_tested", (double)r->last_segments_tested);
    cJSON_AddNumberToObject(j, "animation_segments_tested", (double)r->animation_segments_tested);
    cJSON_AddNumberToObject(j, "unchanged_segments", (double)r->unchanged_segments);
    {
        size_t snapshots = 0;
        Segment *s;
        for (s = r->active_segments; s; s = s->active_next)
            if (s->previous)
                snapshots += (size_t)cJSON_GetArraySize(s->previous);
        cJSON_AddNumberToObject(j, "transition_snapshot_commands", (double)snapshots);
    }
    return j;
}
PXRenderer *px_renderer_open(const cJSON *config, char *error, int error_size) {
    PXRenderer *r = (PXRenderer *)calloc(1, sizeof(*r));
    const cJSON *scale = get(config, "scale");
    const char *font_dir;
    SDL_WindowFlags flags = SDL_WINDOW_HIGH_PIXEL_DENSITY | SDL_WINDOW_HIDDEN;
    char path[4096];
    double width = num(config, "width", 960), height = num(config, "height", 640);
    if (!r)
        return NULL;
    r->scale = 1;
    r->automatic_scale = !scale || cJSON_IsNull(scale);
    r->vsync = boolean(get(config, "vsync"), 1);
    r->cache_scene = boolean(get(config, "cache_scene"), 1);
    if (!cJSON_IsObject(config) || !isfinite(width) || !isfinite(height) || width < 1 || height < 1 || width > 16384 ||
        height > 16384 || (!r->automatic_scale && !valid_number(scale, .125, 16))) {
        snprintf(r->error, sizeof(r->error), "Invalid window dimensions or scale");
        goto failed;
    }
    if (!r->automatic_scale)
        r->scale = number(scale, 1);
    font_dir = string(get(config, "font_dir"), NULL);
    if (!font_dir) {
        snprintf(path, sizeof(path), "%sassets", SDL_GetBasePath() ? SDL_GetBasePath() : "");
        font_dir = path;
    }
    r->font_dir = copy_string(font_dir);
    if (!r->font_dir)
        goto failed;
    SDL_SetMainReady();
    if (!SDL_InitSubSystem(SDL_INIT_VIDEO | SDL_INIT_EVENTS)) {
        fail(r, "Initialize SDL3 video");
        goto failed;
    }
    r->initialized = 1;
    if (!TTF_Init()) {
        fail(r, "Initialize SDL3_ttf");
        goto failed;
    }
    r->ttf_initialized = 1;
    if (boolean(get(config, "resizable"), 1))
        flags |= SDL_WINDOW_RESIZABLE;
    r->window = SDL_CreateWindow(string(get(config, "title"), "Pysual"), 320, 200, flags);
    if (!r->window) {
        fail(r, "Create window");
        goto failed;
    }
    r->window_id = SDL_GetWindowID(r->window);
    r->renderer = SDL_CreateRenderer(r->window, NULL);
    if (!r->renderer) {
        fail(r, "Create renderer");
        goto failed;
    }
    if (!SDL_SetRenderDrawBlendMode(r->renderer, SDL_BLENDMODE_BLEND)) {
        fail(r, "Enable alpha blending");
        goto failed;
    }
    if (!set_vsync(r, r->vsync) || !refresh_viewport(r) || !resize_window(r, width, height, 1))
        goto failed;
    /* Fail at open, rather than halfway through the first layout, if assets are absent. */
    if (!font(r, 14, 0) || !font(r, 14, 1))
        goto failed;
    if (!boolean(get(config, "hidden"), 0) && !SDL_ShowWindow(r->window)) {
        fail(r, "Show window");
        goto failed;
    }
    r->commit_started = px_now();
    return r;
failed:
    output_error(r, error, error_size);
    px_renderer_close(r);
    return NULL;
}
void px_renderer_close(PXRenderer *r) {
    int i;
    if (!r)
        return;
    clear_textures(r);
    for (i = 0; i < PX_FONT_ENTRIES; ++i)
        if (r->fonts[i].font)
            TTF_CloseFont(r->fonts[i].font);
    if (r->renderer)
        SDL_DestroyRenderer(r->renderer);
    if (r->window)
        SDL_DestroyWindow(r->window);
    if (r->ttf_initialized)
        TTF_Quit();
    if (r->initialized)
        SDL_QuitSubSystem(SDL_INIT_VIDEO | SDL_INIT_EVENTS);
    cJSON_Delete(r->commands);
    free_draw_commands(r->decoded, r->decoded_count);
    r->decoded = NULL;
    r->decoded_count = 0;
    segments_clear(r);
    cJSON_Delete(r->previous);
    cJSON_Delete(r->resource_events);
    free(r->animation_started);
    free(r->font_dir);
    free(r);
}
static cJSON *event_value(const char *kind) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "kind", kind);
    return j;
}
static void event_modifiers(cJSON *j, SDL_Keymod mods) {
    cJSON_AddBoolToObject(j, "shift", (mods & SDL_KMOD_SHIFT) != 0);
    cJSON_AddBoolToObject(j, "ctrl", (mods & (SDL_KMOD_CTRL | SDL_KMOD_GUI)) != 0);
}
static void event_point(PXRenderer *r, cJSON *j, double x, double y) {
    cJSON_AddNumberToObject(j, "x", x * r->pixel_w / (r->window_w * r->scale));
    cJSON_AddNumberToObject(j, "y", y * r->pixel_h / (r->window_h * r->scale));
}
static const char *key_name(SDL_Keycode key, char buffer[128]) {
    const char *name;
    size_t i;
    switch (key) {
    case SDLK_RETURN:
    case SDLK_KP_ENTER:
        return "Enter";
    case SDLK_SPACE:
        return "Space";
    case SDLK_TAB:
        return "Tab";
    case SDLK_ESCAPE:
        return "Escape";
    case SDLK_BACKSPACE:
        return "Backspace";
    case SDLK_DELETE:
        return "Delete";
    case SDLK_INSERT:
        return "Insert";
    case SDLK_LEFT:
        return "ArrowLeft";
    case SDLK_RIGHT:
        return "ArrowRight";
    case SDLK_UP:
        return "ArrowUp";
    case SDLK_DOWN:
        return "ArrowDown";
    case SDLK_HOME:
        return "Home";
    case SDLK_END:
        return "End";
    case SDLK_PAGEUP:
        return "PageUp";
    case SDLK_PAGEDOWN:
        return "PageDown";
    default:
        break;
    }
    name = SDL_GetKeyName(key);
    if ((key >= SDLK_F1 && key <= SDLK_F12) || (key >= SDLK_F13 && key <= SDLK_F24))
        return name;
    snprintf(buffer, 128, "%s", name ? name : "");
    for (i = 0; buffer[i]; ++i)
        if (buffer[i] >= 'A' && buffer[i] <= 'Z')
            buffer[i] = (char)(buffer[i] - 'A' + 'a');
    return buffer;
}
cJSON *px_renderer_poll(PXRenderer *r) {
    cJSON *events = cJSON_CreateArray(), *j;
    SDL_Event e;
    int count = 0;
    if (!r)
        return events;
    if (r->resource_events) {
        cJSON_Delete(events);
        events = r->resource_events;
        r->resource_events = NULL;
    }
    /* Limit native work per iteration. The host then services IPC and presents. */
    while (count++ < 256 && SDL_PollEvent(&e)) {
        const char *kind = NULL;
        SDL_WindowID id = 0;
        j = NULL;
        switch (e.type) {
        case SDL_EVENT_QUIT:
            kind = "close";
            break;
        case SDL_EVENT_WILL_ENTER_BACKGROUND:
            kind = "suspend";
            break;
        case SDL_EVENT_DID_ENTER_FOREGROUND:
            kind = "resume";
            break;
        case SDL_EVENT_WINDOW_CLOSE_REQUESTED:
            kind = "close";
            id = e.window.windowID;
            break;
        case SDL_EVENT_WINDOW_RESIZED:
        case SDL_EVENT_WINDOW_PIXEL_SIZE_CHANGED:
        case SDL_EVENT_WINDOW_DISPLAY_CHANGED:
        case SDL_EVENT_WINDOW_DISPLAY_SCALE_CHANGED:
            kind = "viewport";
            id = e.window.windowID;
            break;
        case SDL_EVENT_WINDOW_FOCUS_LOST:
            kind = "blur";
            id = e.window.windowID;
            break;
        case SDL_EVENT_WINDOW_MOUSE_LEAVE:
            kind = "pointer_leave";
            id = e.window.windowID;
            break;
        case SDL_EVENT_WINDOW_MINIMIZED:
            kind = "suspend";
            id = e.window.windowID;
            break;
        case SDL_EVENT_WINDOW_RESTORED:
            kind = "resume";
            id = e.window.windowID;
            break;
        case SDL_EVENT_WINDOW_EXPOSED:
            kind = "repaint";
            id = e.window.windowID;
            break;
        case SDL_EVENT_KEY_DOWN:
            kind = "key_down";
            id = e.key.windowID;
            break;
        case SDL_EVENT_KEY_UP:
            kind = "key_up";
            id = e.key.windowID;
            break;
        case SDL_EVENT_MOUSE_MOTION:
            kind = "pointer_move";
            id = e.motion.windowID;
            break;
        case SDL_EVENT_MOUSE_BUTTON_DOWN:
            kind = "pointer_down";
            id = e.button.windowID;
            break;
        case SDL_EVENT_MOUSE_BUTTON_UP:
            kind = "pointer_up";
            id = e.button.windowID;
            break;
        case SDL_EVENT_MOUSE_WHEEL:
            kind = "wheel";
            id = e.wheel.windowID;
            break;
        case SDL_EVENT_TEXT_INPUT:
            kind = "text";
            id = e.text.windowID;
            break;
        case SDL_EVENT_TEXT_EDITING:
            kind = "composition";
            id = e.edit.windowID;
            break;
        case SDL_EVENT_RENDER_TARGETS_RESET:
        case SDL_EVENT_RENDER_DEVICE_RESET:
            clear_textures(r);
            ++r->revision;
            kind = "viewport";
            break;
        default:
            break;
        }
        if (!kind || (id && id != r->window_id))
            continue;
        j = event_value(kind);
        if (!strcmp(kind, "viewport")) {
            if (refresh_viewport(r)) {
                cJSON_AddItemToObject(j, "viewport", viewport_value(r));
                cJSON_AddNumberToObject(j, "resource_revision", (double)r->revision);
            } else {
                cJSON_Delete(j);
                j = event_value("error");
                cJSON_AddStringToObject(j, "text", r->error);
            }
        } else if (e.type == SDL_EVENT_KEY_DOWN || e.type == SDL_EVENT_KEY_UP) {
            char name[128];
            cJSON_AddStringToObject(j, "key", key_name(e.key.key, name));
            event_modifiers(j, e.key.mod);
        } else if (e.type == SDL_EVENT_MOUSE_MOTION) {
            event_point(r, j, e.motion.x, e.motion.y);
            cJSON_AddNumberToObject(j, "button", 1);
            event_modifiers(j, SDL_GetModState());
        } else if (e.type == SDL_EVENT_MOUSE_BUTTON_DOWN || e.type == SDL_EVENT_MOUSE_BUTTON_UP) {
            event_point(r, j, e.button.x, e.button.y);
            cJSON_AddNumberToObject(j, "button", e.button.button);
            event_modifiers(j, SDL_GetModState());
        } else if (e.type == SDL_EVENT_MOUSE_WHEEL) {
            int horizontal = fabs(e.wheel.x) > fabs(e.wheel.y);
            double delta = horizontal ? e.wheel.x : -e.wheel.y;
            if (e.wheel.direction == SDL_MOUSEWHEEL_FLIPPED)
                delta = -delta;
            if (delta == 0) {
                cJSON_Delete(j);
                continue;
            }
            event_point(r, j, e.wheel.mouse_x, e.wheel.mouse_y);
            cJSON_AddNumberToObject(j, "delta", delta);
            event_modifiers(j, SDL_GetModState() | (horizontal ? SDL_KMOD_SHIFT : 0));
        } else if (e.type == SDL_EVENT_TEXT_INPUT)
            cJSON_AddStringToObject(j, "text", e.text.text ? e.text.text : "");
        else if (e.type == SDL_EVENT_TEXT_EDITING)
            cJSON_AddStringToObject(j, "text", e.edit.text ? e.edit.text : "");
        /* Close is an input request. Only Python's accepted close/EOF ends the host. */
        cJSON_AddItemToArray(events, j);
    }
    return events;
}
cJSON *px_renderer_call(PXRenderer *r, const cJSON *request, char *error, int error_size) {
    const char *op = string(get(request, "op"), "");
    const cJSON *v;
    cJSON *result = NULL;
    if (!r)
        return NULL;
    r->error[0] = '\0';
    if (!strcmp(op, "reload_image")) {
        char key[80];
        Texture *texture;
        int i;
        v = get(request, "source");
        if (!valid_string(v))
            goto invalid;
        image_key(v->valuestring, key);
        texture = cache_find(r, key);
        if (texture)
            cache_remove(r, texture);
        for (i = 0; i < 128; ++i)
            if (r->failed_used[i] && !strcmp(key, r->failed_images[i]))
                r->failed_used[i] = 0;
        r->scene_cache_valid = 0;
        ++r->revision;
        return px_renderer_info(r);
    }
    if (!strcmp(op, "measure")) {
        double values[2];
        const cJSON *text = get(request, "text"), *size = get(request, "size"), *mono = get(request, "mono");
        if (!valid_string(text) || !valid_number(size, .01, 2048) || (mono && !cJSON_IsBool(mono)))
            goto invalid;
        if (!measure(r, text->valuestring, size->valuedouble, boolean(mono, 0), &values[0], &values[1]))
            goto failed;
        return cJSON_CreateDoubleArray(values, 2);
    }
    if (!strcmp(op, "measure_many")) {
        const cJSON *texts = get(request, "texts"), *size = get(request, "size"), *mono = get(request, "mono");
        cJSON *array, *text;
        int count, index;
        if (!cJSON_IsArray(texts) || !valid_number(size, .01, 2048) || (mono && !cJSON_IsBool(mono)))
            goto invalid;
        count = cJSON_GetArraySize(texts);
        if (count < 0 || count > 4096)
            goto invalid;
        array = cJSON_CreateArray();
        if (!array)
            goto failed;
        index = 0;
        cJSON_ArrayForEach(text, texts) {
            double values[2];
            cJSON *pair;
            if (!valid_string(text)) {
                cJSON_Delete(array);
                goto invalid;
            }
            if (!measure(r, text->valuestring, size->valuedouble, boolean(mono, 0), &values[0], &values[1])) {
                cJSON_Delete(array);
                goto failed;
            }
            pair = cJSON_CreateDoubleArray(values, 2);
            if (!pair) {
                cJSON_Delete(array);
                goto failed;
            }
            cJSON_AddItemToArray(array, pair);
            ++index;
        }
        if (index != count) {
            cJSON_Delete(array);
            goto invalid;
        }
        return array;
    }
    if (!strcmp(op, "set_title")) {
        v = get(request, "title");
        if (!valid_string(v))
            goto invalid;
        if (!SDL_SetWindowTitle(r->window, v->valuestring)) {
            fail(r, "Set window title");
            goto failed;
        }
        return cJSON_CreateNull();
    }
    if (!strcmp(op, "set_size")) {
        v = get(request, "width");
        if (!valid_number(v, 1, 16384) || !valid_number(get(request, "height"), 1, 16384))
            goto invalid;
        if (!resize_window(r, v->valuedouble, num(request, "height", 1), 0))
            goto failed;
        return px_renderer_info(r);
    }
    if (!strcmp(op, "text_input")) {
        v = get(request, "rect");
        if (!valid_box(v, 1))
            goto invalid;
        if (!v || cJSON_IsNull(v)) {
            if (!SDL_StopTextInput(r->window)) {
                fail(r, "Stop text input");
                goto failed;
            }
        } else {
            Box b = box_json(v);
            double sx = r->window_w * r->scale / r->pixel_w, sy = r->window_h * r->scale / r->pixel_h;
            SDL_Rect area = {rnd(b.x * sx), rnd(b.y * sy), (int)max2(1, rnd(b.w * sx)), (int)max2(1, rnd(b.h * sy))};
            if (!SDL_SetTextInputArea(r->window, &area, 0) || !SDL_StartTextInput(r->window)) {
                fail(r, "Start text input");
                goto failed;
            }
        }
        return cJSON_CreateNull();
    }
    if (!strcmp(op, "clipboard_read")) {
        char *text = SDL_GetClipboardText();
        if (!text) {
            fail(r, "Read clipboard");
            goto failed;
        }
        if (!clipboard_fits(text)) {
            SDL_free(text);
            snprintf(r->error, sizeof(r->error), "Clipboard text exceeds the 8 MiB text or 16 MiB JSON limit");
            goto failed;
        }
        result = cJSON_CreateString(text);
        SDL_free(text);
        return result;
    }
    if (!strcmp(op, "clipboard_write")) {
        v = get(request, "text");
        if (!valid_string(v))
            goto invalid;
        if (strlen(v->valuestring) > PX_MAX_CLIPBOARD) {
            snprintf(r->error, sizeof(r->error), "Clipboard text exceeds 8 MiB");
            goto failed;
        }
        if (!SDL_SetClipboardText(v->valuestring)) {
            fail(r, "Write clipboard");
            goto failed;
        }
        return cJSON_CreateNull();
    }
    if (!strcmp(op, "capture")) {
        const char *path = string(get(request, "path"), NULL);
        SDL_Surface *pixels;
        SDL_Texture *capture_target;
        size_t length;
        int ok, own_capture = 0;
        if (!path || !*path || strlen(path) > 32768)
            goto invalid;
        if (!px_renderer_draw(r, error, error_size))
            return NULL;
        /* Hidden window backbuffers can be suppressed or stale on some SDL
           drivers. Read an explicit offscreen target, never that backbuffer. */
        capture_target = r->scene_cache_valid ? r->scene_cache : NULL;
        if (!capture_target) {
            own_capture = 1;
            capture_target = SDL_CreateTexture(r->renderer, SDL_PIXELFORMAT_RGBA8888, SDL_TEXTUREACCESS_TARGET,
                                               r->pixel_w, r->pixel_h);
        }
        if (!capture_target || !SDL_SetRenderTarget(r->renderer, capture_target)) {
            if (own_capture)
                SDL_DestroyTexture(capture_target);
            fail(r, "Create capture target");
            goto failed;
        }
        ok = !own_capture ||
             (r->segmented ? render_segments(r, (Box){0, 0, r->width, r->height}, px_now(), error, error_size)
                           : render_commands(r, error, error_size));
        if (!ok) {
            SDL_SetRenderTarget(r->renderer, NULL);
            if (own_capture)
                SDL_DestroyTexture(capture_target);
            return NULL;
        }
        pixels = SDL_RenderReadPixels(r->renderer, NULL);
        ok = SDL_SetRenderTarget(r->renderer, NULL);
        if (own_capture)
            SDL_DestroyTexture(capture_target);
        if (!ok) {
            SDL_DestroySurface(pixels);
            fail(r, "Restore capture target");
            goto failed;
        }
        if (!pixels) {
            fail(r, "Read frame pixels");
            goto failed;
        }
        length = strlen(path);
        ok = length >= 4 && SDL_strcasecmp(path + length - 4, ".png") == 0 ? IMG_SavePNG(pixels, path)
                                                                           : SDL_SaveBMP(pixels, path);
        SDL_DestroySurface(pixels);
        if (!ok) {
            fail(r, "Save captured frame");
            goto failed;
        }
        result = cJSON_CreateObject();
        cJSON_AddStringToObject(result, "path", path);
        cJSON_AddNumberToObject(result, "width", r->pixel_w);
        cJSON_AddNumberToObject(result, "height", r->pixel_h);
        return result;
    }
    if (!strcmp(op, "configure")) {
        const cJSON *vsync = get(request, "vsync"), *reduce = get(request, "reduce_motion"),
                    *transition = get(request, "transition_ms"), *scale = get(request, "scale"),
                    *cache = get(request, "cache_scene");
        double seconds = r->transition_seconds;
        if (vsync && !cJSON_IsBool(vsync) && !valid_number(vsync, 0, 1))
            goto invalid;
        if (reduce && !cJSON_IsBool(reduce))
            goto invalid;
        if (cache && !cJSON_IsBool(cache))
            goto invalid;
        if (transition) {
            if (!valid_number(transition, 0, 60000))
                goto invalid;
            seconds = transition->valuedouble / 1000;
        } else if ((transition = get(request, "transition_seconds"))) {
            if (!valid_number(transition, 0, 60))
                goto invalid;
            seconds = transition->valuedouble;
        }
        if (scale && !cJSON_IsNull(scale) && !valid_number(scale, .125, 16))
            goto invalid;
        if (vsync && !set_vsync(r, cJSON_IsBool(vsync) ? cJSON_IsTrue(vsync) : (int)vsync->valuedouble))
            goto failed;
        if (scale) {
            r->scene_cache_valid = 0;
            r->footprints_stale = 1;
            r->automatic_scale = cJSON_IsNull(scale);
            if (!r->automatic_scale)
                r->scale = scale->valuedouble;
            if (!refresh_viewport(r))
                goto failed;
        }
        if (reduce) {
            if (r->reduce_motion != cJSON_IsTrue(reduce)) {
                r->scene_cache_valid = 0;
                r->animation_pending = 1;
                if (!r->segmented) {
                    r->animation_settled = 0;
                }
            }
            r->reduce_motion = cJSON_IsTrue(reduce);
        }
        if (cache) {
            r->cache_scene = cJSON_IsTrue(cache);
            if (!r->cache_scene)
                discard_scene_cache(r);
        }
        r->transition_seconds = seconds;
        if (r->reduce_motion) {
            r->retained_scene_bytes -= json_heap_bytes(r->previous);
            cJSON_Delete(r->previous);
            r->previous = NULL;
        }
        return px_renderer_info(r);
    }
    snprintf(r->error, sizeof(r->error), "Unsupported renderer operation: %.100s", op);
    goto failed;
invalid:
    snprintf(r->error, sizeof(r->error), "Invalid arguments for renderer operation: %.100s", op);
failed:
    output_error(r, error, error_size);
    return NULL;
}
