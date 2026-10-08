"""Scrolling container geometry and input."""

from __future__ import annotations

from typing import ClassVar
from ..controls import Container
from ..geometry import Rect
from ..schema import Dirty, prop
from .._scrolling import can_scroll, scrollbar, handle_scrollbars, paint_scrollbars


class ScrollArea(Container):
    scroll_x: float = prop(default=0.0, minimum=0, affects=Dirty.ARRANGE)
    scroll_y: float = prop(default=0.0, minimum=0, affects=Dirty.ARRANGE)
    _style_kind: ClassVar[str] = "ScrollArea"
    style_parts: ClassVar[tuple[str, ...]] = ("body", "track", "thumb")
    _contained_style_parts: ClassVar[frozenset[str]] = frozenset(("track", "thumb"))

    def _initialize(self):
        super()._initialize()
        self._scroll_limits = (0.0, 0.0)
        self._scroll_axes = (False, False)
        self._scroll_drag = None

    def _paint_layout_key(self):
        # Descendant layout determines scrollbar visibility and thumb size.
        return self._scroll_limits, self._scroll_axes

    def content_bounds(self, bounds):
        area = super().content_bounds(bounds)
        horizontal, vertical = self._scroll_axes
        return Rect(area.x, area.y, max(0, area.width - 12 * vertical),
                    max(0, area.height - 12 * horizontal))

    def arrange_children(self, area, measure):
        base = super().content_bounds(self._rect)
        axes = (False, False)
        boxes = {}
        for _ in range(3):
            self._scroll_axes = axes
            area = self.content_bounds(self._rect)
            boxes = self._arrange_declared(area, measure)
            right = max((box.right for box in boxes.values()), default=area.right)
            bottom = max((box.bottom for box in boxes.values()), default=area.bottom)
            self._scroll_limits = max(0, right - area.right), max(0, bottom - area.bottom)
            updated = (axes[0] or self._scroll_limits[0] > 0,
                       axes[1] or self._scroll_limits[1] > 0)
            if updated == axes or base.width <= 0 or base.height <= 0:
                break
            axes = updated
        self.scroll_x = min(self.scroll_x, self._scroll_limits[0])
        self.scroll_y = min(self.scroll_y, self._scroll_limits[1])
        return boxes

    def _scrollbars(self):
        area = self.content_bounds(self._rect)
        base = super().content_bounds(self._rect)
        bars = []
        for axis, limit in enumerate(self._scroll_limits):
            if limit <= 0:
                continue
            extent = area.width if axis == 0 else area.height
            if extent <= 0:
                continue
            track = (Rect(area.x, area.bottom, area.width, max(0, base.bottom - area.bottom))
                     if axis == 0 else Rect(area.right, area.y, max(0, base.right - area.right), area.height))
            bar = scrollbar(track, extent + limit, extent,
                            self.scroll_x if axis == 0 else self.scroll_y, axis)
            if bar is not None:
                bars.append(bar)
        return bars

    def paint(self, p, /):
        super().paint(p)
        paint_scrollbars(p, self._scrollbars())

    def _reveal_bounds(self, bounds: Rect) -> Rect:
        """Minimally expose descendant bounds in this area's content viewport."""
        area = self.content_bounds(self._rect)

        def offset(start, end, low, high):
            if high <= low or (start <= low and end >= high):
                return 0
            if start < low:
                return max(start - low, end - high)
            if end > high:
                return min(start - low, end - high)
            return 0

        before_x, before_y = self.scroll_x, self.scroll_y
        self.scroll_x = max(
            0,
            min(
                self._scroll_limits[0],
                before_x + offset(bounds.x, bounds.right, area.x, area.right),
            ),
        )
        self.scroll_y = max(
            0,
            min(
                self._scroll_limits[1],
                before_y + offset(bounds.y, bounds.bottom, area.y, area.bottom),
            ),
        )
        return Rect(
            bounds.x - (self.scroll_x - before_x),
            bounds.y - (self.scroll_y - before_y),
            bounds.width,
            bounds.height,
        )

    def scroll_input(self, e, /) -> bool:
        axis = 0 if e.shift else 1
        offset = self.scroll_x if e.shift else self.scroll_y
        if not can_scroll(offset, self._scroll_limits[axis], e.delta):
            return False
        self.handle_input(e)
        return True

    def handle_input(self, e, /):
        handled, self._scroll_drag, values = handle_scrollbars(
            e, self._scrollbars(), self._scroll_drag)
        for axis, value in values.items():
            setattr(self, "scroll_x" if axis == 0 else "scroll_y", value)
        if handled:
            return
        if e.kind == "wheel":
            if e.shift:
                self.scroll_x = max(
                    0, min(self._scroll_limits[0], self.scroll_x + e.delta * 32)
                )
                return
            self.scroll_y = max(
                0, min(self._scroll_limits[1], self.scroll_y + e.delta * 32)
            )


# Keep public imports, diagnostics, and reflection stable across source moves.
ScrollArea.__module__ = "pysual.widgets"
