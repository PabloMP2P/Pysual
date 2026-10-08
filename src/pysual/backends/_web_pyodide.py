"""Shared SVG renderer delivered locally through a Pyodide browser bridge."""

import asyncio
import base64
import json
import mimetypes
import re
from pathlib import Path
from urllib.parse import unquote

from ._web_svg import SVGRenderer

from ..host import (
    MAX_TEXT_BYTES,
    CapabilityError,
    Input,
    TextFile,
    Viewport,
    validate_session_key,
    validate_session_text,
)


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

    def __init__(self):
        from js import pysualHost  # type: ignore[import-not-found]

        super().__init__()
        self.bridge = pysualHost
        self._session_serial = 0
        self._sent_revision = -1
        self._resource_events = []
        self._opened = False
        self._session_namespace = "default"

    def open(self, title, width, height, resizable, scale):
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

    def set_size(self, width, height):
        raise CapabilityError("The browser owns the app viewport size")

    def set_title(self, title):
        title = str(title)
        if title == self._title:
            return
        super().set_title(title)
        self.bridge.title(title)

    def set_session_namespace(self, namespace):
        self._session_namespace = namespace
        if self._opened:
            self.bridge.setSessionNamespace(namespace)

    @property
    def viewport(self):
        if hasattr(self.bridge, "viewport"):
            data = json.loads(str(self.bridge.viewport))
            data["safe_area"] = tuple(data.get("safe_area", (0, 0, 0, 0)))
            return Viewport(**data)
        return Viewport(*self.size, self.render_scale)

    def poll(self):
        events = json.loads(self.bridge.poll())
        result = []
        for event in events:
            if event.get("kind") == "image_error":
                try:
                    identifier = int(event.get("text", ""))
                except (TypeError, ValueError):
                    continue
                self.report_image_error(identifier)
                continue
            if event.get("viewport") is not None:
                data = event["viewport"]
                data["safe_area"] = tuple(data.get("safe_area", (0, 0, 0, 0)))
                event["viewport"] = Viewport(**data)
            result.append(Input(**event))
        result.extend(self._resource_events)
        self._resource_events.clear()
        self.size = (float(self.bridge.width), float(self.bridge.height))
        return result

    def lines(self, points, color, width: float = 1):
        for first, last in zip(points, points[1:]):
            self.line(first[0], first[1], last[0], last[1], color, width)

    def _resolve_image_source(self, source):
        # Included/package assets exist in Pyodide's virtual filesystem. Other
        # relative paths and absolute URLs retain ordinary browser resolution.
        if source.startswith(("http://", "https://", "blob:")):
            return source
        if source.startswith("data:image/"):
            if source.startswith(tuple(f"data:image/{kind};base64," for kind in ("png", "jpeg", "gif", "webp"))):
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
            parts = []
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
        if mime and mime.startswith("image/") and mime not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
            with path.open("rb") as stream:
                data = stream.read(32 * 1024 * 1024 + 1)
            if len(data) > 32 * 1024 * 1024:
                raise ValueError("Browser image exceeds 32 MiB")
            return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
        return super()._resolve_image_source(str(path))

    @property
    def render_scale(self):
        return float(getattr(self.bridge, "renderScale", 1))

    @property
    def resource_revision(self):
        return self._resource_revision + int(getattr(self.bridge, "resourceRevision", 0))

    def _resource_error(self, message):
        self._resource_events.append(Input("resource_error", text=message))

    def present(self):
        self.publish_scene()
        packet = self.frame_packet(self._sent_revision)
        self.bridge.present(json.dumps(packet, separators=(",", ":")))
        self._sent_revision = packet["revision"]

    def close(self):
        try:
            if self._opened:
                self.bridge.close()
        finally:
            self.close_scene()
            self._resource_events.clear()
            self._sent_revision = -1
            self._opened = False

    async def clipboard_read(self):
        try:
            return str(await self._service_call("readClipboard"))
        except Exception as exc:
            raise CapabilityError(
                "Browser clipboard read was denied or is unsupported"
            ) from exc

    async def clipboard_write(self, text):
        try:
            if not await self._service_call("writeClipboard", text):
                raise CapabilityError("Browser clipboard write was cancelled")
        except Exception as exc:
            raise CapabilityError(
                "Browser clipboard write was denied or is unsupported"
            ) from exc

    def text_input(self, rect):
        super().text_input(rect)
        self.bridge.textInput(
            rect.x if rect else -1,
            rect.y if rect else 0,
            rect.width if rect else -1,
            rect.height if rect else 0,
        )

    async def open_text_file(self):
        try:
            value = json.loads(await self._service_call("openTextFile", MAX_TEXT_BYTES))
            return TextFile(**value) if value is not None else None
        except Exception as exc:
            raise CapabilityError(f"Browser file import failed: {exc}") from exc

    async def save_text_file(self, text, suggested_name, location):
        try:
            saved = await self._service_call("saveTextFile", text, suggested_name)
            return TextFile(str(saved), text) if saved else None
        except Exception as exc:
            raise CapabilityError(f"Browser file export failed: {exc}") from exc

    async def _service_call(self, method, *args, cancel="cancelOperation"):
        self._session_serial += 1
        operation = self._session_serial
        try:
            return await getattr(self.bridge, method)(*args, operation)
        except asyncio.CancelledError:
            getattr(self.bridge, cancel)(operation)
            raise

    async def read_session(self, key):
        validate_session_key(key)
        try:
            # JSON carries null explicitly across Pyodide, where JS null and
            # Python None are distinct and Python None can become undefined.
            text = await self._service_call("readSessionJSON", key, cancel="cancelSession")
            value = json.loads(str(text))
            validate_session_text(value)
            return value
        except Exception as exc:
            raise CapabilityError(
                f"Browser session storage read failed: {exc}"
            ) from exc

    async def write_session(self, key, text):
        validate_session_key(key)
        validate_session_text(text)
        try:
            await self._service_call("writeSessionJSON", key, json.dumps(text), cancel="cancelSession")
        except Exception as exc:
            raise CapabilityError(
                f"Browser session storage write failed: {exc}"
            ) from exc

    async def open_url(self, url):
        try:
            if not await self._service_call("openUrl", url):
                raise CapabilityError("Opening the URL was cancelled")
        except Exception as exc:
            raise CapabilityError(f"Browser URL opening failed: {exc}") from exc
