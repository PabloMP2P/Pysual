"""A local Python application hosted by a small SVG browser framebuffer.

Only normalized input and host-service results travel back from the page. Layout,
painting, controls and application state always remain in the Python process.
The HTTP listener is loopback-only and every stateful request needs a per-launch
capability token. No filesystem paths or executable application code are served.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
from math import inf, isfinite
import re
import secrets
import socket
from socketserver import TCPServer
import sys
import threading
import time
from typing import NotRequired, TypedDict, TypeGuard, cast
from urllib.parse import SplitResult, parse_qs, urlsplit
import webbrowser
import zlib

from ..geometry import Rect
from ..host import CapabilityError, Input, MAX_TEXT_BYTES, TextFile, Viewport
from . import _font
from ._native import NativeServices
from ._web_svg import SVGRenderer, _FrameSnapshot


# A legal UTF-8 result can expand sixfold as JSON (e.g. NUL -> \u0000).
# The browser sends at most one large result in each bounded envelope.
_MAX_REQUEST = MAX_TEXT_BYTES * 6 + 65536
_MAX_CONNECTIONS = 12
_COALESCED_CHUNK_LIMIT = 64 * 1024
_INPUT_KINDS = frozenset(
    {
        "pointer_down",
        "pointer_up",
        "pointer_move",
        "pointer_cancel",
        "pointer_leave",
        "wheel",
        "key_down",
        "key_up",
        "text",
        "composition",
        "blur",
        "suspend",
        "resume",
        "viewport",
        "close",
        "repaint",
        "image_error",
    }
)
_INPUT_FIELDS = frozenset(Input.__dataclass_fields__)


def _number(value: object) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Coordinates must be finite numbers")
    try:
        finite = isfinite(value)
    except OverflowError:
        finite = False
    if not finite:
        raise ValueError("Coordinates must be finite numbers")
    return value


def _is_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _bounded_list(value: object, limit: int) -> list[object]:
    if not _is_list(value) or len(value) > limit:
        raise ValueError("Too many events or replies")
    return value


class _ViewportData(TypedDict):
    width: float
    height: float
    scale: NotRequired[float]
    safe_area: NotRequired[tuple[float, float, float, float]]
    keyboard_occlusion: NotRequired[float]


class _InputData(TypedDict):
    kind: str
    x: NotRequired[float]
    y: NotRequired[float]
    key: NotRequired[str]
    text: NotRequired[str]
    delta: NotRequired[float]
    shift: NotRequired[bool]
    ctrl: NotRequired[bool]
    button: NotRequired[int]
    pointer_id: NotRequired[int]
    pointer_kind: NotRequired[str]
    viewport: NotRequired[Viewport | None]
    paste: NotRequired[bool]


class _TransportPacket(TypedDict):
    revision: int
    closed: bool
    reset: NotRequired[bool]
    length: NotRequired[int]
    updates: NotRequired[list[object]]
    definitions: NotRequired[list[object]]
    remove_definitions: NotRequired[list[str]]
    image_sources: NotRequired[dict[str, str]]
    remove_images: NotRequired[list[str]]
    width: NotRequired[float]
    height: NotRequired[float]
    title: NotRequired[str]
    text_input: NotRequired[list[float] | None]
    commands: NotRequired[list[object]]


def _accepts_gzip(value: str) -> bool:
    """Select gzip only when a valid quality value permits it.

    Explicit gzip settings take precedence over a wildcard. Malformed or
    conflicting duplicate settings fail closed rather than enabling a coding
    the client may have excluded. An explicit identity preference is honored.
    """
    qualities: dict[str, float] = {}
    for item in value.split(","):
        parts = item.split(";")
        coding = parts[0].strip().lower()
        if coding not in {"gzip", "identity", "*"}:
            continue
        quality = 1.0
        if len(parts) > 1:
            parameter, _, weight = parts[1].strip().partition("=")
            weight = weight.strip()
            if (
                len(parts) != 2
                or parameter.strip().lower() != "q"
                or re.fullmatch(r"(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)", weight)
                is None
            ):
                quality = 0.0
            else:
                quality = float(weight)
        qualities[coding] = min(qualities.get(coding, 1.0), quality)
    quality = qualities.get("gzip", qualities.get("*", 0.0))
    return quality > 0 and quality >= qualities.get("identity", 0.0)


@dataclass
class _FrameCursor:
    # One delivered base per stream, even if publication outruns socket writes.
    snapshot: _FrameSnapshot | None = None
    metadata_revision: int = -1


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], adapter: LiveSVGHost) -> None:
        self.adapter = adapter
        self.token: str = adapter._token
        self.slots = threading.BoundedSemaphore(_MAX_CONNECTIONS)
        super().__init__(address, _Handler)

    def server_bind(self) -> None:
        # HTTPServer resolves the bound address with getfqdn(). This listener
        # is strictly numeric loopback; reverse DNS adds no information and
        # can block the shared UI owner while an OS resolver times out.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def process_request(self, request: object, client_address: object) -> None:
        if not self.slots.acquire(False):
            self.shutdown_request(cast(socket.socket, request))
            return
        try:
            super().process_request(cast(socket.socket, request), client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: object, client_address: object) -> None:
        try:
            assert isinstance(request, socket.socket)
            request.settimeout(25)
            super().process_request_thread(cast(socket.socket, request), client_address)
        finally:
            self.slots.release()


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 lets the browser reuse the input connection. The frame stream is
    # one long response; /frame remains for exports and older clients.
    protocol_version = "HTTP/1.1"
    server_version = "PysualWebLive/1"

    @property
    def adapter(self) -> LiveSVGHost:
        return cast(_Server, self.server).adapter

    @property
    def launch_token(self) -> str:
        return cast(_Server, self.server).token

    def log_message(self, format: str, *args: object) -> None:
        pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # A tab can disconnect during headers or the next keep-alive read.
            self.close_connection = True

    def _send(
        self,
        status: int,
        body: bytes | str = b"",
        content_type: str = "application/json; charset=utf-8",
        *,
        compress: bool = False,
        events_ack: bool = False,
    ) -> None:
        if isinstance(body, str):
            body = body.encode("utf-8")
        encoded = compress and _accepts_gzip(
            ",".join(self.headers.get_all("Accept-Encoding", []))
        )
        if encoded:
            compressor = zlib.compressobj(6, wbits=31)
            body = compressor.compress(body) + compressor.flush()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if compress:
            self.send_header("Vary", "Accept-Encoding")
        if encoded:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
            if events_ack
            else "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src data:; font-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        try:
            self.end_headers()
            self.wfile.write(body)
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
        ):
            self.close_connection = True

    def _token_matches(self, presented: str) -> bool:
        expected = self.adapter._token
        if len(presented) != len(expected):
            return False
        return secrets.compare_digest(
            presented.encode("utf-8"), expected.encode("ascii")
        )

    def _allowed(
        self, authenticated: bool = False, query_token: str | None = None
    ) -> bool:
        adapter = self.adapter
        origin = self.headers.get("Origin")
        if self.headers.get("Host") != adapter._authority or (
            origin and origin != adapter._origin
        ):
            self._send(403, '{"error":"Origin denied"}')
            return False
        if authenticated:
            presented = (
                query_token
                if query_token is not None
                else self.headers.get("X-Pysual-Token", "")
            )
            if not self._token_matches(presented):
                self._send(403, '{"error":"Invalid host token"}')
                return False
        return True

    def _write_chunk(self, payload: bytes) -> None:
        header = f"{len(payload):X}\r\n".encode("ascii")
        if len(payload) <= _COALESCED_CHUNK_LIMIT:
            # One socket write for small events; avoid copying large resets.
            self.wfile.write(b"".join((header, payload, b"\r\n")))
        else:
            self.wfile.write(header)
            self.wfile.write(payload)
            self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream(self, parsed: SplitResult) -> None:
        token = parse_qs(parsed.query).get("token", [""])[0]
        if not self._allowed(True, query_token=token):
            return
        compressor = (
            zlib.compressobj(6, wbits=31)
            if _accepts_gzip(
                ",".join(self.headers.get_all("Accept-Encoding", []))
            )
            else None
        )
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Vary", "Accept-Encoding")
        if compressor is not None:
            self.send_header("Content-Encoding", "gzip")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; connect-src 'self'",
        )
        self.end_headers()
        revision = -1
        cursor = _FrameCursor()
        try:
            while True:
                payload = self.adapter._frame_packet(
                    revision, token=self.launch_token, cursor=cursor
                )
                # Metadata and services can change without a new scene revision.
                # _frame_packet already waits when there is nothing to deliver.
                body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                event = f"data: {body}\n\n".encode("utf-8")
                if compressor is not None:
                    event = compressor.compress(event) + compressor.flush(zlib.Z_SYNC_FLUSH)
                self._write_chunk(event)
                revision = payload["revision"]
                if payload["closed"]:
                    if compressor is not None:
                        self._write_chunk(compressor.flush(zlib.Z_FINISH))
                    self._write_chunk(b"")
                    if self.launch_token == self.adapter._token:
                        self.adapter._closed_delivered.set()
                    break
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
            OSError,
        ):
            self.close_connection = True

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/stream":
            self._stream(parsed)
            return
        if not self._allowed(parsed.path in {"/frame", "/export.svg", "/export.html"}):
            return
        adapter = self.adapter
        if parsed.path == "/":
            self._send(
                200,
                files("pysual").joinpath("web/live.html").read_bytes(),
                "text/html; charset=utf-8",
            )
        elif parsed.path in ("/live.js", "/svg.js", "/input.js", "/services.js"):
            self._send(
                200,
                files("pysual").joinpath("web" + parsed.path).read_bytes(),
                "text/javascript; charset=utf-8",
            )
        elif parsed.path in ("/font-sans.ttf", "/font-mono.ttf"):
                self._send(
                200, _font.font_bytes(parsed.path == "/font-mono.ttf"), "font/ttf"
            )
        elif parsed.path == "/export.svg":
            try:
                self._send(
                    200,
                    adapter.export_svg(_token=self.launch_token),
                    "image/svg+xml; charset=utf-8",
                )
            except CapabilityError:
                self._send(410, '{"error":"web session has ended"}')
        elif parsed.path == "/export.html":
            try:
                self._send(
                    200,
                    adapter.export_html(_token=self.launch_token),
                    "text/html; charset=utf-8",
                )
            except CapabilityError:
                self._send(410, '{"error":"web session has ended"}')
        elif parsed.path == "/frame":
            try:
                revision = int(parse_qs(parsed.query).get("revision", ["-1"])[0])
                if revision < -1:
                    raise ValueError("Invalid revision")
                payload = adapter._frame_packet(revision, token=self.launch_token)
                self._send(
                    200, json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                    compress=True,
                )
                if payload["closed"] and self.launch_token == adapter._token:
                    adapter._closed_delivered.set()
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
        else:
            self._send(404, '{"error":"Not found"}')

    def do_POST(self) -> None:
        if not self._allowed(True):
            return
        if self.path != "/events":
            self._send(404, '{"error":"Not found"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if not 0 <= length <= _MAX_REQUEST:
                self._send(413, '{"error":"Request too large"}')
                return
            if self.headers.get_content_type() != "application/json":
                self._send(415, '{"error":"Expected JSON"}')
                return
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError("Incomplete request")
            loaded: object = json.loads(data)
            accepted = self.adapter._receive(loaded, token=self.launch_token)
            self._send(200 if accepted else 410, "{}", events_ack=accepted)
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            self._send(400, json.dumps({"error": str(exc)[:200]}))
        except (TimeoutError, ConnectionError):
            self.close_connection = True


class LiveSVGHost(SVGRenderer, NativeServices):
    """Stdlib-only live SVG host; ``open_browser=False`` supports embedding/tests.

    ``url`` is available after :meth:`open`. Export methods serialize the last
    presented frame and embed fonts and images: exported files are independent
    snapshots, while interaction requires the running Python application.
    Sessions use the existing durable Python store, independent of localhost's
    ephemeral port. Clipboard, file picking/downloads and URLs use the browser.
    """

    _error_prefix = "web"
    web_execution = "live"
    event_driven = True

    capabilities = frozenset(
        {
            "clipboard",
            "images",
            "image_fit",
            "text_composition",
            "text_files",
            "render_surfaces",
            "scene_patches",
            "session_storage",
            "open_url",
            "shadow_masks",
        }
    )

    def __init__(
        self, *, open_browser: bool = True, host: str = "127.0.0.1", port: int = 0
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("LiveSVGHost only listens on 127.0.0.1")
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid web port")
        self._open_browser = open_browser
        self._port = port
        super().__init__()
        self._session_lock = asyncio.Lock()
        self._condition = threading.Condition()
        self._closed_delivered = threading.Event()
        self._events: deque[Input] = deque()
        self._commands: OrderedDict[int, dict[str, object]] = OrderedDict()
        self._pending: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Future[object]]] = {}
        self._operation = 0
        self._sent_metadata_revision = -1
        self._delivered_frames: OrderedDict[int, _FrameSnapshot] = OrderedDict()
        self._closed = True
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self._token = ""
        self._origin = self._authority = ""
        self._disconnect_at: float | None = None
        self._last_contact: float | None = None
        self.url: str | None = None
        self._wake: Callable[[], None] | None = None
        self._wake_loop: asyncio.AbstractEventLoop | None = None
        self._wake_token: object | None = None

    def set_wake_callback(self, callback: Callable[[], None] | None) -> None:
        with self._condition:
            self._wake = callback
            self._wake_loop = asyncio.get_running_loop() if callback is not None else None
            self._wake_token = None

    def _notify_input(self) -> None:
        # Called under _condition by HTTP workers as well as the UI owner.
        loop = self._wake_loop
        if self._closed or loop is None or self._wake is None or self._wake_token is not None:
            return
        token = self._wake_token = object()

        def deliver() -> None:
            with self._condition:
                if self._wake_token is not token:
                    return
                self._wake_token = None
                callback = self._wake
            if callback is not None:
                callback()

        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            self._wake_token = None

    @property
    def next_poll_delay(self) -> float:
        """Seconds until disconnect housekeeping; input wakes independently."""
        with self._condition:
            deadline = min(
                self._disconnect_at if self._disconnect_at is not None else inf,
                self._last_contact + 60 if self._last_contact is not None else inf,
            )
            return max(0.0, deadline - time.monotonic())

    def open(
        self,
        title: str,
        width: float,
        height: float,
        resizable: bool,
        scale: float | None,
    ) -> None:
        if not self._closed:
            raise RuntimeError("web host is already open")
        if scale not in (None, 1):
            raise CapabilityError(
                "web uses browser zoom/device scaling; set ui_scale=1"
            )
        with self._condition:
            self.reset_scene(title, width, height)
            self._token = secrets.token_urlsafe(32)
            self._closed = False
            self._closed_delivered.clear()
            self._events.clear()
            self._delivered_frames.clear()
            self._session_lock = asyncio.Lock()
            self._disconnect_at = None
            self._last_contact = None
        try:
            self._server = _Server(("127.0.0.1", self._port), self)
            self._authority = f"127.0.0.1:{self._server.server_port}"
            self._origin = f"http://{self._authority}"
            self.url = f"{self._origin}/#{self._token}"
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.05},
                name="pysual-web",
                daemon=True,
            )
            self._thread.start()
            if self._open_browser:
                print(f"Pysual web: {self.url}", file=sys.stderr)
                try:
                    opened = webbrowser.open(self.url, new=2)
                except (webbrowser.Error, OSError):
                    opened = False
                if not opened:
                    print(
                        "Open the URL above in your browser to connect.",
                        file=sys.stderr,
                    )
        except BaseException:
            self.close()
            raise

    def set_size(self, width: float, height: float) -> None:
        raise CapabilityError("The browser owns the web viewport size")

    def set_title(self, title: object) -> None:
        with self._condition:
            previous = self._metadata_revision
            super().set_title(title)
            if previous != self._metadata_revision:
                self._condition.notify_all()

    def poll(self) -> list[Input]:
        with self._condition:
            result = list(self._events)
            self._events.clear()
            if (
                self._disconnect_at is not None
                and time.monotonic() >= self._disconnect_at
            ):
                result.append(Input("close"))
                self._disconnect_at = None
            elif (
                self._last_contact is not None
                and time.monotonic() - self._last_contact >= 60
            ):
                result.append(Input("close"))
                self._last_contact = None
        for event in result:
            if event.viewport is not None:
                self._viewport = event.viewport
                self.size = (event.viewport.width, event.viewport.height)
        return result

    @staticmethod
    def _input(data: object) -> Input:
        if not _is_dict(data):
            raise ValueError("Unknown input event")
        kind = data.get("kind")
        if not isinstance(kind, str) or kind not in _INPUT_KINDS:
            raise ValueError("Unknown input event")
        text = data.get("text")
        if kind == "image_error" and (
            data.keys() != {"kind", "text"}
            or not isinstance(text, str)
            or not text.isascii()
            or not text.isdigit()
            or len(text) > 20
        ):
            raise ValueError("Invalid image resource identifier")
        if data.keys() - _INPUT_FIELDS:
            raise ValueError("Unknown input fields")
        values = dict(data)
        for field_name in ("x", "y", "delta"):
            if field_name in values:
                _number(values[field_name])
        for field_name in ("shift", "ctrl", "paste"):
            if field_name in values and type(values[field_name]) is not bool:
                raise ValueError("Invalid input modifiers")
        for field_name in ("button", "pointer_id"):
            if field_name in values and type(values[field_name]) is not int:
                raise ValueError("Invalid pointer data")
        for field_name in ("key", "text", "pointer_kind"):
            value = values.get(field_name, "")
            if (
                not isinstance(value, str)
                or len(value.encode("utf-8")) > MAX_TEXT_BYTES
            ):
                raise ValueError("Input text is too large")
        if values.get("viewport") is not None:
            viewport_fields = dict(cast(Mapping[object, object], values["viewport"]))
            safe_area = viewport_fields.get("safe_area", (0, 0, 0, 0))
            viewport_fields["safe_area"] = tuple(cast(Iterable[object], safe_area))
            viewport = Viewport(**cast(_ViewportData, viewport_fields))
            if viewport.width > 32768 or viewport.height > 32768 or viewport.scale > 16:
                raise ValueError("Viewport exceeds host limits")
            values["viewport"] = viewport
        if values["kind"] == "viewport" and values.get("viewport") is None:
            raise ValueError("Viewport event requires viewport data")
        return Input(**cast(_InputData, values))

    def _receive(self, data: object, *, token: str | None = None) -> bool:
        if not _is_dict(data):
            raise ValueError("Invalid event envelope")
        envelope = data
        if set(envelope) - {"events", "replies", "disconnect"}:
            raise ValueError("Invalid event envelope")
        raw_events = envelope.get("events", [])
        raw_replies = envelope.get("replies", [])
        events = _bounded_list(raw_events, 256)
        replies: list[dict[object, object]] = []
        for reply in _bounded_list(raw_replies, 32):
            if not _is_dict(reply):
                raise ValueError("Invalid service reply")
            replies.append(reply)
        parsed = [self._input(event) for event in events]
        for reply in replies:
            reply_error = reply.get("error", "")
            if (
                type(reply.get("id")) is not int
                or not isinstance(reply_error, str)
            ):
                raise ValueError("Invalid service reply")
        with self._condition:
            if self._closed or (token is not None and token != self._token):
                return False
            self._last_contact = time.monotonic()
            if envelope.get("disconnect"):
                # A reload can reconnect before this grace period; closing the
                # tab requests app closure even when no frame was being painted.
                self._disconnect_at = time.monotonic() + 2
            for event in parsed:
                if event.kind == "image_error":
                    self.report_image_error(int(event.text))
                    continue
                if (
                    self._events
                    and event.kind in ("pointer_move", "viewport")
                    and self._events[-1].kind == event.kind
                    and (
                        event.kind == "viewport"
                        or (
                            self._events[-1].pointer_id == event.pointer_id
                            and self._events[-1].pointer_kind == event.pointer_kind
                        )
                    )
                ):
                    self._events[-1] = event
                else:
                    self._events.append(event)
            if len(self._events) > 2048:
                # Coalesce first, then discard only expendable motion. Close,
                # viewport and cancellation events must survive congestion.
                self._events = deque(
                    event for event in self._events if event.kind != "pointer_move"
                )
            for reply in replies:
                operation = cast(int, reply["id"])
                pending = self._pending.pop(operation, None)
                self._commands.pop(operation, None)
                if pending is not None:
                    loop, future = pending

                    def finish(
                        future: asyncio.Future[object] = future,
                        reply: dict[object, object] = reply,
                    ) -> None:
                        if not future.done():
                            error = reply.get("error")
                            if error:
                                message = error if isinstance(error, str) else cast(str, error)
                                future.set_exception(CapabilityError(message[:1000]))
                            else:
                                future.set_result(reply.get("value"))

                    loop.call_soon_threadsafe(finish)
            self._condition.notify_all()
            self._notify_input()
            return True

    @staticmethod
    def _ended_packet() -> _TransportPacket:
        return {
            "revision": 0,
            "reset": True,
            "length": 0,
            "updates": [],
            "width": 1,
            "height": 1,
            "title": "Pysual",
            "text_input": None,
            "commands": [],
            "closed": True,
        }

    def _frame_packet(
        self,
        revision: int,
        *,
        token: str | None = None,
        cursor: _FrameCursor | None = None,
    ) -> _TransportPacket:
        with self._condition:
            if token is not None and token != self._token:
                return self._ended_packet()
            # Only a new attachment cancels pagehide's reload grace period.
            # An existing stream (or its last fetch) may still request a packet.
            if revision == -1:
                self._disconnect_at = None
            self._last_contact = time.monotonic()
            self._notify_input()
            metadata_revision = (
                cursor.metadata_revision if cursor is not None
                else self._sent_metadata_revision
            )
            self._condition.wait_for(
                lambda: revision != self._revision
                or self._metadata_revision != metadata_revision
                or self._closed
                or (token is not None and token != self._token),
                timeout=15,
            )
            if token is not None and token != self._token:
                return self._ended_packet()
            self._sent_metadata_revision = self._metadata_revision
            base = (
                cursor.snapshot if cursor is not None
                else self._delivered_frames.get(revision)
            )
            packet = self.frame_packet(revision, _base=base)
            snapshot = self._frame_snapshot()
            if cursor is not None:
                cursor.snapshot = snapshot
                cursor.metadata_revision = self._metadata_revision
            elif not self._closed:
                # Polls can move between HTTP connections. Retain recently sent
                # bases rather than every produced revision. This cache and the
                # connection limit bound retention independently of frame rate;
                # all snapshots share immutable nodes and image strings.
                self._delivered_frames[snapshot.revision] = snapshot
                self._delivered_frames.move_to_end(snapshot.revision)
                while len(self._delivered_frames) > _MAX_CONNECTIONS:
                    self._delivered_frames.popitem(last=False)
            return cast(
                _TransportPacket,
                {
                    **packet,
                    "commands": list(self._commands.values()),
                    "closed": self._closed,
                },
            )

    def _image_error(self, detail: object, source_key: int) -> None:
        with self._condition:
            super()._image_error(detail, source_key)

    def reload_image(self, source: object) -> None:
        with self._condition:
            super().reload_image(source)
            self._events.append(Input("repaint"))
            self._notify_input()

    def _resource_error(self, message: str) -> None:
        with self._condition:
            self._events.append(Input("resource_error", text=message))
            self._notify_input()

    def present(self) -> None:
        with self._condition:
            if self.publish_scene():
                self._condition.notify_all()

    def text_input(self, rect: Rect | None) -> None:
        with self._condition:
            previous = self._metadata_revision
            super().text_input(rect)
            if previous != self._metadata_revision:
                self._condition.notify_all()

    async def _rpc(self, method: str, **arguments: object) -> object:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._condition:
            if self._closed:
                raise CapabilityError("The web host is closed")
            if len(self._pending) >= 32:
                raise CapabilityError("Too many pending browser operations")
            self._operation += 1
            operation = self._operation
            self._pending[operation] = (loop, future)
            self._commands[operation] = {"id": operation, "method": method, **arguments}
            self._metadata_revision += 1
            self._condition.notify_all()
        try:
            return await asyncio.wait_for(future, 300)
        except asyncio.TimeoutError as exc:
            raise CapabilityError("Browser operation timed out") from exc
        finally:
            with self._condition:
                self._pending.pop(operation, None)
                self._commands.pop(operation, None)
                self._metadata_revision += 1
                self._condition.notify_all()

    async def clipboard_read(self) -> str:
        value = await self._rpc("clipboard_read")
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise CapabilityError(
                "Browser clipboard is not valid text of at most 8 MiB"
            )
        return value

    async def clipboard_write(self, text: str) -> None:
        try:
            valid = isinstance(text, str) and len(text.encode("utf-8")) <= MAX_TEXT_BYTES
        except UnicodeError:
            valid = False
        if not valid:
            raise CapabilityError("Clipboard text must be valid UTF-8 of at most 8 MiB")
        if not await self._rpc("clipboard_write", text=text):
            raise CapabilityError("Browser clipboard copy was cancelled")

    async def open_text_file(self) -> TextFile | None:
        value = await self._rpc("open_text_file")
        if value is None:
            return None
        if not _is_dict(value):
            raise CapabilityError("Invalid UTF-8 browser file result")
        name = value.get("name")
        text = value.get("text")
        if (
            not isinstance(name, str)
            or not isinstance(text, str)
            or len(text.encode("utf-8")) > MAX_TEXT_BYTES
        ):
            raise CapabilityError("Invalid UTF-8 browser file result")
        return TextFile(name, text)

    async def save_text_file(
        self, text: str, suggested_name: str, location: str | None = None
    ) -> TextFile | None:
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Text files must be at most 8 MiB")
        name = (
            str(suggested_name).replace("\\", "/").rsplit("/", 1)[-1] or "document.txt"
        )
        result = await self._rpc("save_text_file", text=text, name=name)
        return TextFile(result if isinstance(result, str) else name, text) if result else None

    async def open_url(self, url: str) -> None:
        if (
            not isinstance(url, str)
            or urlsplit(url).scheme.lower() not in {"https", "http", "mailto"}
            or any(ord(char) < 32 for char in url)
        ):
            raise ValueError("URLs must use http, https or mailto")
        if not await self._rpc("open_url", url=url):
            raise CapabilityError("Opening the URL was cancelled")


    def export_svg(
        self, path: str | Path | None = None, *, _token: str | None = None
    ) -> str:
        with self._condition:
            if _token is not None and _token != self._token:
                raise CapabilityError("web session has ended")
            return super().export_svg(path)

    def export_html(
        self, path: str | Path | None = None, *, _token: str | None = None
    ) -> str:
        with self._condition:
            if _token is not None and _token != self._token:
                raise CapabilityError("web session has ended")
            return super().export_html(path)

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._wake = self._wake_loop = self._wake_token = None
            for loop, future in self._pending.values():

                def finish(future: asyncio.Future[object] = future) -> None:
                    if not future.done():
                        future.set_exception(
                            CapabilityError("The web host is closed")
                        )

                if not loop.is_closed():
                    loop.call_soon_threadsafe(finish)
            self._pending.clear()
            self._commands.clear()
            self._delivered_frames.clear()
            self._condition.notify_all()
        server, self._server = self._server, None
        if server is not None:
            # The app can exit between a frame response and the next long poll.
            # Briefly retain the listener so that page receives an explicit
            # closed packet instead of reconnecting forever after normal exit.
            if self._last_contact is not None:
                self._closed_delivered.wait(0.35)
            if self._thread is not None and self._thread.is_alive():
                server.shutdown()
            server.server_close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
        self.close_scene()
