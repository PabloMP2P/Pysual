"""Shared SVG renderer delivered locally through a Pyodide browser bridge."""

import asyncio
import base64
import json
import mimetypes
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, cast
from urllib.parse import unquote

from ._web_svg import SVGRenderer, _FramePacket

from ..geometry import Rect
from ..host import (
    MAX_TEXT_BYTES,
    CapabilityError,
    Input,
    TextFile,
    Viewport,
    validate_session_key,
    validate_session_text,
)


class _PyodideBridge(Protocol):
    """Browser object installed by the Pyodide host script."""

    width: float
    height: float
    viewport: object
    renderScale: float
    resourceRevision: int

    def open(self, title: str, width: float, height: float, scale: float) -> None: ...
    def setSessionNamespace(self, namespace: str) -> None: ...
    def title(self, text: str) -> None: ...
    def poll(self) -> str: ...
    def present(self, packet: str) -> None: ...
    def close(self) -> None: ...
    def textInput(self, x: float, y: float, width: float, height: float) -> None: ...
    def readClipboard(self, operation: int) -> Awaitable[object]: ...
    def writeClipboard(self, text: str, operation: int) -> Awaitable[object]: ...
    def cancelOperation(self, operation: int) -> None: ...
    def cancelSession(self, operation: int) -> None: ...
    def readSessionJSON(self, key: str, operation: int) -> Awaitable[object]: ...
    def writeSessionJSON(self, key: str, text: str, operation: int) -> Awaitable[object]: ...
    def openUrl(self, url: str, operation: int) -> Awaitable[object]: ...
    def openTextFile(self, limit: int, operation: int) -> Awaitable[object]: ...
    def saveTextFile(self, text: str, name: str, operation: int) -> Awaitable[object]: ...


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


class _TextFileData(TypedDict):
    name: str
    text: str
    location: NotRequired[str | None]


def _as_tuple(value: object) -> tuple[object, ...]:
    return tuple(item for item in cast(Iterable[object], value))


def _get(mapping: object, key: str, default: object) -> object:
    getter: Callable[[str, object], object] = getattr(mapping, "get")
    return getter(key, default)


def _set(mapping: object, key: str, value: object) -> None:
    setter: Callable[[str, object], None] = getattr(mapping, "__setitem__")
    setter(key, value)


def _viewport_from_json(data: object) -> Viewport:
    _set(data, "safe_area", _as_tuple(_get(data, "safe_area", (0, 0, 0, 0))))
    return Viewport(**cast(_ViewportData, data))


def _json_object(text: str) -> object:
    loaded: object = json.loads(text)
    return loaded


def _loads(value: object) -> object:
    if isinstance(value, (str, bytes, bytearray)):
        return json.loads(value)
    return json.loads(cast(str, value))


class BundledSVGHost(SVGRenderer):
    web_execution = "bundled"
    capabilities = frozenset(
        {
            "clipboard",
            "images",
            "image_fit",
            "shadow_masks",
            "text_composition",
            "text_files",
            "render_surfaces",
            "scene_patches",
            "session_storage",
            "open_url",
        }
    )

    def __init__(self) -> None:
        from js import pysualHost  # type: ignore[import-not-found]

        super().__init__()
        self.bridge: _PyodideBridge = pysualHost
        self._session_serial = 0
        self._sent_revision = -1
        self._resource_events: list[Input] = []
        self._opened = False
        self._session_namespace = "default"

    def open(
        self,
        title: str,
        width: float,
        height: float,
        resizable: bool,
        scale: float | None,
    ) -> None:
        if self._opened:
            raise RuntimeError("The browser host is already open")
        if scale not in (None, 1):
            raise CapabilityError("The web backend uses page/device scaling; set ui_scale=1")
        self.bridge.open(title, width, height, 1)
        # Acquiring the shared DOM surface makes this instance responsible for
        # cleanup, including failures in the remaining Python initialization.
        self._opened = True
        self.bridge.setSessionNamespace(self._session_namespace)
        self.reset_scene(title, float(self.bridge.width), float(self.bridge.height))
        self._sent_revision = -1
        self._resource_events.clear()

    def set_size(self, width: float, height: float) -> None:
        raise CapabilityError("The browser owns the app viewport size")

    def set_title(self, title: object) -> None:
        title = str(title)
        if title == self._title:
            return
        super().set_title(title)
        self.bridge.title(title)

    def set_session_namespace(self, namespace: str) -> None:
        self._session_namespace = namespace
        if self._opened:
            self.bridge.setSessionNamespace(namespace)

    @property
    def viewport(self) -> Viewport:
        if hasattr(self.bridge, "viewport"):
            loaded = _json_object(str(self.bridge.viewport))
            return _viewport_from_json(loaded)
        return Viewport(*self.size, self.render_scale)

    def poll(self) -> list[Input]:
        loaded = _json_object(self.bridge.poll())
        result: list[Input] = []
        for event in cast(Iterable[object], loaded):
            record = cast(dict[str, object], event)
            if record.get("kind") == "image_error":
                try:
                    identifier = int(cast(str, record.get("text", "")))
                except (TypeError, ValueError):
                    continue
                self.report_image_error(identifier)
                continue
            if record.get("viewport") is not None:
                record["viewport"] = _viewport_from_json(record["viewport"])
            result.append(Input(**cast(_InputData, record)))
        result.extend(self._resource_events)
        self._resource_events.clear()
        self.size = (float(self.bridge.width), float(self.bridge.height))
        return result

    def lines(
        self, points: Sequence[tuple[float, float]], color: str, width: float = 1
    ) -> None:
        previous: tuple[float, float] | None = None
        for point in points:
            if previous is not None:
                self.line(previous[0], previous[1], point[0], point[1], color, width)
            previous = point

    def _resolve_image_source(self, source: str) -> str:
        # Included/package assets exist in Pyodide's virtual filesystem. Other
        # relative paths and absolute URLs retain ordinary browser resolution.
        if source.startswith(("http://", "https://", "blob:")):
            return source
        if source.startswith("data:image/"):
            if source.startswith(tuple(
                f"data:image/{kind};base64,"
                for kind in ("png", "jpeg", "gif", "webp", "svg+xml")
            )):
                return super()._resolve_image_source(source)
            if len(source) > 44 * 1024 * 1024:
                raise ValueError("Encoded browser image exceeds 44 MiB")
            return source
        if source.startswith("//"):
            return source
        # __file__ points into Pyodide's virtual filesystem. An existing
        # absolute file wins over browser-root URL resolution; missing files
        # retain ordinary URL behavior, just like relative asset references.
        path = Path(source)
        if not (path.is_absolute() and path.is_file()):
            if re.match(r"^[a-z][a-z\d+.-]*:|^[\\/]", source, re.IGNORECASE):
                return source
            parts: list[str] = []
            try:
                for raw in re.split(r"[?#]", source, maxsplit=1)[0].split("/"):
                    if re.search(r"%(?![0-9a-f]{2})", raw, re.IGNORECASE):
                        return source
                    part = unquote(raw, errors="strict")
                    if "/" in part or "\\" in part or "\x00" in part:
                        return source
                    if not part or part == ".":
                        continue
                    if part == "..":
                        if not parts:
                            return source
                        parts.pop()
                    else:
                        parts.append(part)
            except UnicodeDecodeError:
                return source
            path = Path(*parts)
            if not path.resolve().is_relative_to(Path.cwd().resolve()):
                return source
            if not path.is_file():
                return source
        mime = mimetypes.guess_type(path.name)[0]
        if mime and mime.startswith("image/") and mime not in {
            "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"
        }:
            with path.open("rb") as stream:
                data = stream.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("Browser image exceeds 32 MiB")
            return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        return super()._resolve_image_source(str(path))

    @property
    def render_scale(self) -> float:
        scale: object = getattr(self.bridge, "renderScale", 1)
        return float(cast(float, scale))

    @property
    def metrics_revision(self) -> int:
        # Older bridges only expose the combined font/viewport generation.
        revision: object = getattr(self.bridge, "metricsRevision",
                                   getattr(self.bridge, "resourceRevision", 0))
        return self._metrics_revision + int(cast(int, revision))

    @property
    def resource_revision(self) -> int:
        revision: object = getattr(self.bridge, "resourceRevision", 0)
        return self._resource_revision + int(cast(int, revision))

    def _resource_error(self, message: str) -> None:
        self._resource_events.append(Input("resource_error", text=message))

    def present(self) -> None:
        self.publish_scene()
        packet: _FramePacket = self.frame_packet(self._sent_revision)
        self.bridge.present(json.dumps(packet, separators=(",", ":")))
        self._sent_revision = packet["revision"]

    def close(self) -> None:
        try:
            if self._opened:
                self.bridge.close()
        finally:
            self.close_scene()
            self._resource_events.clear()
            self._sent_revision = -1
            self._opened = False

    async def clipboard_read(self) -> str:
        try:
            return str(await self._service_call(self.bridge.readClipboard))
        except Exception as exc:
            raise CapabilityError(
                "Browser clipboard read was denied or is unsupported"
            ) from exc

    async def clipboard_write(self, text: str) -> None:
        try:
            if not await self._service_call(
                lambda operation: self.bridge.writeClipboard(text, operation)
            ):
                raise CapabilityError("Browser clipboard write was cancelled")
        except Exception as exc:
            raise CapabilityError(
                "Browser clipboard write was denied or is unsupported"
            ) from exc

    def text_input(self, rect: Rect | None) -> None:
        super().text_input(rect)
        self.bridge.textInput(
            rect.x if rect else -1,
            rect.y if rect else 0,
            rect.width if rect else -1,
            rect.height if rect else 0,
        )

    async def open_text_file(self) -> TextFile | None:
        try:
            loaded = _loads(await self._service_call(
                lambda operation: self.bridge.openTextFile(MAX_TEXT_BYTES, operation)
            ))
            if loaded is None:
                return None
            return TextFile(**cast(_TextFileData, loaded))
        except Exception as exc:
            raise CapabilityError(f"Browser file import failed: {exc}") from exc

    async def save_text_file(
        self, text: str, suggested_name: str, location: str | None
    ) -> TextFile | None:
        try:
            saved = await self._service_call(
                lambda operation: self.bridge.saveTextFile(text, suggested_name, operation)
            )
            return TextFile(str(saved), text) if saved else None
        except Exception as exc:
            raise CapabilityError(f"Browser file export failed: {exc}") from exc

    async def choose_path(
        self,
        *,
        save: bool = False,
        directory: bool = False,
        suggested_name: str = "",
        title: str = "",
        initial_directory: str | None = None,
        extensions: Sequence[str] = (),
        all_files: bool = False,
    ) -> str | None:
        raise CapabilityError("The browser does not expose filesystem paths")

    async def _service_call(
        self,
        invoke: Callable[[int], Awaitable[object]],
        *,
        cancel: Callable[[int], None] | None = None,
    ) -> object:
        """Keep operation identity and cancellation together for typed bridge calls."""
        self._session_serial += 1
        operation = self._session_serial
        try:
            return await invoke(operation)
        except asyncio.CancelledError as cancelled:
            try:
                (self.bridge.cancelOperation if cancel is None else cancel)(operation)
            except Exception as cleanup_error:
                # Browser teardown must not turn task cancellation into a service
                # failure. Keep cleanup diagnostics attached to the cancellation.
                raise cancelled from cleanup_error
            raise

    async def read_session(self, key: str) -> str | None:
        validate_session_key(key)
        try:
            # JSON carries null explicitly across Pyodide, where JS null and
            # Python None are distinct and Python None can become undefined.
            text = await self._service_call(
                lambda operation: self.bridge.readSessionJSON(key, operation),
                cancel=self.bridge.cancelSession,
            )
            value = _json_object(str(text))
            if isinstance(value, str) or value is None:
                validate_session_text(value)
                return value
            validate_session_text(cast(str | None, value))
            return cast(str | None, value)
        except Exception as exc:
            raise CapabilityError(
                f"Browser session storage read failed: {exc}"
            ) from exc

    async def write_session(self, key: str, text: str | None) -> None:
        validate_session_key(key)
        validate_session_text(text)
        try:
            await self._service_call(
                lambda operation: self.bridge.writeSessionJSON(key, json.dumps(text), operation),
                cancel=self.bridge.cancelSession,
            )
        except Exception as exc:
            raise CapabilityError(
                f"Browser session storage write failed: {exc}"
            ) from exc

    async def open_url(self, url: str) -> None:
        try:
            if not await self._service_call(
                lambda operation: self.bridge.openUrl(url, operation)
            ):
                raise CapabilityError("Opening the URL was cancelled")
        except Exception as exc:
            raise CapabilityError(f"Browser URL opening failed: {exc}") from exc
