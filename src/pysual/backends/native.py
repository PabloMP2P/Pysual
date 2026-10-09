"""Native retained rendering, with Python work only for events and scene edits.

The public control/Painter contract is recorded when its model changes. The C
host owns presenting that retained scene, including finite style transitions.
Custom Python painters remain synchronous update-time hooks.
"""

import asyncio
import os
import threading
from collections import OrderedDict, deque
from dataclasses import fields
from pathlib import Path

from ..host import CapabilityError, Input, MAX_TEXT_BYTES, Viewport
from ._native import NativeServices


def _rect(rect):
    return None if rect is None else [rect.x, rect.y, rect.width, rect.height]


def _style(style):
    # Style validates immutable scalars, so only the wire mapping needs a copy.
    return {field.name: getattr(style, field.name) for field in fields(style)}


def _image_source(source):
    # The helper runs beside its executable, not in the application's cwd.
    # Keep URIs for the renderer to validate; only local paths need rebasing.
    if not isinstance(source, str) or not source or source.startswith("data:") or "://" in source:
        return source
    return os.path.abspath(source)


def _viewport(value, fallback):
    if not isinstance(value, dict):
        return Viewport(*fallback)
    return Viewport(
        value.get("width", fallback[0]), value.get("height", fallback[1]),
        value.get("scale", 1), tuple(value.get("safe_area", (0, 0, 0, 0))),
        value.get("keyboard_occlusion", 0),
    )


class NativeHost(NativeServices):
    """Event-driven bridge to the C SDL3 renderer; native calls stay in its process."""

    native_retained = True
    event_driven = True
    backend = "window"
    capabilities = frozenset({
        "clipboard", "host_resize", "images", "image_fit", "tinted_images",
        "text_composition", "session_storage", "open_url", "text_files", "native_animation",
    })

    def __init__(self, *, hidden=None, vsync=None, client_factory=None, executable=None, color="auto"):
        self.hidden = (os.environ.get("PYSUAL_HIDDEN") == "1"
                       if hidden is None else bool(hidden))
        self.vsync = (os.environ.get("PYSUAL_VSYNC", "1") != "0"
                      if vsync is None else bool(vsync))
        self._client_factory = client_factory
        self._executable = executable
        self._color = color
        self._client = None
        self._opened = False
        self._closed = False
        self._events = deque()
        self._event_lock = threading.Lock()
        self._wake = None
        self._wake_loop = None
        self._overflow = False
        self._commands = []
        self._scene_background = None
        self._scene_pending = None
        self._segment = None
        self._metrics = OrderedDict()
        self._metric_bytes = 0
        self._settings = {}
        self._transition_seconds = 0.0
        self._title = None
        self._text_input_rect = object()
        self._session_lock = asyncio.Lock()
        self.size = (960.0, 640.0)
        self._viewport = Viewport(*self.size)
        self.resource_revision = 0
        self._native_resource_revision = None
        self.info = {}

    @property
    def viewport(self):
        return self._viewport

    @property
    def render_scale(self):
        return self._viewport.scale

    @property
    def diagnostics(self):
        return {} if self._client is None else self._client.diagnostics

    def set_wake_callback(self, callback):
        """Bind once on the asyncio owner; the reader never touches UI state."""
        loop = asyncio.get_running_loop() if callback is not None else None
        with self._event_lock:
            self._wake = callback
            self._wake_loop = loop
            pending = bool(self._events)
        if pending and callback is not None:
            callback()

    def _on_event(self, value):
        with self._event_lock:
            if self._closed or self._overflow:
                return
            if len(self._events) >= 4096:
                self._events.clear()
                self._events.append({"kind": "error", "text": "Native input queue exceeded 4096 events"})
                self._overflow = True
            else:
                self._events.append(value)
            loop, callback = self._wake_loop, self._wake
        if loop is not None and callback is not None:
            try:
                loop.call_soon_threadsafe(callback)
            except RuntimeError:
                # A terminal callback can race the bounded owner shutdown.
                pass

    def _request(self, op, **kwargs):
        if self._client is None or self._closed:
            raise RuntimeError("Native host is not open")
        return self._client.request(op, **kwargs)

    def open(self, title, width, height, resizable, scale):
        if self._opened:
            raise RuntimeError("Native host is already open")
        if self._closed:
            raise RuntimeError("A closed host cannot reopen; create another host")
        if self.backend == "terminal" and scale not in (None, 1):
            raise CapabilityError("Terminal text uses its fixed cell grid; ui_scale must be 1")
        factory = self._client_factory
        if factory is None:
            from ._native_client import NativeClient
            factory = lambda **kwargs: NativeClient(executable=self._executable, **kwargs)
        self._client = factory(on_event=self._on_event)
        try:
            self.info = self._request(
                "open", backend=self.backend, title=title, width=width, height=height,
                resizable=resizable, scale=scale, hidden=self.hidden, vsync=self.vsync,
                continuous=self._settings.get("continuous", False),
                font_dir=str(Path(__file__).resolve().parents[1] / "assets"),
                color=self._color,
            )
            self._accept_viewport(self.info)
            if self.info.get("scene_patches", False):
                self.capabilities = self.capabilities | {"scene_patches"}
            if self.info.get("images") is False:
                self.capabilities = self.capabilities - {"images", "image_fit", "tinted_images"}
            self._title = title
            self._opened = True
            if self._settings:
                configured = self._request("configure", **self._settings)
                if isinstance(configured, dict):
                    self.info.update(configured)
                    self._accept_viewport(self.info)
        except BaseException:
            self.close()
            raise

    def _accept_viewport(self, info):
        previous = self._viewport
        previous_revision = self._native_resource_revision
        self._native_resource_revision = info.get("resource_revision", previous_revision)
        self._viewport = _viewport(info.get("viewport", info), self.size)
        self.size = (self._viewport.width, self._viewport.height)
        if (previous.scale != self._viewport.scale
                or previous_revision != self._native_resource_revision):
            self.resource_revision += 1
            self._metrics.clear()
            self._metric_bytes = 0

    def poll(self):
        with self._event_lock:
            values = tuple(self._events)
            self._events.clear()
        allowed = {field.name for field in fields(Input)}
        events = []
        for value in values:
            if not isinstance(value, dict) or "kind" not in value:
                raise RuntimeError("Native host returned an invalid input event")
            data = {key: value for key, value in value.items() if key in allowed}
            if data["kind"] in ("resize", "viewport"):
                self._accept_viewport(value)
                data["viewport"] = self._viewport
            elif isinstance(data.get("viewport"), dict):
                data["viewport"] = _viewport(data["viewport"], self.size)
            if data["kind"] == "error" and not data.get("text"):
                data["text"] = str(value.get("error", "Native host failed"))
            events.append(Input(**data))
        return events

    def configure_rendering(self, *, redraw_interval, fps_limit, reduce_motion, vsync=None):
        """Configure rendering; omitted VSync preserves the last requested mode.

        Native statistics report the renderer's actual VSync value. Rendering
        policies requested before opening are staged until the host opens.
        """
        if vsync is not None and self.backend != "window":
            raise CapabilityError("This native backend does not support VSync control")
        settings = dict(continuous=redraw_interval == 0, redraw_interval=redraw_interval,
                        fps_limit=fps_limit, reduce_motion=reduce_motion)
        if vsync is not None:
            settings["vsync"] = bool(vsync)
        elif "vsync" in self._settings:
            settings["vsync"] = self._settings["vsync"]
        if settings != self._settings:
            if self._opened:
                configured = self._request("configure", **settings)
                if isinstance(configured, dict):
                    self.info.update(configured)
                    self._accept_viewport(self.info)
            # A failed request must leave the accepted policy unchanged, so
            # the next identical call retries rather than silently skipping it.
            self._settings = settings
            if "vsync" in settings:
                self.vsync = settings["vsync"]
        if reduce_motion:
            self._transition_seconds = 0.0

    def request_transition(self, seconds):
        self._transition_seconds = max(self._transition_seconds, seconds)

    def set_title(self, title):
        if title != self._title:
            self._request("set_title", title=title)
            self._title = title

    def set_size(self, width, height):
        self._accept_viewport(self._request("set_size", width=width, height=height))

    def begin(self, background):
        self._scene_pending = None
        self._commands = [["begin", background]]

    def begin_scene(self, background):
        """Start a control patch batch; unchanged controls send no commands."""
        self._scene_pending = {"upsert": [], "remove": []}
        if background != self._scene_background:
            self._scene_pending["background"] = background
        self._commands = []
        self._segment = None

    def begin_segment(self, identity, bounds):
        if self._scene_pending is None or self._segment is not None:
            raise RuntimeError("Invalid retained segment recording state")
        self._commands = []
        self._segment = dict(id=identity, bounds=_rect(bounds), commands=self._commands)

    def end_segment(self):
        if self._segment is None:
            raise RuntimeError("No retained segment is being recorded")
        self._scene_pending["upsert"].append(self._segment)
        self._segment = None
        self._commands = []

    def end_scene(self, order=None, remove=()):
        if self._scene_pending is None or self._segment is not None:
            raise RuntimeError("Invalid retained scene recording state")
        if order is not None:
            self._scene_pending["order"] = list(order)
        self._scene_pending["remove"] = list(remove)
        self._present(scheduled=True)

    def clip(self, rect):
        self._commands.append(["clip", _rect(rect)])

    def rect(self, rect, fill, radius=0, border="", border_width=1):
        self._commands.append(["rect", _rect(rect), fill, radius, border, border_width])

    def text(self, text, x, y, color, size, mono=False):
        self._commands.append(["text", text, x, y, color, size, mono])

    def _store_metric(self, text, size, mono, result):
        if len(text) > 8192:
            return result
        key = (text, size, mono)
        cost = len(text.encode("utf-8")) + 128
        previous = self._metrics.pop(key, None)
        if previous is not None:
            self._metric_bytes -= previous[1]
        while self._metrics and (
            len(self._metrics) >= 16384 or self._metric_bytes + cost > 16 * 1024 * 1024
        ):
            _, (_, released) = self._metrics.popitem(last=False)
            self._metric_bytes -= released
        self._metrics[key] = (result, cost)
        self._metric_bytes += cost
        return result

    def measure(self, text, size, mono=False):
        key = (text, size, mono)
        if key in self._metrics:
            self._metrics.move_to_end(key)
            return self._metrics[key][0]
        result = tuple(self._request("measure", text=text, size=size, mono=mono))
        return self._store_metric(text, size, mono, result)

    def measure_many(self, texts, size, mono=False):
        """Measure several strings in one native round trip and fill the same cache."""
        if isinstance(texts, str):
            raise TypeError("measure_many expects a sequence of strings")
        texts = tuple(texts)
        if len(texts) > 4096:
            raise ValueError("measure_many accepts at most 4096 strings")
        results = [None] * len(texts)
        missing = {}
        for index, text in enumerate(texts):
            if not isinstance(text, str):
                raise TypeError("Measured text must be a string")
            key = (text, size, mono)
            cached = self._metrics.get(key)
            if cached is not None:
                self._metrics.move_to_end(key)
                results[index] = cached[0]
            else:
                missing.setdefault(text, []).append(index)
        if missing:
            measured = self._request(
                "measure_many", texts=list(missing), size=size, mono=mono
            )
            if not isinstance(measured, list) or len(measured) != len(missing):
                raise RuntimeError("Native measure_many returned an invalid batch")
            for (text, indices), value in zip(missing.items(), measured):
                result = self._store_metric(text, size, mono, tuple(value))
                for index in indices:
                    results[index] = result
        return results

    def line(self, x1, y1, x2, y2, color, width=1):
        self._commands.append(["line", x1, y1, x2, y2, color, width])

    def lines(self, points, color, width=1):
        self._commands.append(["lines", [list(point) for point in points], color, width])

    def segments(self, segments, color, width=1):
        points = [point for x1, y1, x2, y2 in segments for point in ([x1, y1], [x2, y2])]
        self._commands.append(["segments", points, color, width])

    def gradient_rect(self, rect, first, last, axis, radius, border_width=0):
        self._commands.append(["gradient_rect", _rect(rect), first, last, axis, radius, border_width])
        return True

    def styled_rect(self, rect, style, *, effect_clip=None):
        self._commands.append(["styled_rect", _rect(rect), _style(style), _rect(effect_clip)])
        return True

    def marker(self, rect, style, *, shape, checked, effect_clip=None):
        # Pixel hosts retain the precise shared catalog checkmark/dot drawing.
        if self.backend != "terminal":
            return False
        self._commands.append(["marker", _rect(rect), _style(style), shape, checked, _rect(effect_clip)])
        return True

    def icon(self, name, x, y, size, color):
        if self.backend != "terminal":
            return False
        from .term_text import _ICON_GLYPHS
        if name not in _ICON_GLYPHS:
            return False
        self._commands.append(["icon", name, x, y, size, color])
        return True

    def caret(self, x, y, height, color):
        self._commands.append(["caret", x, y, height, color])
        return True

    def focus_ring(self, rect, color, radius=0):
        self._commands.append(["focus_ring", _rect(rect), color, radius])
        return True

    def image(self, source, rect, *, tint="#ffffff", fit="stretch", _edges=None):
        if _edges is not None:
            return self.image_nine(source, rect, _edges, tint=tint)
        self._commands.append(["image", _image_source(source), _rect(rect), tint, fit])

    def image_nine(self, source, rect, edges, *, tint="#ffffff"):
        self._commands.append(["image_nine", _image_source(source), _rect(rect), list(edges), tint])

    def reload_image(self, source):
        info = self._request("reload_image", source=_image_source(source))
        self.info.update(info)
        self._accept_viewport(self.info)

    def sprite(self, source, rect, *, frame_width, frame_height, frame_count,
               fps=12, loop=True, tint="#ffffff"):
        """Declare a row-major sprite sheet; its clock and frame selection run in C."""
        if "native_animation" not in self.capabilities:
            raise CapabilityError("This host does not support native sprite animation")
        self._commands.append([
            "sprite", _image_source(source), _rect(rect), frame_width, frame_height, frame_count,
            fps, loop, tint,
        ])

    def animated_rect(self, rect, fill, *, radius=0, border="", border_width=1,
                      property="x", from_value, to_value, duration=1,
                      loop=True, yoyo=True):
        """Declare native position/opacity animation in absolute logical units."""
        if "native_animation" not in self.capabilities:
            raise CapabilityError("This host does not support native shape animation")
        self._commands.append([
            "animated_rect", _rect(rect), fill, radius, border, border_width,
            {"property": property, "from": from_value, "to": to_value,
             "duration": duration, "loop": loop, "yoyo": yoyo},
        ])

    def present(self):
        self._present(scheduled=False)

    def _present(self, *, scheduled):
        if self._scene_pending is None:
            self._request("frame", commands=self._commands, transition_seconds=self._transition_seconds)
            self._scene_background = None
        else:
            pending = self._scene_pending
            if (pending["upsert"] or pending["remove"]
                    or "order" in pending or "background" in pending):
                self._request("patch", **pending, transition_seconds=self._transition_seconds)
                self._scene_background = pending.get("background", self._scene_background)
            else:
                # Runtime repaints share the native cap; deliberate low-level
                # presents still complete immediately without a scene update.
                self._request("present", scheduled=scheduled)
            self._scene_pending = None
        self._transition_seconds = 0.0

    def text_input(self, rect):
        value = _rect(rect)
        if value != self._text_input_rect:
            self._request("text_input", rect=value)
            self._text_input_rect = value

    async def clipboard_read(self):
        try:
            result = await asyncio.to_thread(self._request, "clipboard_read")
            text = result.get("text", "") if isinstance(result, dict) else result
            if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ValueError("Clipboard text must be at most 8 MiB")
            return text
        except (RuntimeError, ValueError) as error:
            raise CapabilityError("Native clipboard read failed") from error

    async def clipboard_write(self, text):
        try:
            if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ValueError("Clipboard text must be at most 8 MiB")
            await asyncio.to_thread(self._request, "clipboard_write", text=text)
        except (RuntimeError, ValueError) as error:
            # Local JSON size/encoding rejection leaves the client healthy.
            # A transport timeout remains fatal: its commit status is unknown.
            raise CapabilityError("Native clipboard write failed") from error

    def stats(self, reset=False):
        return self._request("stats", reset=reset)

    def native_stats(self, reset=False):
        """Explicit native presentation/resource counters; never polled by Python."""
        return self.stats(reset=reset)

    def snapshot(self):
        return self._request("snapshot")

    def capture(self, path):
        return self._request("capture", path=str(Path(path).resolve()))

    def close(self):
        with self._event_lock:
            if self._closed:
                return
            self._closed = True
            self._wake = self._wake_loop = None
            self._events.clear()
        try:
            if self._client is not None:
                self._client.close()
        finally:
            self._opened = False
            self._commands.clear()
            self._scene_pending = self._segment = None
            self._metrics.clear()
            self._metric_bytes = 0


class NativeTermTextHost(NativeHost):
    backend = "terminal"
    renderer = "c"
    text_row_height = 16
    capabilities = NativeHost.capabilities - {
        "host_resize", "text_composition", "native_animation", "clipboard",
    }

    def text(self, text, x, y, color, size, mono=False):
        from ._term_cells import safe_text
        super().text(safe_text(text), x, y, color, size, mono)

    def measure(self, text, size, mono=False):
        from ._term_cells import safe_text
        return super().measure(safe_text(text), size, mono)

    def measure_many(self, texts, size, mono=False):
        from ._term_cells import safe_text
        return super().measure_many(tuple(safe_text(text) for text in texts), size, mono)
