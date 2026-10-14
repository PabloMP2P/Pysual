"""Native retained rendering, with Python work only for events and scene edits.

The public control/Painter contract is recorded when its model changes. The C
host owns presenting that retained scene, including finite style transitions.
Custom Python painters remain synchronous update-time hooks.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any, Literal, NotRequired, Protocol, TypedDict, cast

from ..geometry import Rect
from ..host import CapabilityError, Input, MAX_TEXT_BYTES, Viewport
from ..theme import ResolvedStyle, Style
from . import _native_client
from ._native_client import display_text
from ._native import NativeServices


class _NativeBridge(Protocol):
    @property
    def diagnostics(self) -> Mapping[str, int | str | bool | None]: ...

    def request(self, op: str, **kwargs: Any) -> Any: ...

    def close(self) -> None: ...


class _Segment(TypedDict):
    id: str
    bounds: list[float] | None
    commands: list[object]


class _ScenePending(TypedDict):
    upsert: list[_Segment]
    remove: list[str]
    background: NotRequired[str]
    order: NotRequired[list[str]]


_Metric = tuple[float, float]
_MetricKey = tuple[str, float, bool]
_Setting = bool | int | float | None


def _display(value: str) -> str:
    return display_text(value)


def _rect(rect: Rect | None) -> list[float] | None:
    return None if rect is None else [rect.x, rect.y, rect.width, rect.height]


def _style(style: Style | ResolvedStyle) -> dict[str, object]:
    # Style validates immutable scalars, so only the wire mapping needs a copy.
    # Narrow once. Casting every field would run on each recorded face.
    if not isinstance(style, Style):
        raise TypeError("Resolved style must be a Style")
    return {field.name: getattr(style, field.name) for field in fields(style)}


def _image_source(source: str) -> str:
    # The helper runs beside its executable, not in the application's cwd.
    # Keep URIs for the renderer to validate; only local paths need rebasing.
    if not isinstance(source, str) or not source or source.startswith("data:") or "://" in source:
        return source
    return os.path.abspath(source)


def _viewport(value: object, fallback: tuple[float, float]) -> Viewport:
    if not isinstance(value, dict):
        return Viewport(*fallback)
    raw = cast(dict[str, object], value)
    safe_area = raw.get("safe_area", (0, 0, 0, 0))
    return Viewport(
        cast(float, raw.get("width", fallback[0])),
        cast(float, raw.get("height", fallback[1])),
        cast(float, raw.get("scale", 1)),
        cast(
            tuple[float, float, float, float],
            tuple(cast(Iterable[object], safe_area)),
        ),
        cast(float, raw.get("keyboard_occlusion", 0)),
    )


class NativeHost(NativeServices):
    """Event-driven bridge to the C SDL3 renderer; native calls stay in its process."""

    native_retained: bool = True
    event_driven: bool = True
    backend: str = "window"
    capabilities: frozenset[str] = frozenset({
        "clipboard", "host_resize", "images", "image_fit", "tinted_images",
        "text_composition", "session_storage", "open_url", "text_files", "native_animation",
    })

    def __init__(
        self,
        *,
        hidden: bool | None = None,
        vsync: bool | None = None,
        client_factory: Callable[..., _NativeBridge] | None = None,
        executable: str | Path | None = None,
        color: str = "auto",
    ) -> None:
        self.hidden = (os.environ.get("PYSUAL_HIDDEN") == "1"
                       if hidden is None else bool(hidden))
        self.vsync = (os.environ.get("PYSUAL_VSYNC", "1") != "0"
                      if vsync is None else bool(vsync))
        self._client_factory = client_factory
        self._executable = executable
        self._color = color
        self._client: _NativeBridge | None = None
        self._opened = False
        self._closed = False
        self._events: deque[object] = deque()
        self._event_lock = threading.Lock()
        self._wake: Callable[[], None] | None = None
        self._wake_token: object | None = None
        self._overflow = False
        self._commands: list[object] = []
        self._scene_background: str | None = None
        self._scene_pending: _ScenePending | None = None
        self._segment: _Segment | None = None
        self._metrics: OrderedDict[_MetricKey, tuple[_Metric, int]] = OrderedDict()
        self._metric_bytes = 0
        self._settings: dict[str, _Setting] = {}
        self._transition_seconds = 0.0
        self._title: str | None = None
        self._text_input_rect: object = object()
        self._session_lock = asyncio.Lock()
        self.size = (960.0, 640.0)
        self._viewport = Viewport(*self.size)
        self.resource_revision = 0
        self._native_resource_revision: object | None = None
        self.info: dict[str, object] = {}
        self._wake_loop: asyncio.AbstractEventLoop | None = None

    @property
    def viewport(self) -> Viewport:
        return self._viewport

    @property
    def render_scale(self) -> float:
        return self._viewport.scale

    @property
    def diagnostics(self) -> dict[str, int | str | bool | None]:
        client = self._client
        if client is None:
            return {}
        return cast(dict[str, int | str | bool | None], client.diagnostics)

    def set_wake_callback(self, callback: Callable[[], None] | None) -> None:
        """Bind on the asyncio owner; invalidate any previous owner's wake."""
        loop = asyncio.get_running_loop() if callback is not None else None
        with self._event_lock:
            self._wake = callback
            self._wake_loop = loop
            token = object() if self._events and callback is not None else None
            self._wake_token = token
        if token is not None:
            self._deliver_wake(token)

    def _deliver_wake(self, token: object) -> None:
        with self._event_lock:
            if token is not self._wake_token or self._closed:
                return
            callback = self._wake
        if callback is not None:
            callback()

    def _on_event(self, value: object) -> None:
        with self._event_lock:
            if self._closed or self._overflow:
                return
            if len(self._events) >= 4096:
                self._events.clear()
                self._events.append({"kind": "error", "text": "Native input queue exceeded 4096 events"})
                self._overflow = True
            else:
                self._events.append(value)
            loop = self._wake_loop
            # Keep one wake outstanding until poll consumes the whole batch.
            # The lock also covers poll, so a racing new event always either
            # joins that batch or schedules the next one.
            token = None
            if self._wake_token is None and loop is not None and self._wake is not None:
                token = self._wake_token = object()
        if token is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(self._deliver_wake, token)
            except RuntimeError:
                # A terminal callback can race the bounded owner shutdown.
                with self._event_lock:
                    if token is self._wake_token:
                        self._wake_token = None

    def _request(self, op: str, **kwargs: Any) -> Any:
        if self._client is None or self._closed:
            raise RuntimeError("Native host is not open")
        return self._client.request(op, **kwargs)

    def open(
        self,
        title: str,
        width: float,
        height: float,
        resizable: bool,
        scale: float | None,
    ) -> None:
        if self._opened:
            raise RuntimeError("Native host is already open")
        if self._closed:
            raise RuntimeError("A closed host cannot reopen; create another host")
        if self.backend == "terminal" and scale not in (None, 1):
            raise CapabilityError("Terminal text uses its fixed cell grid; ui_scale must be 1")
        factory = self._client_factory
        if factory is None:
            executable = self._executable

            def fallback(**kwargs: Any) -> _NativeBridge:
                return cast(
                    _NativeBridge,
                    _native_client.NativeClient(executable=executable, **kwargs),
                )

            factory = fallback
        self._client = factory(on_event=self._on_event)
        title = _display(title)
        try:
            self.info = cast(dict[str, object], self._request(
                "open", backend=self.backend, title=title, width=width, height=height,
                resizable=resizable, scale=scale, hidden=self.hidden, vsync=self.vsync,
                continuous=self._settings.get("continuous", False),
                font_dir=str(Path(__file__).resolve().parents[1] / "assets"),
                color=self._color,
            ))
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
                    self.info.update(cast(dict[str, object], configured))
                    self._accept_viewport(self.info)
        except BaseException:
            self.close()
            raise

    def _accept_viewport(self, info: Mapping[str, object]) -> None:
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

    def poll(self) -> list[Input]:
        with self._event_lock:
            values = tuple(self._events)
            self._events.clear()
            self._wake_token = None
        allowed = {field.name for field in fields(Input)}
        events: list[Input] = []
        for value in values:
            if not isinstance(value, dict):
                raise RuntimeError("Native host returned an invalid input event")
            raw = cast(dict[str, object], value)
            if "kind" not in raw:
                raise RuntimeError("Native host returned an invalid input event")
            data: dict[str, object] = {key: item for key, item in raw.items() if key in allowed}
            if data["kind"] in ("resize", "viewport"):
                self._accept_viewport(raw)
                data["viewport"] = self._viewport
            elif isinstance(data.get("viewport"), dict):
                data["viewport"] = _viewport(data["viewport"], self.size)
            if data["kind"] == "error" and not data.get("text"):
                data["text"] = str(raw.get("error", "Native host failed"))
            events.append(Input(**cast(Any, data)))
        return events

    def configure_rendering(
        self,
        *,
        redraw_interval: float | None,
        fps_limit: int | float | None,
        reduce_motion: bool,
        vsync: bool | None = None,
    ) -> None:
        """Configure rendering; omitted VSync preserves the last requested mode.

        Native statistics report the renderer's actual VSync value. Rendering
        policies requested before opening are staged until the host opens.
        """
        if vsync is not None and self.backend != "window":
            raise CapabilityError("This native backend does not support VSync control")
        settings: dict[str, _Setting] = dict(
            continuous=redraw_interval == 0, redraw_interval=redraw_interval,
            fps_limit=fps_limit, reduce_motion=reduce_motion,
        )
        if vsync is not None:
            settings["vsync"] = bool(vsync)
        elif "vsync" in self._settings:
            settings["vsync"] = self._settings["vsync"]
        if settings != self._settings:
            if self._opened:
                configured = self._request("configure", **settings)
                if isinstance(configured, dict):
                    self.info.update(cast(dict[str, object], configured))
                    self._accept_viewport(self.info)
            # A failed request must leave the accepted policy unchanged, so
            # the next identical call retries rather than silently skipping it.
            self._settings = settings
            if "vsync" in settings:
                self.vsync = cast(bool, settings["vsync"])
        if reduce_motion:
            self._transition_seconds = 0.0

    def request_transition(self, seconds: float) -> None:
        self._transition_seconds = max(self._transition_seconds, seconds)

    def set_title(self, title: str) -> None:
        title = _display(title)
        if title != self._title:
            self._request("set_title", title=title)
            self._title = title

    def set_size(self, width: float, height: float) -> None:
        self._accept_viewport(cast(
            Mapping[str, object], self._request("set_size", width=width, height=height)
        ))

    def begin(self, background: str) -> None:
        self._scene_pending = None
        self._commands = []
        self._commands.append(["begin", background])

    def begin_scene(self, background: str) -> None:
        """Start a control patch batch; unchanged controls send no commands."""
        self._scene_pending = {"upsert": [], "remove": []}
        if background != self._scene_background:
            self._scene_pending["background"] = background
        self._commands = []
        self._segment = None

    def begin_segment(self, identity: str, bounds: Rect) -> None:
        if self._scene_pending is None or self._segment is not None:
            raise RuntimeError("Invalid retained segment recording state")
        commands: list[object] = []
        self._commands = commands
        self._segment = cast(_Segment, dict(
            id=identity, bounds=_rect(bounds), commands=commands
        ))

    def end_segment(self) -> None:
        pending = self._scene_pending
        segment = self._segment
        if pending is None or segment is None:
            raise RuntimeError("No retained segment is being recorded")
        pending["upsert"].append(segment)
        self._segment = None
        self._commands = []

    def end_scene(
        self, order: Sequence[str] | None = None, remove: Iterable[str] = ()
    ) -> None:
        pending = self._scene_pending
        if pending is None or self._segment is not None:
            raise RuntimeError("Invalid retained scene recording state")
        if order is not None:
            pending["order"] = list(order)
        pending["remove"] = list(remove)
        self._present(scheduled=True)

    def clip(self, rect: Rect | None) -> None:
        self._commands.append(["clip", _rect(rect)])

    def rect(
        self,
        rect: Rect,
        fill: str,
        radius: float = 0,
        border: str = "",
        border_width: float = 1,
    ) -> None:
        self._commands.append(["rect", _rect(rect), fill, radius, border, border_width])

    def text(
        self, text: str, x: float, y: float, color: str, size: float, mono: bool = False
    ) -> None:
        self._commands.append(["text", _display(text), x, y, color, size, mono])

    def _store_metric(
        self, text: str, size: float, mono: bool, result: _Metric
    ) -> _Metric:
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

    def measure(self, text: str, size: float, mono: bool = False) -> _Metric:
        text = _display(text)
        key = (text, size, mono)
        if key in self._metrics:
            self._metrics.move_to_end(key)
            return self._metrics[key][0]
        result = cast(_Metric, tuple(self._request("measure", text=text, size=size, mono=mono)))
        return self._store_metric(text, size, mono, result)

    def measure_many(
        self, texts: Iterable[str], size: float, mono: bool = False
    ) -> list[_Metric]:
        """Measure several strings in one native round trip and fill the same cache."""
        if isinstance(texts, str):
            raise TypeError("measure_many expects a sequence of strings")
        shown = tuple(_display(text) for text in texts)
        if len(shown) > 4096:
            raise ValueError("measure_many accepts at most 4096 strings")
        results: list[_Metric | None] = [None] * len(shown)
        missing: dict[str, list[int]] = {}
        for index, text in enumerate(shown):
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
            batch = cast(list[object], measured) if isinstance(measured, list) else None
            if batch is None or len(batch) != len(missing):
                raise RuntimeError("Native measure_many returned an invalid batch")
            for (text, indices), value in zip(missing.items(), batch):
                result = self._store_metric(
                    text, size, mono, cast(_Metric, tuple(cast(Iterable[object], value)))
                )
                for index in indices:
                    results[index] = result
        return cast(list[_Metric], results)

    def line(
        self, x1: float, y1: float, x2: float, y2: float, color: str, width: float = 1
    ) -> None:
        self._commands.append(["line", x1, y1, x2, y2, color, width])

    def lines(
        self, points: Sequence[tuple[float, float]], color: str, width: float = 1
    ) -> None:
        self._commands.append(["lines", [list(point) for point in points], color, width])

    def segments(
        self,
        segments: Iterable[tuple[float, float, float, float]],
        color: str,
        width: float = 1,
    ) -> None:
        points = [point for x1, y1, x2, y2 in segments for point in ([x1, y1], [x2, y2])]
        self._commands.append(["segments", points, color, width])

    def gradient_rect(
        self,
        rect: Rect,
        first: str,
        last: str,
        axis: Literal["vertical", "horizontal"],
        radius: float,
        border_width: float = 0,
    ) -> bool:
        self._commands.append(["gradient_rect", _rect(rect), first, last, axis, radius, border_width])
        return True

    def styled_rect(
        self, rect: Rect, style: Style | ResolvedStyle, *, effect_clip: Rect | None = None
    ) -> bool:
        self._commands.append(["styled_rect", _rect(rect), _style(style), _rect(effect_clip)])
        return True

    def marker(
        self,
        rect: Rect,
        style: Style | ResolvedStyle,
        *,
        shape: Literal["square", "circle"],
        checked: bool,
        effect_clip: Rect | None = None,
    ) -> bool:
        # Pixel hosts retain the precise shared catalog checkmark/dot drawing.
        if self.backend != "terminal":
            return False
        self._commands.append(["marker", _rect(rect), _style(style), shape, checked, _rect(effect_clip)])
        return True

    def icon(self, name: str, x: float, y: float, size: float, color: str) -> bool:
        if self.backend != "terminal":
            return False
        from .term_text import _ICON_GLYPHS
        if name not in _ICON_GLYPHS:
            return False
        self._commands.append(["icon", name, x, y, size, color])
        return True

    def caret(self, x: float, y: float, height: float, color: str) -> bool:
        self._commands.append(["caret", x, y, height, color])
        return True

    def focus_ring(self, rect: Rect, color: str, radius: float = 0) -> bool:
        self._commands.append(["focus_ring", _rect(rect), color, radius])
        return True

    def image(
        self,
        source: str,
        rect: Rect,
        *,
        tint: str = "#ffffff",
        fit: Literal["stretch", "contain", "cover"] = "stretch",
        _edges: Sequence[int] | None = None,
    ) -> None:
        if _edges is not None:
            self.image_nine(source, rect, _edges, tint=tint)
            return
        self._commands.append(["image", _image_source(source), _rect(rect), tint, fit])

    def image_nine(
        self, source: str, rect: Rect, edges: Sequence[int], *, tint: str = "#ffffff"
    ) -> None:
        self._commands.append(["image_nine", _image_source(source), _rect(rect), list(edges), tint])

    def reload_image(self, source: str) -> None:
        info = self._request("reload_image", source=_image_source(source))
        self.info.update(cast(Mapping[str, object], info))
        self._accept_viewport(self.info)

    def sprite(
        self,
        source: str,
        rect: Rect,
        *,
        frame_width: float,
        frame_height: float,
        frame_count: int,
        fps: float = 12,
        loop: bool = True,
        tint: str = "#ffffff",
    ) -> None:
        """Declare a row-major sprite sheet; its clock and frame selection run in C."""
        if "native_animation" not in self.capabilities:
            raise CapabilityError("This host does not support native sprite animation")
        self._commands.append([
            "sprite", _image_source(source), _rect(rect), frame_width, frame_height, frame_count,
            fps, loop, tint,
        ])

    def animated_rect(
        self,
        rect: Rect,
        fill: str,
        *,
        radius: float = 0,
        border: str = "",
        border_width: float = 1,
        property: Literal["x", "y", "opacity"] = "x",
        from_value: float,
        to_value: float,
        duration: float = 1,
        loop: bool = True,
        yoyo: bool = True,
    ) -> None:
        """Declare native position/opacity animation in absolute logical units."""
        if "native_animation" not in self.capabilities:
            raise CapabilityError("This host does not support native shape animation")
        self._commands.append([
            "animated_rect", _rect(rect), fill, radius, border, border_width,
            {"property": property, "from": from_value, "to": to_value,
             "duration": duration, "loop": loop, "yoyo": yoyo},
        ])

    def present(self) -> None:
        self._present(scheduled=False)

    def _present(self, *, scheduled: bool) -> None:
        pending = self._scene_pending
        if pending is None:
            self._request("frame", commands=self._commands, transition_seconds=self._transition_seconds)
            self._scene_background = None
        else:
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

    def text_input(self, rect: Rect | None) -> None:
        value = _rect(rect)
        if value != self._text_input_rect:
            self._request("text_input", rect=value)
            self._text_input_rect = value

    async def clipboard_read(self) -> str:
        try:
            result = await asyncio.to_thread(self._request, "clipboard_read")
            payload: object = result
            if isinstance(payload, dict):
                payload = cast(dict[str, object], payload).get("text", "")
            if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ValueError("Clipboard text must be at most 8 MiB")
            return payload
        except (RuntimeError, ValueError) as error:
            raise CapabilityError("Native clipboard read failed") from error

    async def clipboard_write(self, text: str) -> None:
        try:
            if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
                raise ValueError("Clipboard text must be at most 8 MiB")
            await asyncio.to_thread(self._request, "clipboard_write", text=text)
        except (RuntimeError, ValueError) as error:
            # Local JSON size/encoding rejection leaves the client healthy.
            # A transport timeout remains fatal: its commit status is unknown.
            raise CapabilityError("Native clipboard write failed") from error

    def stats(self, reset: bool = False) -> Any:
        return self._request("stats", reset=reset)

    def native_stats(self, reset: bool = False) -> Any:
        """Explicit native presentation/resource counters; never polled by Python."""
        return self.stats(reset=reset)

    def snapshot(self) -> Any:
        return self._request("snapshot")

    def capture(self, path: str | Path) -> Any:
        return self._request("capture", path=str(Path(path).resolve()))

    def close(self) -> None:
        with self._event_lock:
            if self._closed:
                return
            self._closed = True
            self._wake = self._wake_loop = None
            self._wake_token = None
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

    def text(
        self, text: str, x: float, y: float, color: str, size: float, mono: bool = False
    ) -> None:
        from ._term_cells import safe_text
        super().text(safe_text(text), x, y, color, size, mono)

    def measure(self, text: str, size: float, mono: bool = False) -> _Metric:
        from ._term_cells import safe_text
        return super().measure(safe_text(text), size, mono)

    def measure_many(
        self, texts: Iterable[str], size: float, mono: bool = False
    ) -> list[_Metric]:
        from ._term_cells import safe_text
        return super().measure_many(tuple(safe_text(text) for text in texts), size, mono)
