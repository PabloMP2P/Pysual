/* Closed scalar command objects keep retained JSON comparisons shallow. */
#ifndef PX_COMMAND_SCHEMA_H
#define PX_COMMAND_SCHEMA_H
#include "cJSON.h"
#include <stddef.h>
#include <stdint.h>
#include <string.h>

static int px_scalar_members(const cJSON *object, const char *const *names, size_t count) {
    const cJSON *value;
    uint64_t seen = 0;
    if (!cJSON_IsObject(object) || count > 64)
        return 0;
    cJSON_ArrayForEach(value, object) {
        size_t i;
        if (!value->string ||
            !(cJSON_IsNull(value) || cJSON_IsBool(value) || cJSON_IsNumber(value) || cJSON_IsString(value)))
            return 0;
        for (i = 0; i < count; i++)
            if (!strcmp(value->string, names[i]))
                break;
        if (i == count || (seen & (UINT64_C(1) << i)))
            return 0;
        seen |= UINT64_C(1) << i;
    }
    return 1;
}

static int px_style_members(const cJSON *object) {
    static const char *const names[] = {
        "fill",        "foreground",  "border",        "radius",          "border_width",  "font_family",
        "font_size",   "padding",     "fill_end",      "border_end",      "gradient_axis", "bevel",
        "bevel_width", "bevel_light", "bevel_dark",    "highlight",       "inner_border",  "glow",
        "glow_width",  "pattern",     "pattern_color", "pattern_spacing", "shadow",        "shadow_blur",
        "shadow_x",    "shadow_y"};
    return px_scalar_members(object, names, sizeof(names) / sizeof(names[0]));
}
#endif
