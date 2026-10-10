"""A bounded character-cell painter, independent of pixel and font libraries.

Logical coordinates map to a fixed terminal cell size. Font size cannot change
the emulator's font, so measuring and painting use the same native cell width.
East Asian ambiguous characters occupy one column; wide characters and emoji
presentation clusters occupy two. Emulator Unicode versions may differ.
Category and width data share the C renderer's pinned Unicode 15.1 baseline;
grapheme boundaries use the separately pinned Unicode 17.0 data.
"""

from bisect import bisect_right
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from math import ceil, floor, isfinite
from typing import TYPE_CHECKING

from ..geometry import Rect
from ..graphemes import boundaries
from ..host import CapabilityError
from .._terminal_data import COMBINING, CONTROL, FLAGS, STARTS, SURROGATE, WIDE
from ._term_images import ImageCache, image_cells, luminance_glyph

if TYPE_CHECKING:
    from ..theme import Style

RGB = tuple[int, int, int]
RGBA = tuple[int, int, int, int]


@dataclass(eq=False)
class _CellSurface:
    """A clipped sub-grid and the backdrop its paint was composed against."""

    col: int
    row: int
    columns: int
    rows: int
    clip: tuple[int, int, int, int]
    revision: int
    cells: list
    ink: bytearray
    backdrop: list
    backdrop_ink: bytearray
    released: bool = False


@dataclass(frozen=True, slots=True)
class Cell:
    """One screen position; width zero belongs to the preceding wide glyph."""

    text: str = " "
    foreground: RGB = (255, 255, 255)
    background: RGB = (0, 0, 0)
    width: int = 1
    underline: bool = False


@lru_cache(maxsize=512)
def _color(color: str) -> RGBA:
    value = color.removeprefix("#")
    if len(value) in (3, 4):
        value = "".join(channel * 2 for channel in value)
    if len(value) == 6:
        value += "ff"
    if len(value) != 8:
        raise ValueError(f"Invalid color: {color!r}")
    return (
        int(value[0:2], 16),
        int(value[2:4], 16),
        int(value[4:6], 16),
        int(value[6:8], 16),
    )


def _over(source: RGBA, destination: RGB) -> RGB:
    alpha = source[3]
    inverse = 255 - alpha
    return (
        (source[0] * alpha + destination[0] * inverse + 127) // 255,
        (source[1] * alpha + destination[1] * inverse + 127) // 255,
        (source[2] * alpha + destination[2] * inverse + 127) // 255,
    )


def _mix(first: RGBA, last: RGBA, amount: float) -> RGBA:
    return (
        round(first[0] + (last[0] - first[0]) * amount),
        round(first[1] + (last[1] - first[1]) * amount),
        round(first[2] + (last[2] - first[2]) * amount),
        round(first[3] + (last[3] - first[3]) * amount),
    )


@lru_cache(maxsize=4096)
def _character_flags(character: str) -> int:
    return FLAGS[bisect_right(STARTS, ord(character)) - 1]


def safe_text(text: str) -> str:
    """Keep printable Unicode, line breaks and tabs, never terminal controls.

    U+0000 is removed, as it is for native display text, so the characters
    after it stay visible. Other controls are dropped only for terminals.
    """
    result = []
    for character in text:
        flags = _character_flags(character)
        if flags & SURROGATE:
            result.append("\ufffd")
        elif character in "\n\t\u200c\u200d" or not flags & CONTROL:
            result.append(character)
    return "".join(result)


@lru_cache(maxsize=4096)
def _cluster_cell(cluster: str) -> tuple[str, int]:
    # A malicious combining sequence must not make one stored cell unbounded.
    cluster = cluster[:64]
    bases = [
        character
        for character in cluster
        if not _character_flags(character) & COMBINING
    ]
    if not bases:
        return "\u25cc" + cluster, 1
    emoji = (
        "\ufe0f" in cluster
        or "\u20e3" in cluster
        or (
            "\u200d" in cluster
            and any(
                0x2600 <= ord(character) <= 0x27BF
                or 0x1F000 <= ord(character) <= 0x1FAFF
                for character in bases
            )
        )
    )
    wide = emoji or any(
        _character_flags(character) & WIDE
        or 0x1F1E6 <= ord(character) <= 0x1F1FF
        for character in bases
    )
    return cluster, 2 if wide else 1


def text_cells(text: str):
    """Yield sanitized graphemes and widths, with newline/tab sentinels."""
    cleaned = safe_text(text)
    offsets = boundaries(cleaned)
    for start, end in pairwise(offsets):
        cluster = cleaned[start:end]
        if cluster in ("\n", "\t"):
            yield cluster, 0
        else:
            # Truncate before the cached call, keeping cache keys bounded too.
            yield _cluster_cell(cluster[:64])


_BOX_MASKS = {
    "│": 5,
    "─": 10,
    "┌": 6,
    "┐": 12,
    "└": 3,
    "┘": 9,
    "├": 7,
    "┤": 13,
    "┬": 14,
    "┴": 11,
    "┼": 15,
    "╭": 6,
    "╮": 12,
    "╰": 3,
    "╯": 9,
}
_MASK_BOX = {mask: glyph for glyph, mask in tuple(_BOX_MASKS.items())[:11]}


class CellRenderer:
    """Convert the Host painter protocol into readable terminal characters."""

    MAX_CELLS = 1024 * 1024
    MAX_EDGE = 16384

    def __init__(
        self,
        columns: int,
        rows: int,
        *,
        cell_width: float = 8.0,
        cell_height: float = 16.0,
        monochrome: bool = False,
    ):
        if any(
            not isfinite(value) or value <= 0 for value in (cell_width, cell_height)
        ):
            raise ValueError("Cell dimensions must be finite and positive")
        if type(columns) is not int or type(rows) is not int:
            raise ValueError("Cell counts must be integers")
        self.cell_width, self.cell_height = float(cell_width), float(cell_height)
        self.monochrome = monochrome
        self._images = ImageCache()
        self.resource_revision = 0
        self._background: RGB = (0, 0, 0)
        self._origin_col = 0
        self._origin_row = 0
        self._target = None
        self._surfaces: set = set()
        self._saved_surface = None
        self.resize(columns * cell_width, rows * cell_height)

    def resize(self, width: float, height: float, scale: float = 1) -> None:
        if any(not isfinite(value) or value <= 0 for value in (width, height, scale)):
            raise ValueError("Text dimensions and scale must be finite and positive")
        column_count, row_count = width / self.cell_width, height / self.cell_height
        if not isfinite(column_count) or not isfinite(row_count):
            raise ValueError("Text framebuffer exceeds its dimension budget")
        columns = max(1, floor(column_count))
        rows = max(1, floor(row_count))
        if max(columns, rows) > self.MAX_EDGE or columns * rows > self.MAX_CELLS:
            raise ValueError("Text framebuffer exceeds its 1 Mi-cell/16384 edge budget")
        self.columns, self.rows = columns, rows
        self.size = float(width), float(height)
        self.cells = [Cell(background=self._background)] * (columns * rows)
        # 0 = decoration, 1 = text, 2 = image with stored upper/lower samples.
        self._ink = bytearray(columns * rows)
        self.resource_revision += 1
        self.clip(None)

    @property
    def render_scale(self) -> float:
        return 1.0

    def close(self) -> None:
        self.cells.clear()
        self._ink.clear()
        self._images.close()
        for surface in self._surfaces:
            surface.released = True
            surface.cells = []
            surface.ink = bytearray()
            surface.backdrop = []
            surface.backdrop_ink = bytearray()
        self._surfaces.clear()
        self._target = None
        self._saved_surface = None
        self.resource_revision += 1

    def snapshot_rows(self) -> tuple[str, ...]:
        return tuple(
            "".join(cell.text for cell in self.cells[start : start + self.columns])
            for start in range(0, len(self.cells), self.columns)
        )

    def begin(self, background: str) -> None:
        self._background = _over(_color(background), (0, 0, 0))
        self.cells = [Cell(background=self._background)] * (self.columns * self.rows)
        self._ink = bytearray(self.columns * self.rows)
        self.clip(None)

    def _box(self, rect: Rect) -> tuple[int, int, int, int]:
        if not all(
            isfinite(value)
            for value in (
                rect.x,
                rect.y,
                rect.width,
                rect.height,
                rect.right,
                rect.bottom,
            )
        ):
            raise ValueError("Text geometry must be finite")
        if rect.width <= 0 or rect.height <= 0:
            return 0, 0, 0, 0
        # Cell centers decide background/clip ownership. Adjacent rectangles
        # then never fight over a partly covered cell along a shared edge.
        left, top = rect.x / self.cell_width, rect.y / self.cell_height
        right, bottom = rect.right / self.cell_width, rect.bottom / self.cell_height
        if not all(isfinite(value) for value in (left, top, right, bottom)):
            raise ValueError("Text geometry exceeds its coordinate budget")
        left, top = ceil(left - 0.5), ceil(top - 0.5)
        right, bottom = ceil(right - 0.5), ceil(bottom - 0.5)
        if self._origin_col or self._origin_row:
            left -= self._origin_col
            right -= self._origin_col
            top -= self._origin_row
            bottom -= self._origin_row
        return left, top, right, bottom

    def clip(self, rect: Rect | None) -> None:
        left, top, right, bottom = (
            (0, 0, self.columns, self.rows) if rect is None else self._box(rect)
        )
        self._clip = (
            max(0, left),
            max(0, top),
            min(self.columns, right),
            min(self.rows, bottom),
        )
        if self._target is not None:
            # The extra columns only hold neighbouring halves of wide glyphs;
            # resetting a painter's clip must not expose this padding to paint.
            self._clip = self._bounds(self._target.clip)

    def _bounds(self, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        left, top, right, bottom = self._clip
        return (
            max(left, box[0]),
            max(top, box[1]),
            min(right, box[2]),
            min(bottom, box[3]),
        )

    def _erase(self, column: int, row: int) -> None:
        """Erase both halves even when clipping selected only half of a glyph."""
        index = row * self.columns + column
        cell = self.cells[index]
        start = column - 1 if cell.width == 0 else column
        end = column + 2 if cell.width == 2 else column + 1
        for current in range(max(0, start), min(self.columns, end)):
            offset = row * self.columns + current
            previous = self.cells[offset]
            self.cells[offset] = Cell(background=previous.background)
            self._ink[offset] = 0

    def _put(
        self,
        column: int,
        row: int,
        text: str,
        width: int,
        color: RGBA,
        *,
        text_ink: bool = False,
    ) -> None:
        left, top, right, bottom = self._clip
        if not (left <= column and column + width <= right and top <= row < bottom):
            return
        if not color[3]:
            return
        for current in range(column, column + width):
            if self.cells[row * self.columns + current].width != 1:
                self._erase(current, row)
        index = row * self.columns + column
        background = self.cells[index].background
        foreground = _over(color, background)
        self.cells[index] = Cell(text, foreground, background, width)
        if width == 2:
            self.cells[index + 1] = Cell("", foreground, background, 0)
        for offset in range(index, index + width):
            self._ink[offset] = text_ink

    def _fill(self, column: int, row: int, color: RGBA) -> None:
        if not color[3]:
            return
        index = row * self.columns + column
        previous = self.cells[index]
        if color[3] == 255:
            if previous.width != 1:
                self._erase(column, row)
            self.cells[index] = Cell(background=(color[0], color[1], color[2]))
            self._ink[index] = 0
        else:
            # Shadows and translucent overlays tint existing glyphs instead
            # of replacing their readable text with a space.
            foreground = _over(color, previous.foreground)
            background = _over(color, previous.background)
            if previous.width != 1:
                start = column - 1 if previous.width == 0 else column
                for current in (start, start + 1):
                    offset = row * self.columns + current
                    old = self.cells[offset]
                    self.cells[offset] = Cell(
                        old.text, foreground, background, old.width, old.underline
                    )
            else:
                self.cells[index] = Cell(
                    luminance_glyph(foreground, background)
                    if self.monochrome and self._ink[index] == 2
                    else previous.text,
                    foreground,
                    background,
                    previous.width,
                    previous.underline,
                )

    def rect(
        self,
        rect: Rect,
        fill: str,
        radius: float = 0,
        border: str = "",
        border_width: float = 1,
    ) -> None:
        box = self._box(rect)
        left, top, right, bottom = self._bounds(box)
        if fill and rect.width > 0 and rect.height > 0:
            if rect.height < self.cell_height / 2 and rect.width >= self.cell_width:
                self.line(
                    rect.x, rect.y, rect.right - self.cell_width / 2, rect.y, fill
                )
                return
            if rect.width < self.cell_width / 2 and rect.height >= self.cell_height:
                self.line(
                    rect.x, rect.y, rect.x, rect.bottom - self.cell_height / 2, fill
                )
                return
        if fill:
            color = _color(fill)
            for row in range(top, bottom):
                for column in range(left, right):
                    if (
                        color[3] < 255
                        and column > left
                        and self.cells[row * self.columns + column].width == 0
                    ):
                        continue  # The preceding lead already tinted the pair.
                    self._fill(column, row, color)
        if border and border_width > 0 and box[2] > box[0] and box[3] > box[1]:
            self._border(box, _color(border), radius=radius)

    def _border(
        self, box, first, last=None, *, axis="vertical", radius: float = 0, compact=False
    ):
        """Paint each perimeter cell once; overlaid outlines are not junctions."""
        left, top, right, bottom = self._bounds(box)
        x1, y1, x2, y2 = box[0], box[1], box[2] - 1, box[3] - 1
        if x2 < x1 or y2 < y1:
            return
        corners = "╭╮╰╯" if radius > 0 else "┌┐└┘"

        def put(x, y, glyph):
            amount = (
                (x - x1) / max(1, x2 - x1)
                if axis == "horizontal"
                else (y - y1) / max(1, y2 - y1)
            )
            color = first if last is None else _mix(first, last, amount)
            self._stroke(x, y, glyph, color, merge=False)

        # A one- or two-row control has no room for horizontal borders plus
        # text. Side caps retain its outline without striking through labels.
        if compact and y2 - y1 < 2 and x1 != x2:
            for row in range(top, bottom):
                if y1 == y2:
                    put(x1, row, "[")
                    put(x2, row, "]")
                else:
                    put(x1, row, corners[0 if row == y1 else 2])
                    put(x2, row, corners[1 if row == y1 else 3])
            return
        for column in range(left, right):
            if x1 < column < x2 or y1 == y2:
                put(column, y1, "─")
                if y2 != y1:
                    put(column, y2, "─")
        if y1 != y2:
            for row in range(top, bottom):
                if y1 < row < y2 or x1 == x2:
                    put(x1, row, "│")
                    if x2 != x1:
                        put(x2, row, "│")
        if x1 != x2 and y1 != y2:
            for x, y, glyph in zip((x1, x2, x1, x2), (y1, y1, y2, y2), corners):
                put(x, y, glyph)

    def marker(self, rect: Rect, style: "Style", *, shape="square", checked=False):
        """Lower an explicit control marker to one cell, independently of old ink."""
        if shape not in ("square", "circle"):
            raise ValueError("Marker shape must be square or circle")
        if type(checked) is not bool:
            raise TypeError("Marker checked state must be a bool")
        self._box(rect)  # Validate the same logical geometry as other surfaces.
        if rect.width <= 0 or rect.height <= 0:
            return True
        fill = _color(style.fill) if style.fill else (0, 0, 0, 0)
        end = _color(style.fill_end) if style.fill_end else fill
        fill = _mix(fill, end, 0.5)
        border = style.border if style.border_width else ""
        foreground = _color(style.foreground) if style.foreground else fill
        column = ceil((rect.x + rect.width / 2) / self.cell_width) - 1
        row = ceil((rect.y + rect.height / 2) / self.cell_height) - 1
        column -= self._origin_col
        row -= self._origin_row
        left, top, right, bottom = self._clip
        if left <= column < right and top <= row < bottom:
            if shape == "square":
                self._fill(column, row, fill)
                glyph = "✓" if checked else "□"
                color = (
                    foreground if checked else (_color(border) if border else foreground)
                )
            else:
                glyph = "◉" if checked else ("○" if border else "●")
                color = _color(border) if border else (foreground if checked else fill)
            self._put(column, row, glyph, 1, color, text_ink=True)
        return True

    def styled_rect(self, rect: Rect, style: "Style") -> bool:
        """Resolve a themed surface once, at the terminal's actual resolution.

        Pixel halos, bevels, scanlines and inner borders cannot occupy separate
        cells. The primary fill/border and state colors carry that information.
        Layout, clipping and control hit regions remain in logical coordinates.
        """
        box = self._box(rect)
        if rect.width <= 0 or rect.height <= 0:
            return True
        radius = min(style.radius or 0, rect.width / 2, rect.height / 2)
        fill = _color(style.fill) if style.fill else (0, 0, 0, 0)
        end = _color(style.fill_end) if style.fill_end else fill
        border = style.border if style.border_width else ""
        axis = style.gradient_axis or "vertical"
        if rect.height < self.cell_height / 2 and rect.width >= self.cell_width:
            # Sub-row tracks must remain visible even between cell centers.
            row = ceil((rect.y + rect.height / 2) / self.cell_height) - 1
            row -= self._origin_row
            first = _color(border) if border else fill
            last = (
                (_color(style.border_end) if style.border_end else first)
                if border
                else end
            )
            left, _, right, _ = self._bounds(box)
            for column in range(left, right):
                amount = (
                    (column - box[0]) / max(1, box[2] - box[0] - 1)
                    if axis == "horizontal"
                    else 0.5
                )
                self._stroke(column, row, "─", _mix(first, last, amount), merge=False)
            return True
        if style.fill:
            if style.fill_end and style.fill_end != style.fill:
                self.gradient_rect(rect, style.fill, style.fill_end, axis, radius)
            else:
                self.rect(rect, style.fill, radius)
        if border:
            self._border(
                box,
                _color(border),
                _color(style.border_end) if style.border_end else None,
                axis=axis,
                radius=radius,
                compact=True,
            )
        return True

    def icon(self, glyph: str, x: float, y: float, size: float, color: str) -> None:
        """Center a terminal icon within its logical square and current clip."""
        if not all(isfinite(value) for value in (x, y, size)) or size < 0:
            raise ValueError("Icon coordinates must be finite and size nonnegative")
        if size == 0:
            return
        box = self._box(Rect(x, y, size, size))
        width, _ = self.measure(glyph, size)
        columns = int(width / self.cell_width)
        if box[2] - box[0] < columns or box[3] <= box[1]:
            return
        column = ceil((x + size / 2) / self.cell_width) - 1 - columns // 2
        row = ceil((y + size / 2) / self.cell_height) - 1
        column -= self._origin_col
        row -= self._origin_row
        column = max(box[0], min(box[2] - columns, column))
        row = max(box[1], min(box[3] - 1, row))
        previous = self._clip
        self._clip = self._bounds(box)
        try:
            self.text(
                glyph, (column + self._origin_col) * self.cell_width,
                (row + self._origin_row) * self.cell_height, color, size,
            )
        finally:
            self._clip = previous

    def measure(
        self, text: str, size: float, mono: bool = False
    ) -> tuple[float, float]:
        column = maximum = 0
        lines = 1
        for cluster, width in text_cells(text):
            if cluster == "\n":
                maximum = max(maximum, column)
                column = 0
                lines += 1
            elif cluster == "\t":
                column += 4 - column % 4
            else:
                column += width
        return max(maximum, column) * self.cell_width, lines * self.cell_height

    def text(
        self, text: str, x: float, y: float, color: str, size: float, mono: bool = False
    ) -> None:
        if not isfinite(x) or not isfinite(y):
            raise ValueError("Text coordinates must be finite")
        # Text uses the same nearest-boundary rule as clip/background cells;
        # floor here would discard a label's first glyph or its entire row
        # whenever its non-cell-aligned control starts beyond a half cell.
        logical_column, logical_row = x / self.cell_width, y / self.cell_height
        if not isfinite(logical_column) or not isfinite(logical_row):
            raise ValueError("Text exceeds its coordinate budget")
        column = ceil(logical_column - 0.5) - self._origin_col
        row = ceil(logical_row - 0.5) - self._origin_row
        start = column
        ink = _color(color)
        for cluster, width in text_cells(text):
            if cluster == "\n":
                column = start
                row += 1
            elif cluster == "\t":
                for _ in range(4 - (column - start) % 4):
                    self._put(column, row, " ", 1, ink, text_ink=True)
                    column += 1
            else:
                self._put(column, row, cluster, width, ink, text_ink=True)
                column += width
            if row >= self._clip[3]:
                break

    def _stroke(
        self, column: int, row: int, glyph: str, color: RGBA, *, merge: bool = True
    ) -> None:
        left, top, right, bottom = self._clip
        if not (left <= column < right and top <= row < bottom):
            return
        # A thin pixel stroke would not erase an entire glyph. Keep the text
        # layer (including literal box-drawing characters and wide pairs) when
        # its cell is also needed by a border, rule, or decoration. Opaque fills
        # still erase it, so foreground panels/popups retain normal occlusion.
        old = self.cells[row * self.columns + column]
        if self._ink[row * self.columns + column] == 1 or old.underline:
            return
        owner = self.cells[row * self.columns + column - 1] if old.width == 0 else old
        if color[3] < 128 and owner.text.strip() and owner.text not in _BOX_MASKS:
            return
        if merge and old.text in _BOX_MASKS and glyph in _BOX_MASKS:
            glyph = _MASK_BOX.get(_BOX_MASKS[old.text] | _BOX_MASKS[glyph], glyph)
        self._put(column, row, glyph, 1, color)

    def focus_ring(
        self, rect: Rect, color: str, radius: float = 0, *, monochrome=False
    ) -> None:
        box = self._box(rect)
        self._border(box, _color(color), radius=radius, compact=True)
        if monochrome:
            # Color-only outline changes disappear in NO_COLOR. Underline the
            # original perimeter without replacing labels or splitting wide ink.
            left, top, right, bottom = self._bounds(box)
            x1, y1, x2, y2 = box[0], box[1], box[2] - 1, box[3] - 1
            if x2 < x1 or y2 < y1:
                return
            for column in range(left, right):
                self._underline(column, y1)
                if y2 != y1:
                    self._underline(column, y2)
            for row in range(max(top, y1 + 1), min(bottom, y2)):
                self._underline(x1, row)
                if x2 != x1:
                    self._underline(x2, row)

    def caret(self, x: float, y: float, height: float, color: str) -> None:
        """Underline the insertion cell without replacing its text or wide pair."""
        if not all(isfinite(value) for value in (x, y, height)):
            raise ValueError("Caret geometry must be finite")
        if height <= 0:
            return
        column = ceil(x / self.cell_width - 0.5) - self._origin_col
        row = ceil(y / self.cell_height - 0.5) - self._origin_row
        self._underline(column, row, _color(color))

    def _underline(self, column: int, row: int, color: RGBA | None = None) -> None:
        """Annotate one whole visible glyph; blank carets may provide a color."""
        left, top, right, bottom = self._clip
        if not (left <= column < right and top <= row < bottom):
            return
        index = row * self.columns + column
        if self.cells[index].width == 0:
            column -= 1
            index -= 1
        old = self.cells[index]
        if column < left or column + old.width > right:
            return
        foreground = (
            _over(color, old.background)
            if color is not None and old.text == " " else old.foreground
        )
        for offset in range(index, index + old.width):
            cell = self.cells[offset]
            self.cells[offset] = Cell(
                cell.text, foreground, cell.background, cell.width, True
            )

    def line(
        self, x1: float, y1: float, x2: float, y2: float, color: str, width: float = 1
    ) -> None:
        if not all(isfinite(value) for value in (x1, y1, x2, y2, width)):
            raise ValueError("Line geometry must be finite")
        if width <= 0:
            return
        left, top, right, bottom = self._clip
        if left >= right or top >= bottom:
            return
        a, b = x1 / self.cell_width, y1 / self.cell_height
        c, d = x2 / self.cell_width, y2 / self.cell_height
        a, c = a - self._origin_col, c - self._origin_col
        b, d = b - self._origin_row, d - self._origin_row
        dx, dy = c - a, d - b
        if not all(isfinite(value) for value in (a, b, c, d, dx, dy)):
            raise ValueError("Line geometry exceeds its coordinate budget")
        low, high = 0.0, 1.0
        # Liang-Barsky clips before stepping: huge offscreen lines are bounded
        # by the visible grid, never their original coordinate magnitude.
        for p, q in (
            (-dx, a - left),
            (dx, right - 1e-9 - a),
            (-dy, b - top),
            (dy, bottom - 1e-9 - b),
        ):
            if p == 0:
                if q < 0:
                    return
            elif p < 0:
                low = max(low, q / p)
            else:
                high = min(high, q / p)
        if low > high:
            return
        x = max(left, min(right - 1, floor(a + low * dx)))
        y = max(top, min(bottom - 1, floor(b + low * dy)))
        end_x = max(left, min(right - 1, floor(a + high * dx)))
        end_y = max(top, min(bottom - 1, floor(b + high * dy)))
        delta_x, delta_y = abs(end_x - x), abs(end_y - y)
        step_x, step_y = (1 if x < end_x else -1), (1 if y < end_y else -1)
        error = delta_x - delta_y
        ink = _color(color)
        # A shallow segment may occupy only one row (or column) after cell
        # projection. Its original pixel slope must not turn a flat trace
        # into disconnected diagonal glyphs.
        if dy == 0 or (dx != 0 and delta_y == 0):
            glyph = "─"
        elif dx == 0 or delta_x == 0:
            glyph = "│"
        else:
            glyph = "╲" if (dx > 0) == (dy > 0) else "╱"
        for _ in range(max(delta_x, delta_y) + 1):
            self._stroke(x, y, glyph, ink)
            if (x, y) == (end_x, end_y):
                break
            twice = error * 2
            if twice > -delta_y:
                error -= delta_y
                x += step_x
            if twice < delta_x:
                error += delta_x
                y += step_y

    def gradient_rect(
        self,
        rect: Rect,
        first: str,
        last: str,
        axis: str,
        radius: float,
        border_width: float = 0,
    ) -> bool:
        box = self._box(rect)
        left, top, right, bottom = self._bounds(box)
        a, b = _color(first), _color(last)
        if border_width:
            if border_width > 0:
                self._border(box, a, b, axis=axis, radius=radius)
            return True
        horizontal = axis == "horizontal"
        start = box[0] if horizontal else box[1]
        extent = (box[2] if horizontal else box[3]) - start
        colors = [
            _mix(a, b, (position - start) / max(1, extent - 1))
            for position in (range(left, right) if horizontal else range(top, bottom))
        ]
        for row in range(top, bottom):
            for column in range(left, right):
                color = colors[column - left if horizontal else row - top]
                if (
                    color[3] < 255
                    and column > left
                    and self.cells[row * self.columns + column].width == 0
                ):
                    continue
                self._fill(column, row, color)
        return True

    def image(
        self, source: str, rect: Rect, *, tint: str = "#ffffff", fit: str = "stretch", _edges=None
    ) -> None:
        bounds = self._bounds(self._box(rect))
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            return
        color = _color(tint)
        if not color[3]:
            return
        image = self._images.get(source)
        if image is None:
            self.image_placeholder(rect, tint=tint)
            return
        for column, row, upper, lower in image_cells(
            image,
            Rect(
                rect.x - self._origin_col * self.cell_width,
                rect.y - self._origin_row * self.cell_height,
                rect.width, rect.height,
            ),
            bounds,
            self.cell_width,
            self.cell_height,
            color,
            _edges,
            fit=fit,
        ):
            if not (upper[3] or lower[3]):
                continue
            index = row * self.columns + column
            previous = self.cells[index]
            foreground = _over(
                upper,
                previous.foreground if self._ink[index] == 2 else previous.background,
            )
            background = _over(lower, previous.background)
            if previous.width != 1:
                self._erase(column, row)
            glyph = luminance_glyph(foreground, background) if self.monochrome else "▀"
            self.cells[index] = Cell(glyph, foreground, background)
            self._ink[index] = 2

    def image_placeholder(self, rect: Rect, *, tint: str = "#ffffff") -> None:
        """Keep failed resources visible while the host reports their errors."""
        previous = self._clip
        self._clip = self._bounds(self._box(rect))
        try:
            self.text("[image]", rect.x, rect.y, tint, self.cell_height)
        finally:
            self._clip = previous

    def image_nine(
        self, source: str, rect: Rect, edges, *, tint: str = "#ffffff"
    ) -> None:
        self.image(source, rect, tint=tint, _edges=edges)

    def _surface_bounds(self, rect: Rect):
        left, top, right, bottom = self._box(rect)
        left, top = max(0, min(self.columns, left)), max(0, min(self.rows, top))
        right, bottom = max(left, min(self.columns, right)), max(top, min(self.rows, bottom))
        # An opaque fill or translucent tint can affect both halves of a glyph
        # even when its lead or continuation lies just outside the paint clip.
        col, end = max(0, left - 1), min(self.columns, right + 1)
        if left == right or top == bottom:
            return left, top, 0, 0, (0, 0, 0, 0)
        return col, top, end - col, bottom - top, (left - col, 0, right - col, bottom - top)

    def surface_byte_size(self, rect: Rect) -> int:
        _, _, columns, rows, _ = self._surface_bounds(rect)
        # Conservative Python storage estimate for input/output Cells, bounded
        # 65-codepoint text, RGB tuples, list references and both ink arrays.
        # Empty projections still cache a no-op: densely packed controls may
        # have a logical clip without owning a terminal cell at this resolution.
        return 512 + columns * rows * 1280

    def surface_create(self, rect: Rect) -> object:
        if self._target is not None:
            raise RuntimeError("Cannot create a text surface while another is active")
        if len(self._surfaces) >= 8192:
            raise MemoryError("Too many text render surfaces")
        col, row, columns, rows, clip = self._surface_bounds(rect)
        surface = _CellSurface(
            col, row, columns, rows, clip, self.resource_revision,
            [], bytearray(), [], bytearray(),
        )
        self._surfaces.add(surface)
        return surface

    def _check_surface(self, surface: object) -> _CellSurface:
        if not isinstance(surface, _CellSurface) or surface not in self._surfaces or surface.released:
            raise ValueError("Unknown or released text render surface")
        return surface

    def surface_begin(self, surface: object) -> None:
        target = self._check_surface(surface)
        if self._target is not None:
            raise RuntimeError("Nested render surfaces are unsupported")
        if target.revision != self.resource_revision:
            raise ValueError("Text render surface belongs to an earlier framebuffer")
        # Cell operations depend on existing text, ink and colour. Recording
        # over the exact backdrop preserves the ordinary painter's composition.
        target.backdrop = []
        target.backdrop_ink = bytearray()
        for row in range(target.row, target.row + target.rows):
            start = row * self.columns + target.col
            target.backdrop.extend(self.cells[start:start + target.columns])
            target.backdrop_ink.extend(self._ink[start:start + target.columns])
        target.cells = target.backdrop.copy()
        target.ink = target.backdrop_ink.copy()
        self._saved_surface = (
            self.cells, self._ink, self.columns, self.rows, self._clip,
            self._origin_col, self._origin_row,
        )
        self._target = target
        self.cells = target.cells
        self._ink = target.ink
        self.columns = target.columns
        self.rows = target.rows
        self._origin_col = target.col
        self._origin_row = target.row
        self.clip(None)

    def surface_end(self) -> None:
        if self._target is None or self._saved_surface is None:
            raise RuntimeError("No text surface is active")
        # The active buffer is the surface's own list; keep those cells.
        self._target.cells = self.cells
        self._target.ink = self._ink
        (
            self.cells, self._ink, self.columns, self.rows, self._clip,
            self._origin_col, self._origin_row,
        ) = self._saved_surface
        self._target = None
        self._saved_surface = None

    def surface_matches(self, surface: object) -> bool:
        """Whether a cached result can still be used over the current backdrop."""
        target = self._check_surface(surface)
        if self._target is not None or target.revision != self.resource_revision:
            return False
        if len(target.backdrop) != target.columns * target.rows:
            return False
        for row in range(target.rows):
            start = (target.row + row) * self.columns + target.col
            source = row * target.columns
            end = source + target.columns
            if (self.cells[start:start + target.columns] != target.backdrop[source:end]
                    or self._ink[start:start + target.columns] != target.backdrop_ink[source:end]):
                return False
        return True

    def surface_blit(self, surface: object) -> None:
        target = self._check_surface(surface)
        if self._target is not None:
            raise RuntimeError("Cannot blit while recording a text surface")
        if target.revision != self.resource_revision:
            raise ValueError("Text render surface belongs to an earlier framebuffer")
        left, top, right, bottom = self._clip
        first, last = max(left, target.col), min(right, target.col + target.columns)
        if first >= last:
            return
        for row in range(max(top, target.row), min(bottom, target.row + target.rows)):
            source = (row - target.row) * target.columns + first - target.col
            index = row * self.columns + first
            count = last - first
            self.cells[index:index + count] = target.cells[source:source + count]
            self._ink[index:index + count] = target.ink[source:source + count]

    def surface_release(self, surface: object) -> None:
        target = self._check_surface(surface)
        if self._target is target:
            raise RuntimeError("Cannot release an active render surface")
        self._surfaces.remove(target)
        target.released = True
        target.cells = []
        target.ink = bytearray()
        target.backdrop = []
        target.backdrop_ink = bytearray()
