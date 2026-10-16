/* Include the production host, replacing only external APIs in this test TU.
   No real sockets, windows, sleeps, or SDL dependency are used at runtime. */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include "px_backend.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static WSAEVENT WSAAPI fake_create(void);
static int WSAAPI fake_associate(SOCKET socket, WSAEVENT event, long mask);
static BOOL WSAAPI fake_close(WSAEVENT event);
static DWORD WINAPI fake_wait(DWORD count, const HANDLE *handles, DWORD timeout, DWORD mask, DWORD flags);
static int WSAAPI fake_enum(SOCKET socket, WSAEVENT event, LPWSANETWORKEVENTS events);
static int WSAAPI fake_select(int count, fd_set *reading, fd_set *writing, fd_set *errors,
                              const struct timeval *timeout);
static int WSAAPI fake_send(SOCKET socket, const char *data, int length, int flags);
static int WSAAPI fake_error(void);
static int fake_pending(PXRenderer *renderer);

#define PYSUAL_WITH_WINDOW
#define main unused_host_main
#define WSACreateEvent fake_create
#define WSAEventSelect fake_associate
#define WSACloseEvent fake_close
#define MsgWaitForMultipleObjectsEx fake_wait
#define WSAEnumNetworkEvents fake_enum
#define select fake_select
#define send fake_send
#define WSAGetLastError fake_error
#define px_renderer_poll_pending fake_pending
#include "host.c"
#undef main
#undef WSACreateEvent
#undef WSAEventSelect
#undef WSACloseEvent
#undef MsgWaitForMultipleObjectsEx
#undef WSAEnumNetworkEvents
#undef select
#undef send
#undef WSAGetLastError
#undef px_renderer_poll_pending
#undef PYSUAL_WITH_WINDOW
#include "window_stub.c"

static struct {
    int create_fail, associate_fail, enum_fail, cancel_fail, pending, send_blocks;
    int creates, associations, cancellations, closes, waits, enums, selects, sends;
    int bad_arguments, sequence, wait_sequence, enum_sequence;
    int select_reads, select_writes;
    DWORD wait_result, wait_timeout;
    long select_timeout;
} fake;

static WSAEVENT event_handle(void) {
    return (WSAEVENT)(uintptr_t)123;
}
static Host fresh_host(void) {
    Host h;
    memset(&h, 0, sizeof(h));
    memset(&fake, 0, sizeof(fake));
    fake.wait_result = WAIT_TIMEOUT;
    h.socket = (SOCKET)1;
    h.network_event = WSA_INVALID_EVENT;
    h.renderer = (PXRenderer *)(uintptr_t)1;
    return h;
}
static WSAEVENT WSAAPI fake_create(void) {
    ++fake.creates;
    return fake.create_fail ? WSA_INVALID_EVENT : event_handle();
}
static int WSAAPI fake_associate(SOCKET socket, WSAEVENT event, long mask) {
    fake.bad_arguments |= socket != (SOCKET)1;
    if (!mask) {
        ++fake.cancellations;
        fake.bad_arguments |= event != NULL;
        return fake.cancel_fail ? SOCKET_ERROR : 0;
    }
    ++fake.associations;
    fake.bad_arguments |= event != event_handle() || mask != (FD_READ | FD_WRITE | FD_CLOSE);
    return fake.associate_fail ? SOCKET_ERROR : 0;
}
static BOOL WSAAPI fake_close(WSAEVENT event) {
    ++fake.closes;
    fake.bad_arguments |= event != event_handle();
    return TRUE;
}
static DWORD WINAPI fake_wait(DWORD count, const HANDLE *handles, DWORD timeout, DWORD mask, DWORD flags) {
    ++fake.waits;
    fake.wait_sequence = ++fake.sequence;
    fake.wait_timeout = timeout;
    fake.bad_arguments |=
        count != 1 || handles[0] != event_handle() || mask != QS_ALLINPUT || flags != MWMO_INPUTAVAILABLE;
    return fake.wait_result;
}
static int WSAAPI fake_enum(SOCKET socket, WSAEVENT event, LPWSANETWORKEVENTS events) {
    ++fake.enums;
    fake.enum_sequence = ++fake.sequence;
    fake.bad_arguments |= socket != (SOCKET)1 || event != event_handle();
    memset(events, 0, sizeof(*events));
    events->lNetworkEvents = FD_READ | FD_WRITE | FD_CLOSE;
    return fake.enum_fail ? SOCKET_ERROR : 0;
}
static int WSAAPI fake_select(int count, fd_set *reading, fd_set *writing, fd_set *errors,
                              const struct timeval *timeout) {
    (void)count;
    (void)errors;
    ++fake.selects;
    fake.select_reads = reading != NULL;
    fake.select_writes = writing != NULL;
    fake.select_timeout = timeout->tv_usec;
    return 0;
}
static int WSAAPI fake_send(SOCKET socket, const char *data, int length, int flags) {
    (void)socket;
    (void)data;
    (void)flags;
    ++fake.sends;
    return fake.send_blocks ? SOCKET_ERROR : length;
}
static int WSAAPI fake_error(void) {
    return WSAEWOULDBLOCK;
}
static int fake_pending(PXRenderer *renderer) {
    (void)renderer;
    return fake.pending;
}

#define CHECK(expression)                                                                                              \
    do {                                                                                                               \
        if (!(expression) || fake.bad_arguments) {                                                                     \
            fprintf(stderr, "line %d: %s (bad API arguments: %d)\n", __LINE__, #expression, fake.bad_arguments);       \
            return 1;                                                                                                  \
        }                                                                                                              \
    } while (0)

int main(void) {
    Host h;
    int i;
    h = fresh_host();
    fake.create_fail = 1;
    wait_for_io(&h);
    wait_for_io(&h);
    CHECK(fake.creates == 1 && fake.associations == 0 && fake.selects == 2 && fake.select_timeout == 10000);
    CHECK(!h.disconnected && h.network_event == WSA_INVALID_EVENT);

    h = fresh_host();
    fake.associate_fail = 1;
    wait_for_io(&h);
    CHECK(fake.associations == 1 && fake.closes == 1 && fake.selects == 1 && !h.disconnected);
    CHECK(h.network_event == WSA_INVALID_EVENT);

    /* Wait/enum errors cancel safely and permanently fall back to select. */
    for (i = 0; i < 2; ++i) {
        h = fresh_host();
        fake.wait_result = i ? WAIT_OBJECT_0 : WAIT_FAILED;
        fake.enum_fail = i;
        wait_for_io(&h);
        wait_for_io(&h);
        CHECK(fake.creates == 1 && fake.waits == 1 && fake.enums == i);
        CHECK(fake.cancellations == 1 && fake.closes == 1 && fake.selects == 2 && !h.disconnected);
        CHECK(h.network_event == WSA_INVALID_EVENT);
    }

    /* A failed cancellation must leave the handle alive for socket-first cleanup. */
    for (i = 0; i < 2; ++i) {
        h = fresh_host();
        fake.wait_result = i ? WAIT_OBJECT_0 : WAIT_FAILED;
        fake.enum_fail = i;
        fake.cancel_fail = 1;
        wait_for_io(&h);
        CHECK(h.disconnected && fake.cancellations == 1 && fake.closes == 0 && fake.selects == 0);
        CHECK(h.network_event == event_handle());
    }

    /* A full SDL batch returns before either wait is armed. */
    h = fresh_host();
    fake.pending = 1;
    wait_for_io(&h);
    CHECK(fake.creates == 0 && fake.waits == 0 && fake.selects == 0);
    fake.pending = 0;
    CHECK(wait_for_windows_input(&h, 0) == 1 && fake.waits == 0);
    CHECK(wait_for_windows_input(&h, .0005) == 0 && fake.waits == 0);
    CHECK(wait_for_windows_input(&h, .0015) == 1 && fake.wait_timeout == 1);

    /* A byte-budget limit needs immediate progress; real blocking needs FD_WRITE. */
    h = fresh_host();
    h.output = malloc(IO_BUDGET + 1);
    CHECK(h.output != NULL);
    h.output_size = IO_BUDGET + 1;
    flush_output(&h);
    CHECK(h.output_size == 1 && fake.sends == 1 && !h.socket_write_blocked);
    wait_for_io(&h);
    CHECK(fake.waits == 0 && fake.selects == 0);
    fake.send_blocks = 1;
    flush_output(&h);
    CHECK(h.output_size == 1 && h.socket_write_blocked && !h.disconnected);
    wait_for_io(&h);
    CHECK(fake.waits == 1 && fake.wait_timeout == 10 && fake.selects == 0);
    fake.send_blocks = 0;
    flush_output(&h);
    CHECK(h.output_size == 0 && h.output_start == 0 && !h.socket_write_blocked);
    free(h.output);

    h = fresh_host();
    fake.wait_result = WAIT_OBJECT_0;
    wait_for_io(&h);
    CHECK(fake.enums == 1 && fake.enum_sequence > fake.wait_sequence && !h.disconnected);
    /* A network close notification wakes normal recv/send; it does not drop data. */
    fake.wait_result = WAIT_OBJECT_0 + 1;
    wait_for_io(&h);
    fake.wait_result = WAIT_TIMEOUT;
    wait_for_io(&h);
    CHECK(fake.waits == 3 && fake.enums == 1 && fake.closes == 0);

    /* Terminal, pre-open, and close-drain hosts wait with select. */
    h = fresh_host();
    h.renderer = NULL;
    wait_for_io(&h);
    h.terminal = (PTBackend *)(uintptr_t)1;
    wait_for_io(&h);
    CHECK(fake.creates == 0 && fake.selects == 2 && fake.select_reads && !fake.select_writes);
    h = fresh_host();
    h.closing = 1;
    h.output_size = 1;
    fake.pending = 1;
    wait_for_io(&h);
    CHECK(fake.creates == 0 && fake.selects == 1 && !fake.select_reads && fake.select_writes);
    return 0;
}
