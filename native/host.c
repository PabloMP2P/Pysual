/* Standalone retained-scene host. No Python headers, objects or frame calls. */
#define _POSIX_C_SOURCE 200809L
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
typedef SOCKET PxSocket;
#define PX_BAD_SOCKET INVALID_SOCKET
#else
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>
typedef int PxSocket;
#define PX_BAD_SOCKET (-1)
#endif
#include "px_backend.h"
#include <time.h>
#include <inttypes.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define MAX_PAYLOAD PX_MAX_PAYLOAD
#define HEADER_SIZE 16u
#define INPUT_CAP (MAX_PAYLOAD + HEADER_SIZE)
#define OUTPUT_CAP (2u * MAX_PAYLOAD + 65536u)
#define COMMAND_WATERMARK (OUTPUT_CAP - MAX_PAYLOAD - HEADER_SIZE - 4096u)
#define IO_BUDGET 262144u
#define COMMAND_BUDGET 32u
enum { OP_HELLO = 1, OP_COMMAND = 9, OP_EVENT = 101, OP_CLOSED = 102, OP_REPLY = 200 };
#ifndef _WIN32
static volatile sig_atomic_t interrupted = 0;
static void stop_signal(int signal_number) {
    (void)signal_number;
    interrupted = 1;
}
#endif
typedef struct {
    PxSocket socket;
    PXRenderer *renderer;
    PTBackend *terminal;
    unsigned char *input, *output;
    size_t input_size, output_start, output_size;
    uint32_t last_request;
    int initialized, continuous, closing, disconnected, protocol_error, has_scene, suspended, needs_draw,
        update_pending;
    double started, last_frame, close_deadline, interval, fps_limit, update_started;
    uint64_t frames, commands, events, scene_updates;
    double draw_seconds, present_seconds;
    double last_draw_seconds, max_draw_seconds, last_present_seconds, max_present_seconds;
    double scene_update_seconds, last_scene_update_seconds, max_scene_update_seconds;
    double scene_commit_seconds, last_scene_commit_seconds, max_scene_commit_seconds;
    double scene_render_seconds, last_scene_render_seconds, max_scene_render_seconds;
    double last_update_draw_seconds, last_update_present_seconds, update_samples[120];
    unsigned int update_sample_count, update_sample_next;
    uint64_t last_scene_commands_touched, last_scene_commands_replayed, last_scene_segments_tested;
} Host;
static double now_seconds(void) {
#ifdef _WIN32
    LARGE_INTEGER counter, frequency;
    QueryPerformanceCounter(&counter);
    QueryPerformanceFrequency(&frequency);
    return (double)counter.QuadPart / (double)frequency.QuadPart;
#else
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (double)now.tv_sec + (double)now.tv_nsec / 1000000000.0;
#endif
}

static uint32_t read_le32(const unsigned char *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void write_le32(unsigned char *p, uint32_t value) {
    p[0] = (unsigned char)value;
    p[1] = (unsigned char)(value >> 8);
    p[2] = (unsigned char)(value >> 16);
    p[3] = (unsigned char)(value >> 24);
}

static int queue_frame(Host *h, uint32_t opcode, uint32_t id, const void *payload, size_t size) {
    unsigned char *target;
    size_t total = HEADER_SIZE + size;
    if (size > MAX_PAYLOAD || total > OUTPUT_CAP - h->output_size)
        return 0;
    if (h->output_start + h->output_size + total > OUTPUT_CAP) {
        memmove(h->output, h->output + h->output_start, h->output_size);
        h->output_start = 0;
    }
    target = h->output + h->output_start + h->output_size;
    memcpy(target, "PXN1", 4);
    write_le32(target + 4, opcode);
    write_le32(target + 8, id);
    write_le32(target + 12, (uint32_t)size);
    if (size)
        memcpy(target + HEADER_SIZE, payload, size);
    h->output_size += total;
    return 1;
}

static int queue_json(Host *h, uint32_t opcode, uint32_t id, cJSON *value) {
    char *data = cJSON_PrintUnformatted(value);
    int ok = data && queue_frame(h, opcode, id, data, strlen(data));
    cJSON_free(data);
    cJSON_Delete(value);
    return ok;
}

static void reply(Host *h, uint32_t id, cJSON *result, const char *error) {
    cJSON *j = cJSON_CreateObject();
    cJSON_AddBoolToObject(j, "ok", error == NULL);
    if (error) {
        cJSON_AddStringToObject(j, "error", error);
        cJSON_Delete(result);
    } else
        cJSON_AddItemToObject(j, "result", result ? result : cJSON_CreateNull());
    if (!queue_json(h, OP_REPLY, id, j))
        h->disconnected = 1;
}

static cJSON *info(Host *h) {
    return h->terminal ? pt_info(h->terminal) : h->renderer ? px_renderer_info(h->renderer) : cJSON_CreateObject();
}

static cJSON *statistics(Host *h) {
    cJSON *j = info(h);
    double elapsed = now_seconds() - h->started;
    cJSON_AddNumberToObject(j, "frames", (double)h->frames);
    cJSON_AddNumberToObject(j, "elapsed_seconds", elapsed);
    cJSON_AddNumberToObject(j, "fps", elapsed > 0 ? h->frames / elapsed : 0);
    cJSON_AddNumberToObject(j, "draw_seconds", h->draw_seconds);
    cJSON_AddNumberToObject(j, "present_seconds", h->present_seconds);
    cJSON_AddNumberToObject(j, "last_draw_seconds", h->last_draw_seconds);
    cJSON_AddNumberToObject(j, "max_draw_seconds", h->max_draw_seconds);
    cJSON_AddNumberToObject(j, "last_present_seconds", h->last_present_seconds);
    cJSON_AddNumberToObject(j, "max_present_seconds", h->max_present_seconds);
    cJSON_AddNumberToObject(j, "scene_update_seconds", h->scene_update_seconds);
    cJSON_AddNumberToObject(j, "last_scene_update_seconds", h->last_scene_update_seconds);
    cJSON_AddNumberToObject(j, "max_scene_update_seconds", h->max_scene_update_seconds);
    cJSON_AddNumberToObject(j, "update_seconds", h->scene_update_seconds);
    cJSON_AddNumberToObject(j, "last_update_seconds", h->last_scene_update_seconds);
    cJSON_AddNumberToObject(j, "max_update_seconds", h->max_scene_update_seconds);
    cJSON_AddNumberToObject(j, "scene_commit_seconds", h->scene_commit_seconds);
    cJSON_AddNumberToObject(j, "last_scene_commit_seconds", h->last_scene_commit_seconds);
    cJSON_AddNumberToObject(j, "max_scene_commit_seconds", h->max_scene_commit_seconds);
    cJSON_AddNumberToObject(j, "last_commit_seconds", h->last_scene_commit_seconds);
    cJSON_AddNumberToObject(j, "scene_render_seconds", h->scene_render_seconds);
    cJSON_AddNumberToObject(j, "last_scene_render_seconds", h->last_scene_render_seconds);
    cJSON_AddNumberToObject(j, "max_scene_render_seconds", h->max_scene_render_seconds);
    cJSON_AddNumberToObject(j, "last_update_draw_seconds", h->last_update_draw_seconds);
    cJSON_AddNumberToObject(j, "last_update_present_seconds", h->last_update_present_seconds);
    cJSON_AddNumberToObject(j, "last_scene_commands_touched", (double)h->last_scene_commands_touched);
    cJSON_AddNumberToObject(j, "last_scene_commands_replayed", (double)h->last_scene_commands_replayed);
    cJSON_AddNumberToObject(j, "last_scene_segments_tested", (double)h->last_scene_segments_tested);
    {
        cJSON *samples = cJSON_CreateArray();
        unsigned int i;
        for (i = 0; i < h->update_sample_count; i++) {
            unsigned int index = (h->update_sample_next + 120 - h->update_sample_count + i) % 120;
            cJSON_AddItemToArray(samples, cJSON_CreateNumber(h->update_samples[index] * 1000));
        }
        cJSON_AddItemToObject(j, "scene_update_samples_ms", samples);
    }
    cJSON_AddNumberToObject(j, "requests", (double)h->commands);
    cJSON_AddNumberToObject(j, "input_events", (double)h->events);
    cJSON_AddNumberToObject(j, "scene_updates", (double)h->scene_updates);
    cJSON_AddBoolToObject(j, "continuous", h->continuous);
    cJSON_AddNumberToObject(j, "fps_limit", h->fps_limit);
    return j;
}

static void begin_close(Host *h, const char *reason, const char *error) {
    cJSON *j;
    if (h->closing)
        return;
    h->closing = 1;
    h->close_deadline = now_seconds() + 2;
    j = cJSON_CreateObject();
    cJSON_AddStringToObject(j, "reason", reason);
    if (error)
        cJSON_AddStringToObject(j, "error", error);
    cJSON_AddItemToObject(j, "stats", statistics(h));
    if (!queue_json(h, OP_CLOSED, 0, j))
        h->disconnected = 1;
}

static int draw(Host *h, char *error, int n) {
    double start = now_seconds(), done;
    int ok = h->terminal ? pt_draw(h->terminal, error, n) : px_renderer_draw(h->renderer, error, n);
    done = now_seconds();
    h->last_draw_seconds = done - start;
    h->draw_seconds += h->last_draw_seconds;
    h->max_draw_seconds = fmax(h->max_draw_seconds, h->last_draw_seconds);
    if (!ok)
        return 0;
    ok = h->terminal ? pt_present(h->terminal, error, n) : px_renderer_present(h->renderer, error, n);
    h->last_present_seconds = now_seconds() - done;
    h->present_seconds += h->last_present_seconds;
    h->max_present_seconds = fmax(h->max_present_seconds, h->last_present_seconds);
    if (ok) {
        h->frames++;
        h->last_frame = now_seconds();
        h->needs_draw = 0;
        if (h->update_pending) {
            double finished = h->last_frame;
            h->last_scene_render_seconds = h->last_draw_seconds + h->last_present_seconds;
            h->scene_render_seconds += h->last_scene_render_seconds;
            h->max_scene_render_seconds = fmax(h->max_scene_render_seconds, h->last_scene_render_seconds);
            h->last_update_draw_seconds = h->last_draw_seconds;
            h->last_update_present_seconds = h->last_present_seconds;
            if (h->terminal) {
                pt_last_work(h->terminal, &h->last_scene_commands_touched, &h->last_scene_commands_replayed);
                h->last_scene_segments_tested = pt_last_segment_tests(h->terminal);
            } else {
                px_renderer_last_work(h->renderer, &h->last_scene_commands_touched, &h->last_scene_commands_replayed);
                h->last_scene_segments_tested = px_renderer_last_segment_tests(h->renderer);
            }
            h->last_scene_update_seconds = finished - h->update_started;
            h->scene_update_seconds += h->last_scene_update_seconds;
            h->max_scene_update_seconds = fmax(h->max_scene_update_seconds, h->last_scene_update_seconds);
            h->update_samples[h->update_sample_next] = h->last_scene_update_seconds;
            h->update_sample_next = (h->update_sample_next + 1) % 120;
            if (h->update_sample_count < 120)
                h->update_sample_count++;
            h->update_pending = 0;
        }
    }
    return ok;
}

static const char *str(const cJSON *j, const char *key) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsString(v) ? v->valuestring : "";
}

static double number(const cJSON *j, const char *key, double fallback) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(j, key);
    return cJSON_IsNumber(v) && isfinite(v->valuedouble) ? v->valuedouble : fallback;
}

static cJSON *backend_call(Host *h, const cJSON *j, char *error, int n) {
    return h->terminal ? pt_call(h->terminal, j, error, n) : px_renderer_call(h->renderer, j, error, n);
}

static void configure(Host *h, const cJSON *j) {
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(j, "continuous");
    if (cJSON_IsBool(v))
        h->continuous = cJSON_IsTrue(v);
    v = cJSON_GetObjectItemCaseSensitive(j, "redraw_interval");
    if (v) {
        h->continuous = cJSON_IsNumber(v);
        h->interval = fmax(0, number(j, "redraw_interval", 0));
    }
    v = cJSON_GetObjectItemCaseSensitive(j, "fps_limit");
    if (v)
        h->fps_limit = fmax(0, number(j, "fps_limit", 0));
}

static void process_command(Host *h, uint32_t id, const unsigned char *data, size_t length) {
    const char *end = NULL, *op;
    char error[1024] = {0};
    double command_started = now_seconds();
    cJSON *j = cJSON_ParseWithLengthOpts((const char *)data, length, &end, 0), *result = NULL;
    if (!j || end != (const char *)data + length || !cJSON_IsObject(j)) {
        cJSON_Delete(j);
        reply(h, id, NULL, "Invalid JSON command");
        return;
    }
    op = str(j, "op");
    h->commands++;
    if (!strcmp(op, "close")) {
        reply(h, id, NULL, NULL);
        begin_close(h, "closed", NULL);
        goto done;
    }
    if (!strcmp(op, "open")) {
        if (h->initialized) {
            reply(h, id, NULL, "Host already open");
            goto done;
        }
        if (!strcmp(str(j, "backend"), "terminal"))
            h->terminal = pt_open(j, error, sizeof(error));
        else if (!strcmp(str(j, "backend"), "window") || !*str(j, "backend"))
            h->renderer = px_renderer_open(j, error, sizeof(error));
        else
            snprintf(error, sizeof(error), "Unsupported native backend");
        h->initialized = h->terminal != NULL || h->renderer != NULL;
        if (h->initialized) {
            configure(h, j);
            result = info(h);
        }
        reply(h, id, result, h->initialized ? NULL : (*error ? error : "Could not open backend"));
        goto done;
    }
    if (!h->initialized) {
        reply(h, id, NULL, "Open the backend first");
        goto done;
    }
    if (!strcmp(op, "present")) {
        int ok = h->has_scene;
        if (ok) {
            if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "scheduled")))
                h->needs_draw = 1;
            else
                ok = draw(h, error, sizeof(error));
        }
        reply(h, id, NULL, ok ? NULL : (*error ? error : "Commit a scene before presenting"));
        goto done;
    }
    if (!strcmp(op, "stats")) {
        result = statistics(h);
        if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "reset"))) {
            h->started = now_seconds();
            h->frames = h->commands = h->events = h->scene_updates = 0;
            h->draw_seconds = h->present_seconds = 0;
            h->last_draw_seconds = h->max_draw_seconds = h->last_present_seconds = h->max_present_seconds = 0;
            h->scene_update_seconds = h->last_scene_update_seconds = h->max_scene_update_seconds = 0;
            h->scene_commit_seconds = h->last_scene_commit_seconds = h->max_scene_commit_seconds = 0;
            h->scene_render_seconds = h->last_scene_render_seconds = h->max_scene_render_seconds = 0;
            h->last_update_draw_seconds = h->last_update_present_seconds = 0;
            h->update_sample_count = h->update_sample_next = 0;
            h->last_scene_commands_touched = h->last_scene_commands_replayed = h->last_scene_segments_tested = 0;
            if (h->terminal)
                pt_reset_stats(h->terminal);
            else
                px_renderer_reset_stats(h->renderer);
        }
        reply(h, id, result, NULL);
        goto done;
    }
    if (!strcmp(op, "frame") || !strcmp(op, "patch")) {
        const cJSON *commands = cJSON_GetObjectItemCaseSensitive(j, "commands");
        int ok;
        cJSON *settings = cJSON_CreateObject(), *answer;
        double update_started = command_started, commit_started, render_started;
        cJSON_AddStringToObject(settings, "op", "configure");
        cJSON_AddNumberToObject(settings, "transition_ms",
                                fmax(0, fmin(5000, 1000 * number(j, "transition_seconds", 0))));
        answer = backend_call(h, settings, error, sizeof(error));
        cJSON_Delete(answer);
        cJSON_Delete(settings);
        commit_started = now_seconds();
        if (!strcmp(op, "patch"))
            ok = h->terminal ? pt_patch(h->terminal, j, error, sizeof(error))
                             : px_renderer_patch(h->renderer, j, error, sizeof(error));
        else
            ok = h->terminal ? pt_commit(h->terminal, commands, error, sizeof(error))
                             : px_renderer_commit(h->renderer, commands, error, sizeof(error));
        render_started = now_seconds();
        if (ok) {
            h->last_scene_commit_seconds = render_started - commit_started;
            h->scene_commit_seconds += h->last_scene_commit_seconds;
            h->max_scene_commit_seconds = fmax(h->max_scene_commit_seconds, h->last_scene_commit_seconds);
            h->has_scene = 1;
            h->scene_updates++;
            /* Commit status is known. Draw after the acknowledgement is queued
               so Python does not wait for presentation or VSync. */
            h->needs_draw = 1;
            h->update_pending = 1;
            h->update_started = update_started;
        }
        reply(h, id, NULL, ok ? NULL : (*error ? error : "Could not commit scene"));
        goto done;
    }
    result = backend_call(h, j, error, sizeof(error));
    if (result && !strcmp(op, "configure"))
        configure(h, j);
    if (result && !strcmp(op, "reload_image") && h->has_scene)
        h->needs_draw = 1;
    reply(h, id, result, result ? NULL : (*error ? error : "Unsupported native operation"));
done:
    cJSON_Delete(j);
}

/* NETWORK_HELPERS */
static int would_block(void) {
#ifdef _WIN32
    int error = WSAGetLastError();
    return error == WSAEWOULDBLOCK || error == WSAEINPROGRESS || error == WSAEINTR;
#else
    return errno == EAGAIN || errno == EWOULDBLOCK || errno == EINPROGRESS || errno == EINTR;
#endif
}

static void close_socket(PxSocket socket) {
    if (socket == PX_BAD_SOCKET)
        return;
#ifdef _WIN32
    closesocket(socket);
#else
    close(socket);
#endif
}

static PxSocket connect_parent(unsigned int port) {
    PxSocket socket_fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    struct sockaddr_in address;
    fd_set writing, exception;
    struct timeval timeout;
    int one = 1, result, error = 0;
#ifdef _WIN32
    u_long nonblocking = 1;
    int error_length = sizeof(error);
#else
    socklen_t error_length = sizeof(error);
#endif
    if (socket_fd == PX_BAD_SOCKET)
        return socket_fd;
#ifdef _WIN32
    if (ioctlsocket(socket_fd, FIONBIO, &nonblocking) != 0)
        goto failed;
#else
    if (fcntl(socket_fd, F_SETFL, fcntl(socket_fd, F_GETFL, 0) | O_NONBLOCK) < 0)
        goto failed;
#endif
    (void)setsockopt(socket_fd, IPPROTO_TCP, TCP_NODELAY, (const char *)&one, sizeof(one));
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons((unsigned short)port);
    /* POSIX feature macros hide INADDR_LOOPBACK on macOS. inet_pton is portable. */
    if (inet_pton(AF_INET, "127.0.0.1", &address.sin_addr) != 1)
        goto failed;
    result = connect(socket_fd, (struct sockaddr *)&address, sizeof(address));
    if (result != 0) {
        if (!would_block())
            goto failed;
        FD_ZERO(&writing);
        FD_ZERO(&exception);
        FD_SET(socket_fd, &writing);
        FD_SET(socket_fd, &exception);
        timeout.tv_sec = 5;
        timeout.tv_usec = 0;
        result = select((int)socket_fd + 1, NULL, &writing, &exception, &timeout);
        if (result <= 0 || getsockopt(socket_fd, SOL_SOCKET, SO_ERROR, (char *)&error, &error_length) != 0 || error)
            goto failed;
    }
    return socket_fd;
failed:
    close_socket(socket_fd);
    return PX_BAD_SOCKET;
}

static void flush_output(Host *h) {
    size_t budget = IO_BUDGET;
    while (h->output_size && budget) {
        size_t amount = h->output_size < budget ? h->output_size : budget;
        int sent = (int)send(h->socket, (const char *)h->output + h->output_start, (int)amount, 0);
        if (sent < 0) {
            if (!would_block())
                h->disconnected = 1;
            return;
        }
        if (sent == 0) {
            h->disconnected = 1;
            return;
        }
        h->output_start += (size_t)sent;
        h->output_size -= (size_t)sent;
        budget -= (size_t)sent;
    }
    if (!h->output_size)
        h->output_start = 0;
}

static void receive_input(Host *h) {
    size_t budget = IO_BUDGET;
    while (h->input_size < INPUT_CAP && budget) {
        size_t amount = INPUT_CAP - h->input_size;
        int received;
        if (amount > budget)
            amount = budget;
        received = (int)recv(h->socket, (char *)h->input + h->input_size, (int)amount, 0);
        if (received < 0) {
            if (!would_block())
                h->disconnected = 1;
            return;
        }
        if (received == 0) {
            h->disconnected = 1;
            return;
        }
        h->input_size += (size_t)received;
        budget -= (size_t)received;
    }
}

static void process_input(Host *h) {
    unsigned count = 0;
    size_t consumed = 0;
    while (!h->closing && !h->disconnected && count < COMMAND_BUDGET && h->output_size <= COMMAND_WATERMARK &&
           h->input_size - consumed >= HEADER_SIZE) {
        unsigned char *p = h->input + consumed;
        uint32_t op = read_le32(p + 4), id = read_le32(p + 8), length = read_le32(p + 12);
        if (memcmp(p, "PXN1", 4) || op != OP_COMMAND || !id || id <= h->last_request || length > MAX_PAYLOAD) {
            h->protocol_error = 1;
            h->disconnected = 1;
            break;
        }
        if ((size_t)length > h->input_size - consumed - HEADER_SIZE)
            break;
        h->last_request = id;
        process_command(h, id, p + HEADER_SIZE, length);
        consumed += HEADER_SIZE + length;
        count++;
    }
    if (consumed) {
        memmove(h->input, h->input + consumed, h->input_size - consumed);
        h->input_size -= consumed;
    }
}

static int pending_input(Host *h) {
    return h->input_size >= HEADER_SIZE && h->output_size <= COMMAND_WATERMARK &&
           (read_le32(h->input + 12) > MAX_PAYLOAD || h->input_size >= HEADER_SIZE + (size_t)read_le32(h->input + 12));
}

static int wants_frame(Host *h) {
    return h->initialized && h->has_scene && !h->closing && !h->suspended &&
           (h->needs_draw || h->continuous || (h->renderer && px_renderer_animating(h->renderer)));
}

static double frame_delay(Host *h) {
    double interval;
    if (h->last_frame == 0)
        return 0;
    /* Changes need not wait for a continuous replay, but share its FPS cap. */
    interval = h->continuous && !h->needs_draw ? h->interval : 0;
    if (h->fps_limit > 0)
        interval = fmax(interval, 1 / h->fps_limit);
    return fmax(0, h->last_frame + interval - now_seconds());
}

static void wait_for_io(Host *h) {
    fd_set reading, writing;
    struct timeval timeout;
    double delay = 0.01;
    int result;
    int can_read = !h->closing && h->output_size <= COMMAND_WATERMARK && h->input_size < INPUT_CAP;
    if (pending_input(h))
        delay = 0;
    else if (wants_frame(h))
        delay = fmin(delay, frame_delay(h));
    timeout.tv_sec = 0;
    timeout.tv_usec = (long)(delay * 1e6);
    FD_ZERO(&reading);
    FD_ZERO(&writing);
    if (can_read)
        FD_SET(h->socket, &reading);
    if (h->output_size)
        FD_SET(h->socket, &writing);
    result = select((int)h->socket + 1, can_read ? &reading : NULL, h->output_size ? &writing : NULL, NULL, &timeout);
    if (result < 0 && !would_block())
        h->disconnected = 1;
}

int main(int argc, char **argv) {
    Host h;
    unsigned port = 0;
    const char *token = NULL;
    int i, code = 0;
#ifdef _WIN32
    WSADATA wsa;
#else
    signal(SIGPIPE, SIG_IGN);
    signal(SIGTERM, stop_signal);
    signal(SIGHUP, stop_signal);
    signal(SIGINT, stop_signal);
#endif
    memset(&h, 0, sizeof(h));
    h.socket = PX_BAD_SOCKET;
    for (i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--port") && i + 1 < argc) {
            char *end;
            unsigned long p = strtoul(argv[++i], &end, 10);
            if (*end || !p || p > 65535)
                return 2;
            port = (unsigned)p;
        } else if (!strcmp(argv[i], "--token") && i + 1 < argc)
            token = argv[++i];
        else
            return 2;
    }
    if (!port || !token || strlen(token) != 64)
        return 2;
    for (i = 0; i < 64; i++)
        if (!strchr("0123456789abcdefABCDEF", token[i]))
            return 2;
#ifdef _WIN32
    if (WSAStartup(MAKEWORD(2, 2), &wsa))
        return 2;
#endif
    h.input = malloc(INPUT_CAP);
    h.output = malloc(OUTPUT_CAP);
    h.started = now_seconds();
    if (!h.input || !h.output || (h.socket = connect_parent(port)) == PX_BAD_SOCKET) {
        code = 2;
        goto cleanup;
    }
    if (!queue_frame(&h, OP_HELLO, 0, token, 64)) {
        code = 2;
        goto cleanup;
    }
    while (!h.disconnected) {
        char error[1024] = {0};
#ifndef _WIN32
        if (interrupted)
            break;
#endif
        flush_output(&h);
        if (h.closing && (!h.output_size || now_seconds() >= h.close_deadline))
            break;
        if (!h.closing && h.output_size <= COMMAND_WATERMARK)
            receive_input(&h);
        if (h.disconnected)
            break;
        process_input(&h);
        flush_output(&h);
        if (h.initialized && !h.closing) {
            cJSON *events = h.terminal ? pt_poll(h.terminal) : px_renderer_poll(h.renderer), *event;
            cJSON_ArrayForEach(event, events) {
                if (!strcmp(str(event, "kind"), "suspend"))
                    h.suspended = 1;
                else if (!strcmp(str(event, "kind"), "resume"))
                    h.suspended = 0;
                h.events++;
                if (!queue_json(&h, OP_EVENT, 0, cJSON_Duplicate(event, 1))) {
                    begin_close(&h, "event_overload", "Native input queue exhausted");
                    break;
                }
            }
            cJSON_Delete(events);
        }
        if (wants_frame(&h) && frame_delay(&h) <= 0 && !draw(&h, error, sizeof(error)))
            begin_close(&h, "native_error", error);
        if (!h.disconnected)
            wait_for_io(&h);
    }
    if (h.protocol_error) {
        fprintf(stderr, "Invalid native protocol frame\n");
        code = 2;
    }
cleanup:
    if (h.renderer)
        px_renderer_close(h.renderer);
    if (h.terminal)
        pt_close(h.terminal);
    close_socket(h.socket);
    free(h.input);
    free(h.output);
#ifdef _WIN32
    WSACleanup();
#endif
    return code;
}
