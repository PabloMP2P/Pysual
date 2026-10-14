"""Explicit, stdlib-only character-cell Host for ordinary VT terminals.

The existing application paints its usual Host primitives; this adapter keeps
native text, colors and box drawing in a bounded cell framebuffer. Its transport
updates changed runs only and never requests or emits a graphics protocol.
"""

import asyncio
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, cast

from ..geometry import Rect
from ..graphemes import boundaries
from ..host import CapabilityError, Input, TextFile
from . import _files
from ._term_cells import RGB, Cell, CellRenderer, safe_text
from ._term_images import AsyncImageCache
from ._term_io import TerminalTransport
from ._term_session import TerminalSession

if TYPE_CHECKING:
    from ..theme import ResolvedStyle, Style

_ANSI16 = (
    (0, 0, 0),
    (128, 0, 0),
    (0, 128, 0),
    (128, 128, 0),
    (0, 0, 128),
    (128, 0, 128),
    (0, 128, 128),
    (192, 192, 192),
    (128, 128, 128),
    (255, 0, 0),
    (0, 255, 0),
    (255, 255, 0),
    (0, 0, 255),
    (255, 0, 255),
    (0, 255, 255),
    (255, 255, 255),
)
_LEVELS = (0, 95, 135, 175, 215, 255)
_ANSI256 = (
    _ANSI16
    + tuple(
        (red, green, blue) for red in _LEVELS for green in _LEVELS for blue in _LEVELS
    )
    + tuple((value, value, value) for value in range(8, 239, 10))
)

# Text-presentation symbols avoid the overlapping slashes produced by tiny
# vector paths. Complex icons retain their original primitive fallback.
_ICON_GLYPHS = {
    "check": "✓",
    "close": "×",
    "plus": "+",
    "minus": "−",
    "play": "▶",
    "pause": "‖",
    "home": "⌂",
    "save": "▣",
    "folder": "▱",
    "search": "⌕",
    "undo": "↶",
    "redo": "↷",
    "chevron_left": "‹",
    "chevron_right": "›",
    "chevron_up": "▴",
    "chevron_down": "▾",
    "arrow_left": "←",
    "arrow_right": "→",
    "arrow_up": "↑",
    "arrow_down": "↓",
    "diamond": "◇",
    "settings": "⚙",
    "layers": "▱▱",
    "sparkles": "✧",
    "sun": "☼",
    "moon": "☾",
    "heart": "♥",
    "copy": "⧉",
    "download": "↓",
    "upload": "↑",
    "refresh": "↻",
    "external_link": "↗",
    "grid": "▦",
    "chart": "▥",
    "bolt": "ϟ",
    "menu": "≡",
    "code": "‹›",
    "mail": "✉",
}


@lru_cache(maxsize=1024)
def _palette_index(color: RGB, count: int) -> int:
    palette = _ANSI16 if count == 16 else _ANSI256
    return min(
        range(count),
        key=lambda index: sum((a - b) ** 2 for a, b in zip(color, palette[index])),
    )


@lru_cache(maxsize=4096)
def _sgr(mode: str, foreground: RGB, background: RGB) -> bytes:
    if mode == "none":
        return b""
    values: tuple[int, ...]
    if mode == "truecolor":
        values = (38, 2, *foreground, 48, 2, *background)
    elif mode == "256":
        values = (
            38,
            5,
            _palette_index(foreground, 256),
            48,
            5,
            _palette_index(background, 256),
        )
    else:
        front, back = _palette_index(foreground, 16), _palette_index(background, 16)
        values = (
            30 + front if front < 8 else 90 + front - 8,
            40 + back if back < 8 else 100 + back - 8,
        )
    return ("\x1b[" + ";".join(map(str, values)) + "m").encode("ascii")


def _color_mode(requested: str, environ: dict[str, str]) -> str:
    if requested not in ("auto", "truecolor", "256", "16", "none"):
        raise ValueError("color must be auto, truecolor, 256, 16 or none")
    if requested != "auto":
        return requested
    if environ.get("NO_COLOR") or environ.get("TERM") == "dumb":
        return "none"
    if (
        environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
        or environ.get("WT_SESSION")
        or environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm", "ghostty")
        or environ.get("TERM", "").startswith(("xterm-kitty", "xterm-ghostty"))
    ):
        return "truecolor"
    return "256" if "256color" in environ.get("TERM", "") else "16"


@dataclass
class _FilePrompt:
    save: bool
    value: str
    cursor: int
    answer: asyncio.Future[str | None]
    error: str = ""
    selected: bool = False
    confirm: bool = False
    confirmed: bool = False
    busy: bool = False
    cancelled: bool = False
    operation: asyncio.Task[bool] | asyncio.Task[TextFile] | None = None


class TermTextHost(TerminalSession):
    """Native text host selected explicitly by ``backend="terminal"``.

    One emulator column represents eight logical units and one row sixteen.
    Terminal font settings own the physical font and DPI. ``color`` can select
    an explicit ANSI depth; automatic selection honors a nonempty NO_COLOR.
    Files use an in-terminal path prompt, including in SSH or headless shells.
    """

    backend = "terminal"
    renderer = "python"
    capabilities = TerminalSession.capabilities | {
        "text_files", "images", "tinted_images", "image_fit", "render_surfaces",
    }
    PROBE = b"\x1b[18t\x1b[?u"
    GEOMETRY_QUERY = b"\x1b[18t"
    CELL_WIDTH = 8.0
    CELL_HEIGHT = 16.0
    text_row_height = CELL_HEIGHT
    MAX_WRITE = 64 * 1024

    def __init__(
        self,
        *,
        io: TerminalTransport | None = None,
        probe_timeout: float = 0.15,
        environ: Mapping[str, str] | None = None,
        color: str = "auto",
    ) -> None:
        super().__init__(
            io=io, protocol="text", probe_timeout=probe_timeout, environ=environ
        )
        self.color = _color_mode(color, self._environ)
        self._renderer = CellRenderer(1, 1, monochrome=self.color == "none")
        self._last_cells: tuple[Cell, ...] | None = None
        self._prompt: _FilePrompt | None = None
        self._file_lock = asyncio.Lock()
        self._generation = 0

    def open(
        self,
        title: str,
        width: float,
        height: float,
        resizable: bool,
        scale: float | None,
    ) -> None:
        if scale is not None and scale != 1:
            raise CapabilityError(
                "TermTextHost uses terminal-owned cells; scale must be 1 or None"
            )
        super().open(title, width, height, resizable, scale)

    def _reset_open_state(self) -> None:
        super()._reset_open_state()
        self._renderer._images.close()
        self._renderer._images = AsyncImageCache()
        self._generation += 1
        self._last_cells = None
        self._file_lock = asyncio.Lock()

    def _geometry(self) -> bool:
        facts = self.features
        columns, rows = max(1, facts.columns), max(1, facts.rows)
        if (
            max(columns, rows) > CellRenderer.MAX_EDGE
            or columns * rows > CellRenderer.MAX_CELLS
        ):
            raise CapabilityError("The terminal exceeds the text framebuffer budget")
        size = (columns * self.CELL_WIDTH, rows * self.CELL_HEIGHT)
        changed = size != self.size or self.scale != 1.0
        self.size, self.scale = size, 1.0
        facts.cell_width, facts.cell_height = self.CELL_WIDTH, self.CELL_HEIGHT
        facts.pixel_mouse = False
        self._parser.cell_width, self._parser.cell_height = (
            self.CELL_WIDTH,
            self.CELL_HEIGHT,
        )
        self._parser.scale, self._parser.pixel_mouse = 1.0, False
        if changed or (self._renderer.columns, self._renderer.rows) != (columns, rows):
            self._renderer.resize(*size)
            self.resource_revision += 1
            self._last_cells = None
        return changed

    def begin(self, background: str) -> None:
        self._renderer.begin(background)

    def clip(self, rect: Rect | None) -> None:
        self._renderer.clip(rect)

    def rect(
        self,
        rect: Rect,
        fill: str,
        radius: float = 0,
        border: str = "",
        border_width: float = 1,
    ) -> None:
        self._renderer.rect(rect, fill, radius, border, border_width)

    def text(
        self, text: str, x: float, y: float, color: str, size: float, mono: bool = False
    ) -> None:
        self._renderer.text(text, x, y, color, size, mono)

    def measure(
        self, text: str, size: float, mono: bool = False
    ) -> tuple[float, float]:
        return self._renderer.measure(text, size, mono)

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: str,
        width: float = 1,
    ) -> None:
        self._renderer.line(x1, y1, x2, y2, color, width)

    def caret(self, x: float, y: float, height: float, color: str) -> bool:
        self._renderer.caret(x, y, height, color)
        return True

    def focus_ring(self, rect: Rect, color: str, radius: float = 0) -> bool:
        self._renderer.focus_ring(rect, color, radius, monochrome=self.color == "none")
        return True

    def image(
        self,
        source: str,
        rect: Rect,
        *,
        tint: str = "#ffffff",
        fit: str = "stretch",
        _edges: tuple[int, int, int, int] | None = None,
    ) -> None:
        try:
            self._renderer.image(source, rect, tint=tint, fit=fit, _edges=_edges)
        except (OSError, ValueError) as exc:
            self._renderer.image_placeholder(rect, tint=tint)
            message = safe_text(str(exc)[:512])
            key = (hash(source), message)
            if key not in self._image_errors:
                self._image_errors[key] = None
                while len(self._image_errors) > 128:
                    self._image_errors.popitem(last=False)
                self._resource_events.append(
                    Input("resource_error", text=f"terminal image unavailable: {message}")
                )

    def image_nine(
        self,
        source: str,
        rect: Rect,
        edges: tuple[int, int, int, int],
        *,
        tint: str = "#ffffff",
    ) -> None:
        self.image(source, rect, tint=tint, _edges=edges)

    def reload_image(self, source: str) -> None:
        self._renderer._images.invalidate(source)
        source_key = hash(source)
        for key in tuple(self._image_errors):
            if key[0] == source_key:
                del self._image_errors[key]
        self.resource_revision += 1
        self._renderer.resource_revision += 1
        self._repaint()

    def gradient_rect(
        self,
        rect: Rect,
        first: str,
        last: str,
        axis: str,
        radius: float,
        border_width: float = 0,
    ) -> bool:
        return self._renderer.gradient_rect(
            rect, first, last, axis, radius, border_width
        )

    def styled_rect(self, rect: Rect, style: "Style | ResolvedStyle") -> bool:
        return self._renderer.styled_rect(rect, style)

    def marker(
        self,
        rect: Rect,
        style: "Style | ResolvedStyle",
        *,
        shape: str = "square",
        checked: bool = False,
    ) -> bool:
        return self._renderer.marker(rect, style, shape=shape, checked=checked)

    def icon(self, name: str, x: float, y: float, size: float, color: str) -> bool:
        glyph = _ICON_GLYPHS.get(name)
        if glyph is None:
            return False
        self._renderer.icon(glyph, x, y, size, color)
        return True

    def surface_create(self, rect: Rect) -> object:
        return self._renderer.surface_create(rect)

    def surface_byte_size(self, rect: Rect) -> int:
        return self._renderer.surface_byte_size(rect)

    def surface_matches(self, surface: object) -> bool:
        return self._renderer.surface_matches(surface)

    def surface_begin(self, surface: object) -> None:
        self._renderer.surface_begin(surface)

    def surface_end(self) -> None:
        self._renderer.surface_end()

    def surface_blit(self, surface: object) -> None:
        self._renderer.surface_blit(surface)

    def surface_release(self, surface: object) -> None:
        self._renderer.surface_release(surface)

    def _changes(self, cells: tuple[Cell, ...]) -> Iterator[bytes]:
        previous, columns = self._last_cells, self._renderer.columns
        style = None
        underline = False
        for start in range(0, len(cells), columns):
            row = cells[start : start + columns]
            old = previous[start : start + columns] if previous is not None else None
            dirty = [
                old is None
                or (cell.text, cell.width) != (old[x].text, old[x].width)
                or cell.underline != old[x].underline
                or (self.color != "none" and cell != old[x])
                for x, cell in enumerate(row)
            ]
            # Redraw both halves of old and new wide cells. Closure also covers
            # overlapping wide glyphs after a one-column insertion or deletion.
            pending = [x for x, changed in enumerate(dirty) if changed]
            while pending:
                x = pending.pop()
                for frame in (row, old):
                    if frame is None:
                        continue
                    paired = x - 1 if frame[x].width == 0 else x + 1
                    if frame[x].width == 1 or not 0 <= paired < columns:
                        continue
                    if not dirty[paired]:
                        dirty[paired] = True
                        pending.append(paired)
            x = 0
            while x < columns:
                if not dirty[x]:
                    x += 1
                    continue
                yield f"\x1b[{start // columns + 1};{x + 1}H".encode("ascii")
                while x < columns and dirty[x]:
                    cell = row[x]
                    if cell.width:
                        next_style = _sgr(self.color, cell.foreground, cell.background)
                        if next_style != style:
                            yield next_style
                            style = next_style
                        if cell.underline != underline:
                            yield b"\x1b[4m" if cell.underline else b"\x1b[24m"
                            underline = cell.underline
                        yield cell.text.encode("utf-8")
                    x += 1

    def present(self) -> None:
        if not self._active:
            return
        images = self._renderer._images
        if isinstance(images, AsyncImageCache):
            images.finish_frame()
        cells = (
            self._paint_prompt()
            if self._prompt is not None
            else tuple(self._renderer.cells)
        )
        if cells == self._last_cells:
            return
        changes = self._changes(cells)
        first = next(changes, None)
        if first is not None:
            # Bound individual writes, retaining synchronized output across a
            # large frame. Session cleanup also resets this mode after failure.
            buffer = bytearray(b"\x1b[?2026h")
            buffer.extend(first)
            try:
                for piece in changes:
                    if len(buffer) + len(piece) > self.MAX_WRITE:
                        self.io.write(bytes(buffer))
                        buffer.clear()
                    buffer.extend(piece)
                tail = b"\x1b[0m\x1b[?2026l"
                if len(buffer) + len(tail) > self.MAX_WRITE:
                    self.io.write(bytes(buffer))
                    buffer.clear()
                buffer.extend(tail)
                self.io.write(bytes(buffer))
            except BaseException:
                try:
                    self.io.write(b"\x1b[0m\x1b[?2026l")
                except (OSError, EOFError):
                    pass
                raise
        self._last_cells = cells

    def text_input(self, rect: Rect | None) -> None:
        # Shared controls paint their own caret into character cells.
        pass

    def _repaint(self) -> None:
        self._resource_events.append(Input("repaint"))

    def poll(self) -> list[Input]:
        images = self._renderer._images
        if self._active and isinstance(images, AsyncImageCache) and images.poll():
            # Cached controls and cached cell surfaces have independent keys.
            # Both must stop reusing a placeholder when its pixels arrive.
            self.resource_revision += 1
            self._renderer.resource_revision += 1
            self._repaint()
        result = super().poll()
        if self._prompt is None:
            return result
        retained: list[Input] = []
        for event in result:
            if event.kind == "close":
                self._cancel_prompt()
                self._prompt = None
            if event.kind in ("close", "viewport", "resource_error", "repaint", "blur"):
                retained.append(event)
            else:
                self._prompt_input(event)
        return retained

    def _cancel_prompt(self) -> None:
        prompt = self._prompt
        if prompt is None:
            return
        prompt.cancelled = True
        if prompt.operation is not None:
            prompt.operation.cancel()
        if not prompt.answer.done():
            prompt.answer.set_result(None)
        self._prompt = None
        self._repaint()

    def _prompt_input(self, event: Input) -> None:
        prompt = self._prompt
        if prompt is None or prompt.cancelled:
            return
        if event.kind == "key_down" and (
            event.key == "Escape" or (event.ctrl and event.key.lower() == "c")
        ):
            self._cancel_prompt()
            return
        if prompt.busy or prompt.answer.done():
            return
        if prompt.confirm:
            if (event.kind == "key_down" and event.key == "Enter") or (
                event.kind == "text" and event.text.lower() == "y"
            ):
                prompt.confirmed = True
                prompt.answer.set_result(prompt.value)
            elif event.kind == "text" and event.text.lower() == "n":
                prompt.confirm = False
                self._repaint()
            return
        old = (prompt.value, prompt.cursor, prompt.selected)
        if event.kind == "text":
            inserted = safe_text(event.text).replace("\n", "").replace("\t", "")
            if prompt.selected:
                prompt.value, prompt.cursor, prompt.selected = "", 0, False
            inserted = inserted[: max(0, 4096 - len(prompt.value))]
            prompt.value = (
                prompt.value[: prompt.cursor] + inserted + prompt.value[prompt.cursor :]
            )
            prompt.cursor += len(inserted)
        elif event.kind == "key_down":
            points = boundaries(prompt.value)
            left = next((p for p in reversed(points) if p < prompt.cursor), 0)
            right = next((p for p in points if p > prompt.cursor), len(prompt.value))
            key = event.key
            if event.ctrl and key.lower() == "a":
                prompt.selected = True
            elif key in ("ArrowLeft", "ArrowRight", "Home", "End"):
                prompt.cursor = {
                    "ArrowLeft": left,
                    "ArrowRight": right,
                    "Home": 0,
                    "End": len(prompt.value),
                }[key]
                prompt.selected = False
            elif key in ("Backspace", "Delete"):
                if prompt.selected:
                    prompt.value, prompt.cursor, prompt.selected = "", 0, False
                elif key == "Backspace":
                    prompt.value = prompt.value[:left] + prompt.value[prompt.cursor :]
                    prompt.cursor = left
                else:
                    prompt.value = prompt.value[: prompt.cursor] + prompt.value[right:]
            elif event.ctrl and key.lower() == "u":
                prompt.value, prompt.cursor = prompt.value[prompt.cursor :], 0
                prompt.selected = False
            elif event.ctrl and key.lower() == "k":
                prompt.value = prompt.value[: prompt.cursor]
                prompt.selected = False
            elif key == "Enter" and prompt.value:
                prompt.answer.set_result(prompt.value)
        if old != (prompt.value, prompt.cursor, prompt.selected):
            prompt.error, prompt.confirmed = "", False
            self._repaint()

    def _paint_prompt(self) -> tuple[Cell, ...]:
        prompt = self._prompt
        if prompt is None:
            return tuple(self._renderer.cells)
        columns, rows = self._renderer.columns, self._renderer.rows
        width, height = min(columns, 84), min(rows, 9)
        pane = CellRenderer(width, height)
        pane.begin("#192330")
        pane.rect(Rect(0, 0, width * 8, height * 16), "", 0, "#8aa6c9", 1)
        padding = 2 if width > 8 else 0
        available = max(1, width - 2 * padding)
        title = "Save UTF-8 text file" if prompt.save else "Open UTF-8 text file"
        status = (
            "Working... Esc cancels"
            if prompt.busy
            else "Replace existing file? [y/Enter] Yes  [n] Edit"
            if prompt.confirm
            else "Enter: confirm path    Esc: cancel"
        )
        pane.text(
            title[:available], padding * 8, 16 if height > 3 else 0, "#ffffff", 16
        )
        path_row = min(height - 1, 4 if height > 5 else 1)
        # Locate the visible tail by measuring each nearby grapheme once.
        # Measuring every remaining suffix makes a long pasted path quadratic.
        points = boundaries(prompt.value[: prompt.cursor])
        start = end = prompt.cursor
        advance = 0.0
        for point in reversed(points[:-1]):
            step = pane.measure(prompt.value[point:end], 16)[0]
            if advance + step >= available * 8:
                break
            start = end = point
            advance += step
        pane.clip(Rect(padding * 8, path_row * 16, available * 8, 16))
        pane.text(prompt.value[start:], padding * 8, path_row * 16, "#f1d994", 16)
        pane.clip(None)
        cursor = padding + round(advance / 8)
        if not prompt.busy and not prompt.confirm and 0 <= cursor < width:
            offset = path_row * width + cursor
            cell = pane.cells[offset]
            if cell.width == 0 and cursor:
                offset -= 1
                cell = pane.cells[offset]
            pane.cells[offset] = Cell(
                cell.text, cell.background, cell.foreground, cell.width
            )
            if cell.width == 2 and offset + 1 < len(pane.cells):
                continuation = pane.cells[offset + 1]
                pane.cells[offset + 1] = Cell(
                    "", cell.background, cell.foreground, continuation.width
                )
            if self.color == "none" and height > path_row + 1:
                pane.text("^", cursor * 8, (path_row + 1) * 16, "#ffffff", 16)
        if height > 5:
            pane.text(
                (prompt.error or status)[:available],
                padding * 8,
                (height - 2) * 16,
                "#ffb0a5" if prompt.error else "#b9c9dc",
                16,
            )
        target = list(self._renderer.cells)
        left, top = (columns - width) // 2, (rows - height) // 2
        for y in range(height):
            offset = (top + y) * columns + left
            # The overlay must not leave half of a wide application glyph.
            if left and target[offset].width == 0:
                previous = target[offset - 1]
                target[offset - 1] = Cell(background=previous.background)
            end = offset + width
            if left + width < columns and target[end].width == 0:
                target[end] = Cell(background=target[end].background)
            target[offset:end] = pane.cells[y * width : (y + 1) * width]
        return tuple(target)

    async def _text_file(
        self, *, save: bool, text: str = "", suggested_name: str = ""
    ) -> TextFile | None:
        generation = self._generation
        async with self._file_lock:
            if not self._active or generation != self._generation:
                raise CapabilityError("Open the terminal host before choosing a file")
            loop = asyncio.get_running_loop()
            value = safe_text(suggested_name).replace("\n", "").replace("\t", "")[:4096]
            prompt = _FilePrompt(
                save,
                value,
                len(value),
                cast("asyncio.Future[str | None]", loop.create_future()),
                selected=bool(value),
            )
            self._prompt = prompt
            self._repaint()
            try:
                while True:
                    chosen = await prompt.answer
                    if chosen is None or prompt.cancelled:
                        return None
                    prompt.busy = True
                    self._repaint()
                    try:
                        path = Path(chosen).expanduser()
                        if save and not prompt.confirmed:
                            exists_task = asyncio.create_task(asyncio.to_thread(path.exists))
                            prompt.operation = exists_task
                            exists = await exists_task
                            if prompt.cancelled:
                                return None
                            if exists:
                                prompt.confirm, prompt.busy = True, False
                                prompt.answer = cast(
                                    "asyncio.Future[str | None]", loop.create_future()
                                )
                                self._repaint()
                                continue
                        file_task = asyncio.create_task(
                            _files.write_text(path, text)
                            if save
                            else _files.read_text(path)
                        )
                        prompt.operation = file_task
                        return await file_task
                    except asyncio.CancelledError:
                        if prompt.cancelled:
                            return None
                        raise
                    except (OSError, UnicodeError, ValueError) as exc:
                        prompt.error = safe_text(str(exc)).replace("\n", " ")[:512]
                        prompt.confirm = prompt.confirmed = prompt.busy = False
                        prompt.answer = cast(
                            "asyncio.Future[str | None]", loop.create_future()
                        )
                        self._repaint()
                    finally:
                        prompt.operation = None
            finally:
                if self._prompt is prompt:
                    self._prompt = None
                    self._repaint()

    async def open_text_file(self) -> TextFile | None:
        return await self._text_file(save=False)

    async def save_text_file(
        self, text: str, suggested_name: str, location: str | None
    ) -> TextFile | None:
        if location is not None:
            return await _files.write_text(location, text)
        return await self._text_file(
            save=True, text=text, suggested_name=suggested_name
        )

    def _close_target(self) -> None:
        self._generation += 1
        self._cancel_prompt()
        self._prompt = None
        self._last_cells = None
        self._renderer.close()
        self._renderer = CellRenderer(1, 1, monochrome=self.color == "none")
