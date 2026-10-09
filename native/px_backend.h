/* Retained renderer contract. Owned JSON returns; NULL means error. */
#ifndef PX_BACKEND_H
#define PX_BACKEND_H
#include "cJSON.h"
#include <stdint.h>
#define PX_MAX_PAYLOAD (16u * 1024u * 1024u)
typedef struct PXRenderer PXRenderer;
typedef struct PTBackend PTBackend;
/* Config: title,width,height,resizable,scale,hidden,vsync,continuous,font_dir. */
PXRenderer *px_renderer_open(const cJSON *config, char *error, int error_size);
void px_renderer_close(PXRenderer *r);
/* Validate and replace commands atomically. Commands are arrays [method, args...].
   Own a deep copy; caller frees input. All numbers finite; cap sizes/resources. */
int px_renderer_commit(PXRenderer *r, const cJSON *commands, char *error, int error_size);
/* Atomic retained segment edits: upsert [{id,bounds,commands}], remove [id],
   optional complete order [id], background color. Segment commands may not
   contain begin; clip state starts empty for every segment. */
int px_renderer_patch(PXRenderer *r, const cJSON *patch, char *error, int error_size);
void px_renderer_reset_stats(PXRenderer *r);
void px_renderer_last_work(PXRenderer *r, uint64_t *touched, uint64_t *replayed);
uint64_t px_renderer_last_segment_tests(PXRenderer *r);
int px_renderer_draw(PXRenderer *r, char *error, int error_size);
int px_renderer_present(PXRenderer *r, char *error, int error_size);
cJSON *px_renderer_poll(PXRenderer *r);
cJSON *px_renderer_info(PXRenderer *r);
/* Request has op measure/set_title/set_size/text_input/clipboard_read/write/
   capture/configure/reload_image. Return result JSON (incl JSON null), NULL + error failure. */
cJSON *px_renderer_call(PXRenderer *r, const cJSON *request, char *error, int error_size);
int px_renderer_animating(PXRenderer *r);
/* Terminal same operations; config headless=true makes deterministic cell tests
   without taking over the real user's terminal. Snapshot returns complete cells. */
PTBackend *pt_open(const cJSON *config, char *error, int error_size);
void pt_close(PTBackend *r);
int pt_commit(PTBackend *r, const cJSON *commands, char *error, int error_size);
int pt_patch(PTBackend *r, const cJSON *patch, char *error, int error_size);
void pt_reset_stats(PTBackend *r);
void pt_last_work(PTBackend *r, uint64_t *touched, uint64_t *replayed);
uint64_t pt_last_segment_tests(PTBackend *r);
int pt_draw(PTBackend *r, char *error, int error_size);
int pt_present(PTBackend *r, char *error, int error_size);
cJSON *pt_poll(PTBackend *r);
cJSON *pt_info(PTBackend *r);
cJSON *pt_call(PTBackend *r, const cJSON *request, char *error, int error_size);
#endif
