"""Small choice and path controls, sharing part-aware press gestures."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from typing import ClassVar

from ._text_display import single_line
from .behaviors import PressBehavior
from .controls import Control
from .errors import LifecycleError
from .events import ChangeEvent, Event, UiEvent
from .geometry import Rect
from .painting import resolve_style
from .schema import Dirty, prop


def _caption(p, text, area, style):
    """Fit a caption within its own surface, including very narrow layouts."""
    if area.width <= 0 or area.height <= 0:
        return
    with p._clipped(area):
        text = p.elide(single_line(text), max(0, area.width - 12),
                       size=style.font_size, font_family=style.font_family)
        width, height = p.measure(text, size=style.font_size,
                                  font_family=style.font_family)
        p.text(text, area.x + (area.width - width) / 2,
               area.y + (area.height - height) / 2, color=style.foreground,
               size=style.font_size, font_family=style.font_family)


class _PartControl(Control):
    """One gesture owner; concrete controls retain selection and validation."""

    cache_paint: bool = prop(default=True)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior()
        self._hot_part = None
        self._armed_part = None

    def _clear_interaction(self):
        self._press.reset(self)
        self._hot_part = self._armed_part = None

    def _changed(self, field, old, value):
        # Model or geometry changes must never retarget an in-flight gesture.
        self._clear_interaction()
        super()._changed(field, old, value)

    def _handle_press(self, event, part):
        self._check_live()
        if not self.effective_enabled or event.kind == "blur":
            self._clear_interaction()
            return False
        if event.kind == "pointer_leave":
            if self._hot_part is not None:
                self._hot_part = None
                self.invalidate()
        elif event.kind in ("pointer_down", "pointer_move", "pointer_up"):
            if self._hot_part != part:
                self._hot_part = part
                self.invalidate()
        if event.kind in ("pointer_down", "key_down"):
            if part is None or (event.kind == "pointer_down" and event.button != 1):
                return False
        was_pressed = self._pressed
        activated = self._press.handle_input(self, event, part=part)
        if event.kind in ("pointer_down", "key_down") and self._pressed and not was_pressed:
            self._armed_part = part
        # Moving outside a part temporarily releases its visual state but does
        # not end the gesture. Keep its target so moving back restores styling.
        return activated and part is not None

    def _part_style(self, part, target, *, selected=False, enabled=True):
        state = (
            "disabled" if not enabled or not self.effective_enabled
            else "pressed" if target is not None and self._pressed and self._armed_part == target
            else "hover" if target is not None and self._hover and self._hot_part == target
            else "normal"
        )
        return resolve_style(self, part, selected=selected, state=state)

    def destroy(self):
        self._clear_interaction()
        super().destroy()


class SegmentedControl(_PartControl):
    """One choice in a joined row. Arrows/Home/End change selected_index.

    A selected_index of -1 means no selection. Segments share the available
    width, with a 32-pixel minimum; keyboard selection reveals clipped items.
    segment_width controls the preferred width, independently of layout.
    """

    items: tuple[str, ...] = prop(default=(), affects=Dirty.MEASURE | Dirty.PAINT)
    selected_index: int = prop(default=-1, minimum=-1, changed="changed")
    segment_width: float = prop(default=100.0, minimum=32, affects=Dirty.MEASURE | Dirty.PAINT)
    changed: ClassVar[Event[ChangeEvent[int]]] = Event(ChangeEvent)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("Button",)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "segment", "separator")

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        values = {**self._values, name: value}
        if values["selected_index"] >= len(values["items"]):
            raise ValueError("Selected index is outside items")

    @property
    def selected_item(self) -> str | None:
        return self.items[self.selected_index] if self.selected_index >= 0 else None

    def measure(self, host):
        return max(1, len(self.items)) * self.segment_width + 4, self._control_height(host)

    def _geometry(self):
        available = max(0, self._rect.width - 4)
        count = max(1, len(self.items))
        pitch = max(32, available / count)
        # Expanded slots exactly fill the row; dividing their fractional pitch
        # back into the width can otherwise lose one slot to floating rounding.
        capacity = count if available >= 32 * count else max(1, int(available / 32))
        start = max(0, self.selected_index - capacity + 1)
        return pitch, start

    def _hit_part(self, event):
        if not self._rect.contains(event.x, event.y):
            return None
        pitch, start = self._geometry()
        # The inset is a visual well, not a dead strip in the touch target.
        index = start + int(max(0, event.x - self._rect.x - 2) // pitch)
        return min(index, len(self.items) - 1) if self.items else None

    def handle_input(self, event, /):
        self._check_live()
        part = self._hit_part(event) if event.kind.startswith("pointer") else (
            max(0, self.selected_index) if self.items else None
        )
        if self._handle_press(event, part):
            self.selected_index = part
            if event.kind == "pointer_up":
                # Selection invalidates the model and cancels the gesture; the
                # pointer is still over this segment, so retain its hover cue.
                self._hot_part = part
        elif self.effective_enabled and event.kind == "key_down" and self.items:
            if event.key in ("ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End"):
                self._clear_interaction()
                self.selected_index = (
                    0 if event.key == "Home" else len(self.items) - 1 if event.key == "End"
                    else max(0, min(len(self.items) - 1, self.selected_index + (
                        1 if event.key in ("ArrowRight", "ArrowDown") else -1
                    )))
                )

    def paint(self, p, /):
        p.body()
        pitch, start = self._geometry()
        visible = self._clip.intersect(self._rect.inset(2))
        if visible.width <= 0 or visible.height <= 0:
            return
        first = start + max(0, int((visible.x - self._rect.x - 2) // pitch))
        last = min(len(self.items), start + ceil((visible.right - self._rect.x - 2) / pitch))
        for index in range(first, last):
            area = Rect(2 + (index - start) * pitch, 2, pitch, max(0, p.height - 4))
            style = self._part_style("segment", index, selected=index == self.selected_index)
            with p._clipped(area.intersect(Rect(2, 2, max(0, p.width - 4), max(0, p.height - 4)))):
                p.surface(area, style)
                _caption(p, self.items[index], area, style)
                if (area.height > 12 and index > first and index != self.selected_index
                        and index - 1 != self.selected_index):
                    separator = p.style("separator")
                    p.line(area.x, area.y + 6, area.x, area.bottom - 6,
                           separator.foreground)


@dataclass(frozen=True, kw_only=True)
class BreadcrumbEvent(UiEvent):
    """A request to navigate to an ancestor without rewriting the path."""

    index: int
    item: str


class Breadcrumb(_PartControl):
    """An immutable label path whose last item is the current location.

    Clicking an ancestor emits navigated; the application owns updating items.
    Left/Right/Home/End move keyboard focus, and Enter/Space activates it.
    Long paths collapse to a bounded set containing root, current, and the
    focused ancestor. Ellipses and the current location are not actions.
    """

    items: tuple[str, ...] = prop(default=(), affects=Dirty.MEASURE | Dirty.PAINT)
    max_item_width: float = prop(default=160.0, minimum=32, affects=Dirty.MEASURE | Dirty.PAINT)
    navigated: ClassVar[Event[BreadcrumbEvent]] = Event(BreadcrumbEvent)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("Hyperlink",)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "item", "separator")

    def _initialize(self):
        super()._initialize()
        self._focus_index = max(0, len(self.items) - 2)

    def _changed(self, field, old, value):
        if field.name == "items":
            self._focus_index = max(0, len(self.items) - 2)
        super()._changed(field, old, value)

    def _shown_indices(self):
        count = len(self.items)
        if count <= 5:
            return list(range(count))
        middle = max(1, min(count - 4, self._focus_index - 1))
        indices = [0, *range(middle, middle + 3), count - 1]
        shown = []
        previous = -1
        for index in indices:
            if previous >= 0 and index > previous + 1:
                shown.append(None)
            shown.append(index)
            previous = index
        return shown

    def _parts(self, host=None, *, natural=False):
        if host is None:
            host = getattr(self._root(), "_layout_measure_host", None)
        style = resolve_style(self, "item")
        shown = self._shown_indices()
        widths = []
        for index in shown:
            if index is None:
                width = 26
            elif host is None:
                width = self.max_item_width
            else:
                width = min(self.max_item_width, max(32, host.measure(
                    single_line(self.items[index]), style.font_size,
                    style.font_family == "mono",
                )[0] + 16))
            widths.append(width)
        separator = 16
        desired = sum(widths) + max(0, len(shown) - 1) * separator
        if not natural and desired > self._rect.width:
            ratio = max(0, self._rect.width) / max(1, desired)
            widths = [width * ratio for width in widths]
            separator *= ratio
        x = 0.0
        result = []
        for index, width in zip(shown, widths):
            result.append((index, Rect(x, 0, width, self._rect.height), separator))
            x += width + separator
        return result, desired

    def measure(self, host):
        return self._parts(host, natural=True)[1], self._control_height(host)

    def _hit_part(self, event):
        if not self._rect.contains(event.x, event.y):
            return None
        for index, area, _ in self._parts()[0]:
            if index is not None and index < len(self.items) - 1 and area.contains(
                event.x - self._rect.x, event.y - self._rect.y
            ):
                return index
        return None

    def activate(self, index: int) -> None:
        """Request an ancestor through navigated; requires a running App."""
        self._check_live()
        if type(index) is not int:
            raise TypeError("Breadcrumb index must be an integer")
        if not 0 <= index < len(self.items) - 1:
            raise ValueError("Breadcrumb index must identify an ancestor")
        if self._dispatcher is None or self._dispatcher._stopped:
            raise LifecycleError("Navigation requires a running App")
        if self.effective_enabled:
            self.navigated.emit(BreadcrumbEvent(
                source=self, origin=self._origin, index=index, item=self.items[index],
            ))

    def handle_input(self, event, /):
        self._check_live()
        part = self._hit_part(event) if event.kind.startswith("pointer") else (
            self._focus_index if len(self.items) > 1 else None
        )
        if self._handle_press(event, part):
            if self._focus_index != part:
                self._focus_index = part
                self.invalidate(Dirty.MEASURE | Dirty.PAINT)
            self.activate(part)
        elif self.effective_enabled and event.kind == "key_down" and len(self.items) > 1:
            if event.key in ("ArrowLeft", "ArrowRight", "Home", "End"):
                self._clear_interaction()
                self._focus_index = (
                    0 if event.key == "Home" else len(self.items) - 2 if event.key == "End"
                    else max(0, min(len(self.items) - 2, self._focus_index + (
                        1 if event.key == "ArrowRight" else -1
                    )))
                )
                self.invalidate(Dirty.MEASURE | Dirty.PAINT)

    def paint(self, p, /):
        p.body()
        parts, _ = self._parts(p.host)
        for position, (index, area, separator_width) in enumerate(parts):
            if area.width <= 0:
                continue
            current = index == len(self.items) - 1
            style = self._part_style("item", index, selected=current)
            with p._clipped(area):
                p.surface(area, style)
                _caption(p, "…" if index is None else self.items[index], area, style)
                if self.focused and index == self._focus_index and not current:
                    p.line(area.x + 5, area.bottom - 3, area.right - 5,
                           area.bottom - 3, style.foreground, 2)
            if position < len(parts) - 1:
                separator = p.style("separator")
                arrow_area = Rect(area.right, 0, separator_width, p.height)
                with p._clipped(arrow_area):
                    size = min(14, separator_width, p.height)
                    if size > 0:
                        p.icon("chevron_right", arrow_area.x + (separator_width - size) / 2,
                               (p.height - size) / 2, size=size, color=separator.foreground)


class Pagination(_PartControl):
    """One-based pages with a bounded window, first/last links, and arrows.

    Arrow keys step a page; Home/End select the first/last page. Shrinking
    page_count below page requires an atomic update(page_count=..., page=...).
    """

    page: int = prop(default=1, minimum=1, changed="changed")
    page_count: int = prop(default=1, minimum=1, affects=Dirty.MEASURE | Dirty.PAINT)
    visible_pages: int = prop(default=5, minimum=1, affects=Dirty.MEASURE | Dirty.PAINT)
    changed: ClassVar[Event[ChangeEvent[int]]] = Event(ChangeEvent)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("Button",)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "page", "arrow", "ellipsis")

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        values = {**self._values, name: value}
        if values["page"] > values["page_count"]:
            raise ValueError("Pagination requires 1 <= page <= page_count")
        if values["visible_pages"] > 15:
            raise ValueError("visible_pages must be between 1 and 15")

    def _parts(self):
        count = min(self.visible_pages, self.page_count)
        start = max(1, min(self.page - count // 2, self.page_count - count + 1))
        pages = list(range(start, start + count))
        if pages[0] > 1:
            pages = [1, *([None] if pages[0] > 2 else []), *pages]
        if pages[-1] < self.page_count:
            pages.extend([None] if pages[-1] < self.page_count - 1 else [])
            pages.append(self.page_count)
        result = ["previous", *pages, "next"]
        # A constrained layout keeps the selected page and both arrows visible.
        # Numbered neighbours replace distant boundary links before clipping.
        capacity = max(3, int((self._rect.width + 4) / self._pitch()))
        if self._rect.width > 0 and len(result) > capacity:
            count = min(self.page_count, capacity - 2)
            start = max(1, min(self.page - count // 2, self.page_count - count + 1))
            result = ["previous", *range(start, start + count), "next"]
        return result

    def _pitch(self):
        style = resolve_style(self, "page")
        return max(36, len(str(self.page_count)) * style.font_size * 0.7 + 16)

    def _display_pitch(self):
        return min(self._pitch(), max(4, (self._rect.width + 4) / 3))

    def measure(self, host):
        # Reserve the maximum window width so page changes do not shift layout.
        slots = min(self.page_count, self.visible_pages + 4) + 2
        return slots * self._pitch() - 4, self._control_height(host)

    def _enabled_part(self, part):
        return part is not None and not (
            (part == "previous" and self.page == 1)
            or (part == "next" and self.page == self.page_count)
        )

    def _hit_part(self, event):
        if not self._rect.contains(event.x, event.y):
            return None
        pitch = self._display_pitch()
        x = event.x - self._rect.x
        index = int(x // pitch)
        parts = self._parts()
        if index >= len(parts) or x - index * pitch >= pitch - 4:
            return None
        part = parts[index]
        return part if self._enabled_part(part) else None

    def handle_input(self, event, /):
        self._check_live()
        part = self._hit_part(event) if event.kind.startswith("pointer") else self.page
        if self._handle_press(event, part):
            self.page = self.page - 1 if part == "previous" else self.page + 1 if part == "next" else part
        elif self.effective_enabled and event.kind == "key_down":
            if event.key in ("ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End"):
                self._clear_interaction()
                self.page = (
                    1 if event.key == "Home" else self.page_count if event.key == "End"
                    else max(1, min(self.page_count, self.page + (
                        1 if event.key in ("ArrowRight", "ArrowDown") else -1
                    )))
                )

    def paint(self, p, /):
        p.body()
        pitch = self._display_pitch()
        for index, part in enumerate(self._parts()):
            area = Rect(index * pitch, 0, pitch - 4, p.height)
            if area.x >= p.width:
                break
            kind = "ellipsis" if part is None else "arrow" if isinstance(part, str) else "page"
            style = self._part_style(kind, part, selected=part == self.page,
                                     enabled=self._enabled_part(part))
            with p._clipped(area):
                if part is not None:
                    p.surface(area, style)
                if isinstance(part, str):
                    size = min(18, area.width, area.height)
                    if size > 0:
                        p.icon("chevron_left" if part == "previous" else "chevron_right",
                               area.x + (area.width - size) / 2, (area.height - size) / 2,
                               size=size, color=style.foreground)
                else:
                    _caption(p, "…" if part is None else str(part), area, style)
