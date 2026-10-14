"""Event-driven client for the standalone C host. There is no frame timer here."""
from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import secrets
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, NotRequired, TypedDict, TypeGuard, cast, overload

HEADER = struct.Struct('<4sIII')
MAX_PAYLOAD = 16 * 1024 * 1024
RECEIVE_CHUNK = 256 * 1024
U32 = struct.Struct('<I')


def native_executable() -> Path:
    """Resolve the helper without importing graphics libraries or starting it."""
    return Path(os.environ.get('PYSUAL_HOST') or (Path(__file__).resolve().parents[1] /
                'bin' / ('pysual-host.exe' if sys.platform == 'win32' else 'pysual-host')))


class NativeHostError(RuntimeError):
    pass


# Keep this text equal to the rejection in native/host.c. Raw frames bypass Python.
_NUL_ERROR = 'Embedded NUL is not representable in a native C string'

# One shape for a native payload. Declared at the request boundary so the
# embedded-NUL walk can use isinstance and dict.items directly.
_Json = None | bool | int | float | str | list['_Json'] | tuple['_Json', ...] | dict[str, '_Json']


def _is_json_object(value: object) -> TypeGuard[dict[str, _Json]]:
    return isinstance(value, dict)


@overload
def display_text(value: str) -> str: ...


@overload
def display_text(value: object) -> object: ...


def display_text(value: object) -> object:
    """Drop U+0000 from text that is measured, drawn, or shown as a title.

    Characters on either side stay in order. Terminal painting removes this
    same character; its other control-character rules stay terminal-specific.
    """
    if isinstance(value, str) and '\0' in value:
        return value.replace('\0', '')
    return value


def _contains_embedded_nul(value: _Json) -> bool:
    if isinstance(value, str):
        return '\0' in value
    if isinstance(value, dict):
        return any(_contains_embedded_nul(key) or _contains_embedded_nul(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_contains_embedded_nul(item) for item in value)
    return False


def _sanitize_commands(commands: _Json) -> _Json:
    if not isinstance(commands, (list, tuple)):
        return commands
    sanitized: list[_Json] | None = None
    for index, command in enumerate(commands):
        if not isinstance(command, (list, tuple)) or len(command) < 2:
            continue
        text = command[1]
        if command[0] != 'text' or not isinstance(text, str) or '\0' not in text:
            continue
        if sanitized is None:
            sanitized = list(commands)
        sanitized[index] = [command[0], display_text(text), *command[2:]]
    return commands if sanitized is None else sanitized


def _sanitize_segment(segment: _Json) -> _Json:
    if not isinstance(segment, dict):
        return segment
    commands = segment.get('commands')
    sanitized = _sanitize_commands(commands)
    if sanitized is commands:
        return segment
    return {**segment, 'commands': sanitized}


def _sanitize_display_nuls(op: str, fields: dict[str, _Json]) -> dict[str, _Json]:
    copied = dict(fields)
    if op == 'measure':
        text = copied.get('text')
        if isinstance(text, str):
            copied['text'] = display_text(text)
    elif op == 'measure_many':
        texts = copied.get('texts')
        if isinstance(texts, (list, tuple)):
            copied['texts'] = [display_text(item) if isinstance(item, str) else item for item in texts]
    elif op in ('set_title', 'open'):
        title = copied.get('title')
        if isinstance(title, str):
            copied['title'] = display_text(title)
    elif op == 'frame':
        copied['commands'] = _sanitize_commands(copied.get('commands'))
    elif op == 'patch':
        upsert = copied.get('upsert')
        if isinstance(upsert, (list, tuple)):
            copied['upsert'] = [_sanitize_segment(segment) for segment in upsert]
    return copied


def prepare_native_request(op: str, fields: object) -> dict[str, _Json]:
    """Apply the embedded-NUL policy before a native request is serialized.

    Display text, measurements and titles drop U+0000. Clipboard text, paths,
    identifiers and every other string are rejected. The C host stores strings
    with a terminating zero, so leaving the character in place would hide the
    suffix during measurement, drawing, JSON duplication and service calls.
    """
    if not _is_json_object(fields):
        raise TypeError('Native request fields must be a dictionary')
    if '\0' not in op and not _contains_embedded_nul(fields):
        return fields
    sanitized = _sanitize_display_nuls(op, fields)
    if '\0' in op or _contains_embedded_nul(sanitized):
        raise ValueError(_NUL_ERROR)
    return sanitized


def _read_handshake(sock: socket.socket, check_startup: Callable[[], None]) -> bytes:
    # Poll actual cancellation/process failure without imposing an elapsed
    # deadline on a live helper, including a partially received handshake.
    sock.settimeout(0.1)

    def exact(length: int) -> bytes:
        parts = bytearray()
        while len(parts) < length:
            check_startup()
            try:
                data = sock.recv(length - len(parts))
            except socket.timeout:
                continue
            if not data:
                raise NativeHostError('Native host disconnected during handshake')
            parts.extend(data)
        return bytes(parts)
    magic, opcode, request, length = cast(
        tuple[object, object, object, object], HEADER.unpack(exact(HEADER.size))
    )
    if magic != b'PXN1' or opcode != 1 or request != 0 or length != 64:
        raise NativeHostError('Invalid native host handshake')
    return exact(cast(int, length))


class _Pending(TypedDict):
    event: threading.Event
    result: NotRequired[object]
    error: NotRequired[BaseException]


class NativeClient:
    """One session. Callback runs on the reader thread and must not block it.

    Public requests wait for explicit native acknowledgements. Reads block in
    select until real protocol data arrives; no per-frame polling is performed.
    """
    def __init__(
        self,
        on_event: Callable[[object], None] | None = None,
        *,
        executable: str | Path | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._on_event: Callable[[object], None] = on_event or (lambda payload: None)
        self._executable = Path(executable) if executable is not None else native_executable()
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        self._timeout = float(timeout)
        self._opening = True
        self._process: subprocess.Popen[bytes] | None = None
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._send_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._close_lock = threading.Lock()
        self._pending: dict[int, _Pending] = {}
        self._next_id = 1
        self._closed = threading.Event()
        self._closed_payload: object = None
        self._transport_error: BaseException | None = None
        self._stderr: deque[str] = deque(maxlen=32)
        self._counts: dict[str, int] = dict(requests_sent=0, replies_received=0,
                            input_events=0, closed_events=0,
                            bytes_sent=0, bytes_received=0)

    @property
    def is_alive(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def diagnostics(self) -> dict[str, object]:
        with self._state_lock:
            return {**self._counts, 'stderr_tail': ''.join(self._stderr)[-8192:],
                    'pid': self._process.pid if self._process else None,
                    'closed': self._closed.is_set()}

    @property
    def process(self) -> subprocess.Popen[bytes] | None:
        return self._process

    def _check_startup(self) -> None:
        if self._closed.is_set():
            raise NativeHostError('Native host closed during startup')
        process = self._process
        if process is None:
            raise NativeHostError('Native host is not running')
        code = process.poll()
        if code is not None:
            raise NativeHostError(f'Native host exited during startup (code {code})')

    def _start(self, *, terminal: bool = False) -> None:
        if self._process is not None or self._closed.is_set():
            raise NativeHostError('Create a new NativeClient for each opening')
        if not self._executable.is_file():
            raise NativeHostError(
                'Native host is missing. Install a matching native wheel, or run '
                'python -m pysual native from the repository checkout. '
                'Alternatively use --backend web or --backend terminal --terminal python. '
                'Build instructions: https://github.com/PabloMP2P/Pysual/blob/main/docs/building.md'
            )
        token = secrets.token_hex(32)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(4)
        listener.settimeout(0.1)
        try:
            self._process = subprocess.Popen(
                [str(self._executable.resolve()), '--port', str(listener.getsockname()[1]), '--token', token],
                stdin=None, stdout=None, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' and not terminal else 0,
                cwd=str(self._executable.resolve().parent))
            self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True,
                                                    name='Pysual native diagnostics')
            self._stderr_reader.start()
            while True:
                self._check_startup()
                try:
                    candidate, _ = listener.accept()
                except socket.timeout:
                    continue
                try:
                    received = _read_handshake(candidate, self._check_startup)
                    if not secrets.compare_digest(received, token.encode('ascii')):
                        candidate.close()
                        continue
                except (OSError, NativeHostError):
                    candidate.close()
                    continue
                self._socket = candidate
                break
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._socket.setblocking(False)
            self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                            name='Pysual native events')
            self._reader.start()
            return None
        except BaseException:
            self._abort('startup_failed')
            raise
        finally:
            listener.close()

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        stream = process.stderr
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    return
                with self._state_lock:
                    self._stderr.append(chunk.decode('utf-8', errors='replace'))
        finally:
            stream.close()

    def _receive_frames(self) -> Iterable[tuple[int, int, bytes]]:
        """Parse each received burst before waiting, retaining a partial frame.

        Reading the header and payload separately creates two socket/GIL
        handoffs per message. A burst normally contains several complete
        messages, while the buffer remains bounded by one maximum-size frame.
        """
        received = bytearray()
        while True:
            offset = 0
            while len(received) - offset >= HEADER.size:
                magic, opcode, request, length = cast(
                    tuple[object, object, object, object],
                    HEADER.unpack_from(received, offset),
                )
                if (
                    not isinstance(magic, bytes) or magic != b'PXN1'
                    or not isinstance(opcode, int) or not isinstance(request, int)
                    or not isinstance(length, int) or length > MAX_PAYLOAD
                ):
                    raise NativeHostError('Invalid response framing')
                end = offset + HEADER.size + length
                if end > len(received):
                    break
                yield opcode, request, bytes(received[offset + HEADER.size:end])
                offset = end
            if offset:
                del received[:offset]
            sock = self._socket
            if sock is None:
                raise EOFError('Native host transport is closed')
            select.select([sock], [], [])  # Indefinite event wait, no polling interval.
            try:
                part = sock.recv(min(RECEIVE_CHUNK, HEADER.size + MAX_PAYLOAD - len(received)))
            except BlockingIOError:
                continue
            if not part:
                raise EOFError('Native host disconnected')
            received.extend(part)

    def _read_loop(self) -> None:
        try:
            for opcode, request, raw in self._receive_frames():
                loaded: object = json.loads(raw.decode('utf-8'))
                if not isinstance(loaded, dict):
                    raise NativeHostError('Host response is not an object')
                payload = cast(dict[str, object], loaded)
                with self._state_lock:
                    self._counts['bytes_received'] += HEADER.size + len(raw)
                if opcode == 200:
                    with self._state_lock:
                        pending = self._pending.pop(request, None)
                        self._counts['replies_received'] += 1
                    if pending is None:
                        raise NativeHostError('Unexpected or duplicate host acknowledgement')
                    if payload.get('ok') is True:
                        pending['result'] = payload.get('result')
                    elif payload.get('ok') is False:
                        pending['error'] = NativeHostError(str(payload.get('error', 'Native request rejected')))
                    else:
                        pending['error'] = NativeHostError('Invalid acknowledgement')
                    pending['event'].set()
                elif opcode == 101 and request == 0:
                    with self._state_lock:
                        self._counts['input_events'] += 1
                    self._on_event(payload)
                elif opcode == 102 and request == 0:
                    self._notify_closed(payload)
                    return
                else:
                    raise NativeHostError('Unexpected host message')
        except BaseException as exc:
            if not self._closed.is_set():
                self._transport_error = NativeHostError(str(exc))
                self._notify_closed({'reason': 'transport_error', 'error': str(exc)})
        finally:
            self._fail_pending(self._transport_error or NativeHostError('Native host closed'))
            self._disconnect()

    def _notify_closed(self, payload: dict[str, object]) -> None:
        with self._state_lock:
            if self._closed.is_set():
                return
            self._closed_payload = payload
            self._counts['closed_events'] += 1
            self._closed.set()
        if payload.get('reason') not in ('closed', 'not_started'):
            error = payload.get('error', 'Native host closed unexpectedly')
            self._on_event({'kind': 'error', 'text': error})

    def _fail_pending(self, error: BaseException) -> None:
        with self._state_lock:
            pending, self._pending = self._pending, {}
        for value in pending.values():
            value['error'] = error
            value['event'].set()

    def _send(self, frame: bytes, timeout: float | None) -> None:
        deadline = None if timeout is None else time.monotonic() + timeout
        remaining = memoryview(frame)
        while remaining:
            sock = self._socket
            if sock is None:
                raise NativeHostError('Native transport closed')
            interval = None if deadline is None else deadline - time.monotonic()
            if (interval is not None and interval <= 0) or not select.select([], [sock], [], interval)[1]:
                raise TimeoutError('Native host did not accept the update before the deadline')
            try:
                sent = sock.send(remaining)
            except BlockingIOError:
                continue
            if sent <= 0:
                raise NativeHostError('Native host disconnected during update')
            remaining = remaining[sent:]
        with self._state_lock:
            self._counts['requests_sent'] += 1
            self._counts['bytes_sent'] += len(frame)

    def _await_reply(self, pending: _Pending, timeout: float | None) -> bool:
        """Wait until the reader stores this reply, or the monotonic deadline passes.

        ``Event.wait`` can report success on a spurious wakeup without setting
        its flag, which on macOS accepted a still-missing acknowledgement.
        Trust the flag and the clock.
        """
        event = pending['event']
        if timeout is None:
            while not event.is_set():
                if event.wait() and not event.is_set():
                    time.sleep(0.005)
            return True
        deadline = time.monotonic() + timeout
        while not event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if event.wait(remaining) and not event.is_set():
                time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        return True

    def _request(self, opcode: int, payload: bytes = b'', *, timeout: float | None) -> object:
        if len(payload) > MAX_PAYLOAD:
            raise ValueError('Request exceeds the 16 MiB protocol limit')
        pending: _Pending = {'event': threading.Event()}
        try:
            with self._send_lock:
                with self._state_lock:
                    if self._closed.is_set() or self._socket is None:
                        raise NativeHostError('Native host is closed')
                    request = self._next_id
                    if request > 0xFFFFFFFF:
                        raise NativeHostError('Request ID space exhausted; reopen the host')
                    self._next_id += 1
                    self._pending[request] = pending
                self._send(HEADER.pack(b'PXN1', opcode, request, len(payload)) + payload, timeout)
        except BaseException as exc:
            self._transport_error = NativeHostError(str(exc))
            self._abort('transport_error')
            raise
        if not self._await_reply(pending, timeout):
            self._transport_error = NativeHostError('Native acknowledgement timed out')
            self._abort('transport_error')
            raise TimeoutError('Native acknowledgement timed out; session closed because commit status is unknown')
        if 'error' in pending:
            error = pending['error']
            if isinstance(error, BaseException):
                raise error
            raise NativeHostError('Native host failed')
        return pending.get('result')

    def request(self, op: str, **kwargs: object) -> Any:
        """Send one native operation and return its decoded JSON result.

        The payload shape depends on ``op`` (a mapping, a sequence, or none).
        """
        if not isinstance(op, str):
            raise TypeError('op must be a string')
        prepared = prepare_native_request(op, kwargs)
        payload = json.dumps({'op': op, **prepared}, ensure_ascii=False,
                             allow_nan=False, separators=(',', ':')).encode('utf-8')
        if len(payload) > MAX_PAYLOAD:
            raise ValueError('Request exceeds the 16 MiB protocol limit')
        if self._process is None:
            if op != 'open':
                raise NativeHostError('Open the native host first')
            self._start(terminal=prepared.get('backend') == 'terminal' and not
                        (prepared.get('hidden') or prepared.get('headless')))
        try:
            # The first content frame can legitimately take longer than an
            # ordinary request. EOF, process exit and explicit errors still fail
            # startup; elapsed time alone does not. Cleanup stays bounded.
            timeout = None if self._opening and op != 'close' else self._timeout
            result = self._request(9, payload, timeout=timeout)
            if op in ('frame', 'patch', 'present'):
                self._opening = False
            return result
        except BaseException:
            if op == 'open':
                self._abort('startup_failed')
            raise

    def _disconnect(self) -> None:
        with self._state_lock:
            sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()

    def _reap(self, timeout: float) -> None:
        if self._process is None:
            return
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=1.0)

    def _abort(self, reason: str) -> None:
        self._disconnect()
        self._fail_pending(self._transport_error or NativeHostError(reason))
        self._reap(0.5)
        self._notify_closed({'reason': reason, 'error': str(self._transport_error or reason)})

    def close(self) -> None:
        with self._close_lock:
            if self._process is None:
                self._notify_closed({'reason': 'not_started'})
                return
            if self._opening and not self._closed.is_set():
                # An opening send can wait without a deadline. Explicit cleanup
                # must disconnect it rather than queue behind its send lock.
                self._notify_closed({'reason': 'closed'})
                self._abort('closed')
            elif not self._closed.is_set() and self._socket is not None:
                try:
                    self.request('close')
                    if not self._closed.wait(min(self._timeout, 3.0)):
                        self._abort('close_timeout')
                except (OSError, NativeHostError, TimeoutError):
                    self._abort('close_failed')
            self._disconnect()
            self._reap(1.0)
            if not self._closed.is_set():
                self._notify_closed({'reason': 'closed'})
            current = threading.current_thread()
            for worker in (self._reader, self._stderr_reader):
                if worker and worker is not current:
                    worker.join(timeout=1.0)

    def wait(self, timeout: float | None = None) -> object:
        started = time.monotonic()
        if not self._closed.wait(timeout):
            raise TimeoutError('Native host is still running')
        remaining = None if timeout is None else max(0, timeout - (time.monotonic() - started))
        if self._process is not None:
            try:
                self._process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as exc:
                raise TimeoutError('Native process has not exited yet') from exc
        return self._closed_payload

    def __enter__(self) -> NativeClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
