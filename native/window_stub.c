/* Terminal-only build: no SDL headers, symbols or runtime dependencies. */
#include "px_backend.h"
#include <stdio.h>
#ifndef PYSUAL_WITH_WINDOW
static int unavailable(char *error, int size) {
    snprintf(error, (size_t)size, "Window rendering is unavailable in this terminal-only host; build with window support");
    return 0;
}
PXRenderer *px_renderer_open(const cJSON *j, char *e, int n) { (void)j; unavailable(e,n); return NULL; }
void px_renderer_close(PXRenderer *r) { (void)r; }
int px_renderer_commit(PXRenderer *r,const cJSON *j,char *e,int n) { (void)r;(void)j;return unavailable(e,n); }
int px_renderer_patch(PXRenderer *r,const cJSON *j,char *e,int n) { (void)r;(void)j;return unavailable(e,n); }
void px_renderer_reset_stats(PXRenderer *r) { (void)r; }
void px_renderer_last_work(PXRenderer *r,uint64_t *a,uint64_t *b) { (void)r;*a=*b=0; }
uint64_t px_renderer_last_segment_tests(PXRenderer *r) { (void)r;return 0; }
int px_renderer_draw(PXRenderer *r,char *e,int n) { (void)r;return unavailable(e,n); }
int px_renderer_present(PXRenderer *r,char *e,int n) { (void)r;return unavailable(e,n); }
cJSON *px_renderer_poll(PXRenderer *r) { (void)r;return cJSON_CreateArray(); }
cJSON *px_renderer_info(PXRenderer *r) { (void)r;return cJSON_CreateObject(); }
cJSON *px_renderer_call(PXRenderer *r,const cJSON *j,char *e,int n) { (void)r;(void)j;unavailable(e,n);return NULL; }
int px_renderer_animating(PXRenderer *r) { (void)r;return 0; }
#endif
