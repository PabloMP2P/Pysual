/* Clipboard service bounds, independent of SDL and the OS clipboard. */
#ifndef PX_CLIPBOARD_H
#define PX_CLIPBOARD_H
#include "px_backend.h"
#include <stddef.h>

#define PX_MAX_CLIPBOARD (8u * 1024u * 1024u)

static int clipboard_fits(const char *text) {
    const unsigned char *p = (const unsigned char *)text;
    size_t source = 0, encoded = 0;
    /* A string result uses 23 bytes for {"ok":true,"result":""}.
       Match cJSON escaping without allocating an oversized wire reply. */
    const size_t content_budget = PX_MAX_PAYLOAD - 23u;
    for (; *p; p++) {
        unsigned int cost = 1;
        if (++source > PX_MAX_CLIPBOARD)
            return 0;
        if (*p == '"' || *p == '\\' || *p == '\b' || *p == '\f' || *p == '\n' || *p == '\r' || *p == '\t')
            cost = 2;
        else if (*p < 32)
            cost = 6;
        encoded += cost;
        if (encoded > content_budget)
            return 0;
    }
    return 1;
}
#endif
