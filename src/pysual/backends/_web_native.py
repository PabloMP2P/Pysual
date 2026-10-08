"""A local Python application hosted by a small SVG browser framebuffer.

Only normalized input and host-service results travel back from the page. Layout,
painting, controls and application state always remain in the Python process.
The HTTP listener is loopback-only and every stateful request needs a per-launch
capability token. No filesystem paths or executable application code are served.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from math import isfinite
import secrets
import socket
from socketserver import TCPServer
import sys
import threading
import time
from typing import cast
from urllib.parse import parse_qs, urlsplit
import webbrowser

from ..host import CapabilityError, Input, MAX_TEXT_BYTES, TextFile, Viewport
from . import _font
from ._native import NativeServices
from ._web_svg import SVGRenderer


# A legal UTF-8 result can expand sixfold as JSON (e.g. NUL -> \u0000).
# The browser sends at most one large result in each bounded envelope.
_MAX_REQUEST = MAX_TEXT_BYTES * 6 + 65536
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


def _number(value):
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError("Coordinates must be finite numbers")
    return value


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address, adapter):
        self.adapter = adapter
        self.token = adapter._token
        self.slots = threading.BoundedSemaphore(12)
        super().__init__(address, _Handler)

    def server_bind(self):
        # HTTPServer resolves the bound address with getfqdn(). This listener
        # is strictly numeric loopback; reverse DNS adds no information and
        # can block the shared UI owner while an OS resolver times out.
        TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]

    def process_request(self, request, client_address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            assert isinstance(request, socket.socket)
            request.settimeout(25)
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class _Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 lets the browser reuse the input connection. The frame stream is
    # one long response; /frame remains for exports and older clients.
    protocol_version = "HTTP/1.1"
    server_version = "PysualWebLive/1"

    @property
    def adapter(self):
        return cast(_Server, self.server).adapter

    @property
    def launch_token(self):
        return cast(_Server, self.server).token

    def log_message(self, format, *args):
        pass

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            # A tab can disconnect during headers or the next keep-alive read.
            self.close_connection = True

    def _send(
        self,
        status,
        body: bytes | str = b"",
        content_type="application/json; charset=utf-8",
    ):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src data:; font-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
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

    def _allowed(self, authenticated=False, query_token=None):
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
        self.wfile.write(f"{len(payload):X}\r\n".encode("ascii"))
        self.wfile.write(payload)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _stream(self, parsed):
        token = parse_qs(parsed.query).get("token", [""])[0]
        if not self._allowed(True, query_token=token):
            return
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; connect-src 'self'",
        )
        self.end_headers()
        revision = -1
        try:
            while True:
                payload = self.adapter._frame_packet(revision, token=self.launch_token)
                # Metadata and services can change without a new scene revision.
                # _frame_packet already waits when there is nothing to deliver.
                body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                self._write_chunk(f"data: {body}\n\n".encode("utf-8"))
                revision = payload["revision"]
                if payload["closed"]:
                    if self.launch_token == self.adapter._token:
                        self.adapter._closed_delivered.set()
                    self._write_chunk(b"")
                    break
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
            OSError,
        ):
            self.close_connection = True

    def do_GET(self):
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
                    200, json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                )
                if payload["closed"] and self.launch_token == adapter._token:
                    adapter._closed_delivered.set()
            except ValueError as exc:
                self._send(400, json.dumps({"error": str(exc)}))
        else:
            self._send(404, '{"error":"Not found"}')

    def do_POST(self):
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
            accepted = self.adapter._receive(json.loads(data), token=self.launch_token)
            self._send(200 if accepted else 410, "{}")
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

    def __init__(self, *, open_browser=True, host="127.0.0.1", port=0):
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
        self._events = deque()
        self._commands = OrderedDict()
        self._pending = {}
        self._operation = 0
        self._sent_metadata_revision = -1
        self._closed = True
        self._server = None
        self._thread = None
        self._token = ""
        self._origin = self._authority = ""
        self._disconnect_at = None
        self._last_contact = None
        self.url = None

    def open(self, title, width, height, resizable, scale):
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

    def set_size(self, width, height):
        raise CapabilityError("The browser owns the web viewport size")

    def set_title(self, title):
        with self._condition:
            previous = self._metadata_revision
            super().set_title(title)
            if previous != self._metadata_revision:
                self._condition.notify_all()

    def poll(self):
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
                and time.monotonic() - self._last_contact > 60
            ):
                result.append(Input("close"))
                self._last_contact = None
        for event in result:
            if event.viewport is not None:
                self._viewport = event.viewport
                self.size = (event.viewport.width, event.viewport.height)
        return result

    @staticmethod
    def _input(data):
        if not isinstance(data, dict) or data.get("kind") not in _INPUT_KINDS:
            raise ValueError("Unknown input event")
        if data["kind"] == "image_error" and (
            set(data) != {"kind", "text"}
            or not isinstance(data.get("text"), str)
            or not data["text"].isascii()
            or not data["text"].isdigit()
            or len(data["text"]) > 20
        ):
            raise ValueError("Invalid image resource identifier")
        values = dict(data)
        if set(values) - set(Input.__dataclass_fields__):
            raise ValueError("Unknown input fields")
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
            data = dict(values["viewport"])
            data["safe_area"] = tuple(data.get("safe_area", (0, 0, 0, 0)))
            viewport = Viewport(**data)
            if viewport.width > 32768 or viewport.height > 32768 or viewport.scale > 16:
                raise ValueError("Viewport exceeds host limits")
            values["viewport"] = viewport
        if values["kind"] == "viewport" and values.get("viewport") is None:
            raise ValueError("Viewport event requires viewport data")
        return Input(**values)

    def _receive(self, data, *, token: str | None = None):
        if not isinstance(data, dict) or set(data) - {
            "events",
            "replies",
            "disconnect",
        }:
            raise ValueError("Invalid event envelope")
        events, replies = data.get("events", []), data.get("replies", [])
        if (
            not isinstance(events, list)
            or not isinstance(replies, list)
            or len(events) > 256
            or len(replies) > 32
        ):
            raise ValueError("Too many events or replies")
        parsed = [self._input(event) for event in events]
        for reply in replies:
            if (
                not isinstance(reply, dict)
                or type(reply.get("id")) is not int
                or not isinstance(reply.get("error", ""), str)
            ):
                raise ValueError("Invalid service reply")
        with self._condition:
            if self._closed or (token is not None and token != self._token):
                return False
            self._last_contact = time.monotonic()
            if data.get("disconnect"):
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
                operation = reply["id"]
                pending = self._pending.pop(operation, None)
                self._commands.pop(operation, None)
                if pending is not None:
                    loop, future = pending

                    def finish(future=future, reply=reply):
                        if not future.done():
                            if reply.get("error"):
                                future.set_exception(
                                    CapabilityError(reply["error"][:1000])
                                )
                            else:
                                future.set_result(reply.get("value"))

                    loop.call_soon_threadsafe(finish)
            self._condition.notify_all()
            return True

    @staticmethod
    def _ended_packet():
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

    def _frame_packet(self, revision, *, token: str | None = None):
        with self._condition:
            if token is not None and token != self._token:
                return self._ended_packet()
            # Only a new attachment cancels pagehide's reload grace period.
            # An existing stream (or its last fetch) may still request a packet.
            if revision == -1:
                self._disconnect_at = None
            self._last_contact = time.monotonic()
            self._condition.wait_for(
                lambda: revision != self._revision
                or self._metadata_revision != self._sent_metadata_revision
                or self._closed
                or (token is not None and token != self._token),
                timeout=15,
            )
            if token is not None and token != self._token:
                return self._ended_packet()
            self._sent_metadata_revision = self._metadata_revision
            return {
                **self.frame_packet(revision),
                "commands": list(self._commands.values()),
                "closed": self._closed,
            }

    def _image_error(self, detail):
        with self._condition:
            super()._image_error(detail)

    def _resource_error(self, message):
        with self._condition:
            self._events.append(Input("resource_error", text=message))

    def present(self):
        with self._condition:
            if self.publish_scene():
                self._condition.notify_all()

    def text_input(self, rect):
        with self._condition:
            previous = self._metadata_revision
            super().text_input(rect)
            if previous != self._metadata_revision:
                self._condition.notify_all()

    async def _rpc(self, method, **arguments):
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

    async def clipboard_read(self):
        value = await self._rpc("clipboard_read")
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_TEXT_BYTES:
            raise CapabilityError(
                "Browser clipboard is not valid text of at most 8 MiB"
            )
        return value

    async def clipboard_write(self, text):
        try:
            valid = isinstance(text, str) and len(text.encode("utf-8")) <= MAX_TEXT_BYTES
        except UnicodeError:
            valid = False
        if not valid:
            raise CapabilityError("Clipboard text must be valid UTF-8 of at most 8 MiB")
        if not await self._rpc("clipboard_write", text=text):
            raise CapabilityError("Browser clipboard copy was cancelled")

    async def open_text_file(self):
        value = await self._rpc("open_text_file")
        if value is None:
            return None
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("name"), str)
            or not isinstance(value.get("text"), str)
            or len(value["text"].encode("utf-8")) > MAX_TEXT_BYTES
        ):
            raise CapabilityError("Invalid UTF-8 browser file result")
        return TextFile(value["name"], value["text"])

    async def save_text_file(self, text, suggested_name, location=None):
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Text files must be at most 8 MiB")
        name = (
            str(suggested_name).replace("\\", "/").rsplit("/", 1)[-1] or "document.txt"
        )
        result = await self._rpc("save_text_file", text=text, name=name)
        return TextFile(result if isinstance(result, str) else name, text) if result else None

    async def open_url(self, url):
        if (
            not isinstance(url, str)
            or urlsplit(url).scheme.lower() not in {"https", "http", "mailto"}
            or any(ord(char) < 32 for char in url)
        ):
            raise ValueError("URLs must use http, https or mailto")
        if not await self._rpc("open_url", url=url):
            raise CapabilityError("Opening the URL was cancelled")


    def export_svg(self, path=None, *, _token: str | None = None):
        with self._condition:
            if _token is not None and _token != self._token:
                raise CapabilityError("web session has ended")
            return super().export_svg(path)

    def export_html(self, path=None, *, _token: str | None = None):
        with self._condition:
            if _token is not None and _token != self._token:
                raise CapabilityError("web session has ended")
            return super().export_html(path)

    def close(self):
        with self._condition:
            self._closed = True
            for loop, future in self._pending.values():

                def finish(future=future):
                    if not future.done():
                        future.set_exception(
                            CapabilityError("The web host is closed")
                        )

                if not loop.is_closed():
                    loop.call_soon_threadsafe(finish)
            self._pending.clear()
            self._commands.clear()
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
