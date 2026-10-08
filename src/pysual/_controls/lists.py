"""Virtual fixed-row lists and selection navigation."""

from __future__ import annotations

from time import monotonic
from math import ceil
from typing import ClassVar
from .._text_display import single_line
from ..controls import Control
from ..events import ChangeEvent, Event
from ..geometry import Rect
from ..schema import Dirty, prop
from .._scrolling import (
    can_scroll, clamp_scroll, typeahead_prefix,
    scrollbar, handle_scrollbars, paint_scrollbars,
)


class ListView(Control):
    cache_paint: bool = prop(default=True)
    items: tuple[str, ...] = prop(default=(), affects=Dirty.PAINT)
    selected_index: int = prop(default=-1, minimum=-1, changed="changed")
    row_height: float = prop(default=34.0, minimum=1, affects=Dirty.MEASURE)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[int]]] = Event(ChangeEvent)
    _style_kind: ClassVar[str] = "ListView"
    style_parts: ClassVar[tuple[str, ...]] = ("body", "row", "track", "thumb")
    _contained_style_parts: ClassVar[frozenset[str]] = frozenset(("row", "track", "thumb"))

    def _initialize(self):
        super()._initialize()
        self._scroll = 0
        self._wheel = 0.0
        self._scroll_drag = None
        self._prefix, self._typed_at = "", 0.0

    def measure(self, host):
        return 220, 220

    def _row_height(self):
        return self.effective_row_height(self.row_height)

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "selected_index" and value >= len(self.items):
            raise ValueError("Selected index is outside items")

    def _changed(self, field, old, value):
        if field.name == "items":
            self._clamp_scroll()
            self._wheel = 0.0
            self._scroll_drag = None
            self._prefix = ""
            if self.selected_index >= len(value):
                self.selected_index = -1
        super()._changed(field, old, value)

    def _clamp_scroll(self):
        self._scroll = clamp_scroll(self._scroll, len(self.items), self._visible_rows())

    def _visible_rows(self):
        return max(1, int(self._rect.height / self._row_height()))

    def _scrollbar(self):
        visible = self._visible_rows()
        if (
            len(self.items) <= visible
            or self._rect.width <= 0
            or self._rect.height <= 0
        ):
            return None
        track = Rect(
            max(self._rect.x, self._rect.right - 12),
            self._rect.y,
            min(12, self._rect.width),
            self._rect.height,
        )
        bar = scrollbar(track, len(self.items), visible, self._scroll)
        return (bar.track, bar.thumb) if bar is not None else None

    def _scrollbars(self):
        boxes = self._scrollbar()
        if boxes is None:
            return []
        bar = scrollbar(boxes[0], len(self.items), self._visible_rows(), self._scroll)
        return [bar] if bar is not None else []

    def _row_at(self, x, y):
        if not self._rect.contains(x, y) or not self._clip.contains(x, y):
            return -1
        scrollbar = self._scrollbar()
        if scrollbar is not None and scrollbar[0].contains(x, y):
            return -1
        index = self._scroll + int((y - self._rect.y) / self._row_height())
        return index if index < len(self.items) else -1

    def index_at(self, x: float, y: float) -> int:
        """Return the visible item at app coordinates, or -1 outside item rows."""
        self._check_live()
        return self._row_at(x, y)

    def insertion_index(self, x: float, y: float) -> int | None:
        """Return a drop slot before/after a row; outside/scrollbar returns None."""
        self._check_live()
        if not self._rect.contains(x, y) or not self._clip.contains(x, y):
            return None
        scrollbar = self._scrollbar()
        if scrollbar is not None and scrollbar[0].contains(x, y):
            return None
        row = (y - self._rect.y) / self._row_height()
        return min(len(self.items), self._scroll + int(row + 0.5))

    def reveal(self, index: int) -> None:
        """Scroll a row into view without changing the current selection."""
        self._check_live()
        if type(index) is not int:
            raise TypeError("Row index must be an integer")
        if not 0 <= index < len(self.items):
            raise ValueError("Row index is outside items")
        previous = self._scroll
        self._scroll = max(0, min(self._scroll, index))
        if index >= self._scroll + self._visible_rows():
            self._scroll = index - self._visible_rows() + 1
        self._clamp_scroll()
        if self._scroll != previous:
            self.invalidate()

    def reveal_selection(self) -> None:
        """Scroll the selected row into view; an empty selection does nothing."""
        self._check_live()
        if self.selected_index >= 0:
            self.reveal(self.selected_index)

    def _typeahead(self, key):
        now = monotonic()
        self._prefix, prefix = typeahead_prefix(self._prefix, self._typed_at, key, now)
        self._typed_at = now
        start = self.selected_index + (0 if len(prefix) > 1 else 1)
        for offset in range(len(self.items)):
            index = (start + offset) % len(self.items)
            if self.items[index].casefold().startswith(prefix):
                self.selected_index = index
                self.reveal_selection()
                break

    def scroll_input(self, e, /) -> bool:
        self._clamp_scroll()
        if e.shift or not can_scroll(
            self._scroll, max(0, len(self.items) - self._visible_rows()), e.delta
        ):
            self._wheel = 0.0
            return False
        self.handle_input(e)
        return True

    def handle_input(self, e, /):
        self._clamp_scroll()
        previous_scroll = self._scroll
        if e.kind == "focus" and self._should_reveal_focus():
            self.reveal_selection()
        handled, self._scroll_drag, values = handle_scrollbars(
            e, self._scrollbars(), self._scroll_drag)
        if 1 in values:
            self._scroll = round(values[1])
            self._clamp_scroll()
        if handled:
            if self._scroll != previous_scroll:
                self.invalidate()
            return
        if e.kind in ("blur", "pointer_cancel"):
            self._scroll_drag = None
            self._prefix = ""
        elif e.kind == "wheel":
            self._wheel += e.delta * 3
            rows = int(round(self._wheel, 12))
            self._wheel -= rows
            self._scroll += rows
            self._clamp_scroll()
        elif e.kind == "pointer_down" and e.button == 1:
            i = self._row_at(e.x, e.y)
            if 0 <= i < len(self.items):
                self.selected_index = i
        elif (
            e.kind == "key_down"
            and e.key in ("ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown")
            and self.items
        ):
            self.selected_index = (
                0
                if e.key == "Home"
                else len(self.items) - 1
                if e.key == "End"
                else max(
                    0,
                    min(
                        len(self.items) - 1,
                        self.selected_index
                        + (-1 if e.key in ("ArrowUp", "PageUp") else 1)
                        * (
                            self._visible_rows()
                            if e.key in ("PageUp", "PageDown")
                            else 1
                        ),
                    ),
                )
            )
            self._prefix = ""
            self.reveal_selection()
        elif e.kind == "key_down" and len(e.key) == 1 and not e.ctrl and self.items:
            self._typeahead(e.key)
        if self._scroll != previous_scroll:
            self.invalidate()

    def paint(self, p, /):
        s = p.body()
        with p._clipped(Rect(0, 0, p.width, p.height)):
            self._paint_items(p, s)

    def _paint_items(self, p, s):
        row_height = self._row_height()
        self._clamp_scroll()
        scrollbar = self._scrollbar()
        row_width = max(0, p.width - (scrollbar[0].width if scrollbar else 0))
        self._painted_rows = 0
        first = self._scroll + int(
            max(0, self._clip.y - self._rect.y) / row_height
        )
        end = min(
            len(self.items),
            self._scroll
            + ceil(max(0, self._clip.bottom - self._rect.y) / row_height),
        )
        with p._clipped(Rect(0, 0, row_width, p.height)):
            for i in range(first, end):
                y = (i - self._scroll) * row_height
                row_style = p.style("row", selected=i == self.selected_index)
                text_height = self.effective_row_height(row_style.font_size * 1.2)
                p.surface(Rect(1, y, max(0, row_width - 2), row_height), row_style)
                p.text(
                    p.elide(single_line(self.items[i]), max(0, row_width - 20),
                            size=row_style.font_size, font_family=row_style.font_family),
                    10,
                    y + (row_height - text_height) / 2,
                    color=row_style.foreground,
                    size=row_style.font_size,
                    font_family=row_style.font_family,
                )
                self._painted_rows += 1
        paint_scrollbars(p, self._scrollbars())


# Keep public imports, diagnostics, and reflection stable across source moves.
ListView.__module__ = "pysual.widgets"
