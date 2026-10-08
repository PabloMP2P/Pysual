"""Bounded value controls and progress presentation."""

from __future__ import annotations

from math import ceil, isfinite
from typing import ClassVar
from .._ranges import NumericRange
from ..behaviors import PressBehavior
from ..controls import Control
from ..events import ChangeEvent, Event
from ..geometry import Rect
from ..painting import resolve_style
from ..schema import Dirty, prop


class Slider(Control):
    """A bounded value with primary-pointer dragging and keyboard stepping.

    Arrow keys move one step, PageUp/PageDown move ten steps, and Home/End
    select the bounds. Canceling a gesture retains its last committed value.
    """

    cache_paint: bool = prop(default=True)
    value: float = prop(default=0.0, changed="changed")
    minimum: float = prop(default=0.0)
    maximum: float = prop(default=100.0)
    step: float = prop(default=1.0, minimum=0.000001)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[float]]] = Event(ChangeEvent)
    _style_kind: ClassVar[str] = "Slider"
    style_parts: ClassVar[tuple[str, ...]] = ("body", "track", "fill", "thumb")

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior(keys=())
        self._drag_pointer: int | None = None
        self._drag_update = False

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        data = {**self._values, name: value}
        bounds = NumericRange(data["minimum"], data["maximum"])
        if (not bounds.contains(data["value"]) or not bounds.is_ordered(strict=True)
                or not isfinite(data["maximum"] - data["minimum"])):
            raise ValueError(
                "Slider requires minimum <= value <= maximum and a finite positive range"
            )

    def _clear_interaction(self):
        self._drag_pointer = None
        self._press.reset(self)

    def _changed(self, field, old, value):
        if (field.name in ("minimum", "maximum", "step", "enabled", "visible")
                or field.name == "value" and not self._drag_update):
            self._clear_interaction()
        super()._changed(field, old, value)

    def measure(self, host):
        return 180, self._control_height(host)

    def set_range(self, minimum: float, maximum: float, *, value: float | None = None) -> None:
        """Commit bounds and value together; omitted value clamps the current value."""
        self._check_live()
        for name, item in (("minimum", minimum), ("maximum", maximum)):
            self._schema[name].validate(item)
        bounds = NumericRange(minimum, maximum)
        if not bounds.is_ordered(strict=True):
            raise ValueError("Slider requires minimum < maximum")
        value = bounds.clamp(self.value) if value is None else value
        self._schema["value"].validate(value)
        if not bounds.contains(value):
            raise ValueError("Slider requires minimum <= value <= maximum")
        self.update(minimum=minimum, maximum=maximum, value=value)

    def _track(self):
        inset = min(9, self._rect.width / 2, self._rect.height / 2)
        return inset, max(0, self._rect.width - 2 * inset)

    def paint(self, p, /):
        y = p.height / 2
        left, width = self._track()
        fraction = (self.value - self.minimum) / (self.maximum - self.minimum)
        track_height = min(6, p.height)
        p.surface(Rect(left, y - track_height / 2, width, track_height), p.style("track"))
        p.surface(Rect(left, y - track_height / 2, width * fraction, track_height),
                  p.style("fill"))
        center = left + width * fraction
        thumb = p.style("thumb")
        p.marker(
            Rect(center - left, y - left, left * 2, left * 2),
            thumb,
            shape="circle" if thumb.radius >= left else "square",
        )

    def _move_pointer(self, event):
        left, width = self._track()
        if width <= 0:
            return
        fraction = max(0, min(1, (event.x - self._rect.x - left) / width))
        candidate = self.minimum + fraction * (self.maximum - self.minimum)
        units = (candidate - self.minimum) / self.step
        if fraction == 1:
            candidate = self.maximum
        elif fraction == 0:
            candidate = self.minimum
        elif isfinite(units):
            candidate = self.minimum + round(units) * self.step
        self._drag_update = True
        try:
            self.value = max(self.minimum, min(self.maximum, candidate))
        finally:
            self._drag_update = False

    def handle_input(self, event, /):
        self._check_live()
        if event.kind == "blur":
            self._clear_interaction()
            return
        if event.kind == "pointer_cancel":
            if self._drag_pointer is None or event.pointer_id == self._drag_pointer:
                self._clear_interaction()
            return
        if not self.effective_enabled or not self.visible:
            self._clear_interaction()
            return
        if (event.kind.startswith("pointer") and self._drag_pointer is not None
                and event.pointer_id != self._drag_pointer):
            return
        if event.kind == "pointer_down":
            if (event.button != 1 or self._drag_pointer is not None
                    or not self._rect.contains(event.x, event.y) or self._track()[1] <= 0):
                return
            self._drag_pointer = event.pointer_id
            self._press.handle_input(self, event)
            self._move_pointer(event)
        elif event.kind in ("pointer_move", "pointer_up") and self._drag_pointer is not None:
            if event.kind == "pointer_up" and event.button != 1:
                return
            self._press.handle_input(self, event)
            self._move_pointer(event)
            if event.kind == "pointer_up":
                self._clear_interaction()
        elif event.kind == "key_down" and event.key in (
            "ArrowLeft", "ArrowRight", "ArrowDown", "ArrowUp",
            "PageDown", "PageUp", "Home", "End",
        ):
            self._clear_interaction()
            direction = 1 if event.key in ("ArrowRight", "ArrowUp", "PageUp") else -1
            candidate = (
                self.minimum if event.key == "Home" else
                self.maximum if event.key == "End" else
                self.value + direction * self.step * (10 if event.key.startswith("Page") else 1)
            )
            self.value = max(self.minimum, min(self.maximum, candidate))

    def destroy(self):
        self._clear_interaction()
        super().destroy()


class ProgressBar(Control):
    cache_paint: bool = prop(default=True)
    value: float = prop(default=0.0, minimum=0, changed="changed")
    maximum: float = prop(default=100.0, minimum=0.000001)
    show_text: bool = prop(default=True)
    changed: ClassVar[Event[ChangeEvent[float]]] = Event(ChangeEvent)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "track", "fill")

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        values = {**self._values, name: value}
        if not NumericRange(0, values["maximum"]).contains(values["value"]):
            raise ValueError("ProgressBar requires 0 <= value <= maximum")

    def set_range(self, maximum: float, *, value: float | None = None) -> None:
        """Set the zero-based range atomically, clamping the current value if omitted."""
        self._check_live()
        self._schema["maximum"].validate(maximum)
        bounds = NumericRange(0, maximum)
        candidate = bounds.clamp(self.value) if value is None else value
        self._schema["value"].validate(candidate)
        if not bounds.contains(candidate):
            raise ValueError("ProgressBar requires 0 <= value <= maximum")
        self.update(maximum=maximum, value=candidate)

    def measure(self, host):
        return 200, self._control_height(host)

    def paint(self, p, /):
        s = p.style()
        ratio = self.value / self.maximum
        track, fill = p.style("track"), p.style("fill")
        filled = p.width * ratio
        p.surface(Rect(0, 0, p.width, p.height), track)
        p.surface(Rect(0, 0, filled, p.height), fill)
        if self.show_text:
            text = f"{ratio:.0%}"
            width, height = p.measure(text, size=s.font_size, font_family=s.font_family)
            # A percentage can cross the fill boundary. Each portion uses the
            # corresponding surface's text color at the same glyph positions.
            for area, style in (
                (Rect(0, 0, filled, p.height), fill),
                (Rect(filled, 0, p.width - filled, p.height), track),
            ):
                if area.width > 0:
                    with p._clipped(area):
                        p.text(
                            text, (p.width - width) / 2, (p.height - height) / 2,
                            color=style.foreground, size=s.font_size,
                            font_family=s.font_family,
                        )


class Rating(Control):
    """An integer star rating; zero is unrated and read_only prevents user edits.

    Click and release the same star to select it. Arrow keys step the value;
    Home, Delete, and Backspace clear it, and End selects the maximum. Stars
    use intrinsic, font-scaled slots and clip when the available width shrinks.
    """

    cache_paint: bool = prop(default=True)
    value: int = prop(default=0, minimum=0, changed="changed")
    maximum: int = prop(default=5, minimum=1, affects=Dirty.MEASURE | Dirty.PAINT)
    read_only: bool = prop(default=False)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[int]]] = Event(ChangeEvent)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "star")

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior(keys=())
        self._preview: int | None = None
        self._preview_pointer: int | None = None

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        values = {**self._values, name: value}
        if values["value"] > values["maximum"]:
            raise ValueError("Rating requires 0 <= value <= maximum")

    def _set_preview(self, value):
        if self._preview != value:
            self._preview = value
            if not self._disposed:
                self.invalidate()

    def _clear_interaction(self):
        self._press.reset(self)
        self._preview_pointer = None
        self._set_preview(None)

    def _changed(self, field, old, value):
        if field.name in ("value", "maximum", "read_only", "enabled", "visible"):
            self._clear_interaction()
        super()._changed(field, old, value)

    def _star_pitch(self):
        return max(24, resolve_style(self, "star").font_size * 1.5)

    def _hit_star(self, event):
        if not self._rect.contains(event.x, event.y):
            return None
        star = int((event.x - self._rect.x) // self._star_pitch()) + 1
        return star if star <= self.maximum else None

    def measure(self, host):
        style = resolve_style(self, "star")
        height = host.measure("★", style.font_size, style.font_family == "mono")[1]
        return self._star_pitch() * self.maximum, max(self._control_height(host), height + 8)

    def handle_input(self, event, /):
        self._check_live()
        if event.kind == "blur":
            self._clear_interaction()
            return
        if event.kind == "pointer_cancel":
            if self._preview_pointer is None or event.pointer_id == self._preview_pointer:
                self._clear_interaction()
            return
        if self.read_only or not self.effective_enabled:
            self._clear_interaction()
            return
        if (event.kind.startswith("pointer") and self._preview_pointer is not None
                and event.pointer_id != self._preview_pointer):
            return

        star = self._hit_star(event)
        if event.kind == "pointer_down":
            if event.button != 1 or star is None:
                return
            self._preview_pointer = event.pointer_id
            self._set_preview(star)
        elif event.kind == "pointer_move":
            self._set_preview(star)
        elif event.kind == "pointer_leave":
            self._set_preview(None)

        activate = self._press.handle_input(self, event, part=star)
        if event.kind == "pointer_up" and event.button == 1:
            self._preview_pointer = None
            self._set_preview(None)
        if activate and star is not None:
            self.value = star
        elif event.kind == "key_down" and event.key in (
            "ArrowLeft", "ArrowDown", "ArrowRight", "ArrowUp", "Home", "End",
            "Delete", "Backspace",
        ):
            self._clear_interaction()
            self.value = (
                0 if event.key in ("Home", "Delete", "Backspace")
                else self.maximum if event.key == "End"
                else max(0, min(self.maximum, self.value + (
                    1 if event.key in ("ArrowRight", "ArrowUp") else -1
                )))
            )

    def paint(self, p, /):
        super().paint(p)
        visible = self._clip.intersect(self._rect)
        if visible.width <= 0 or visible.height <= 0:
            return
        shown = (
            self._preview if self._hover and self._preview is not None
            and not self.read_only and self.effective_enabled else self.value
        )
        pitch = self._star_pitch()
        # Only visible slots generate drawing work, even for a large maximum.
        first = max(0, int((visible.x - self._rect.x) // pitch))
        last = min(self.maximum, ceil((visible.right - self._rect.x) / pitch))
        for index in range(first, last):
            selected = index < shown
            style = p.style("star", selected=selected)
            glyph = "★" if selected else "☆"
            width, height = p.measure(glyph, size=style.font_size,
                                      font_family=style.font_family)
            p.text(glyph, index * pitch + (pitch - width) / 2,
                   (p.height - height) / 2, color=style.foreground,
                   size=style.font_size, font_family=style.font_family)

    def destroy(self):
        self._clear_interaction()
        super().destroy()


# Keep public imports, diagnostics, and reflection stable across source moves.
Slider.__module__ = "pysual.widgets"
ProgressBar.__module__ = "pysual.selection"
Rating.__module__ = "pysual.selection"
