"""Interval selection and compact progress using portable range primitives."""

from __future__ import annotations

from functools import lru_cache
from math import acos, ceil, cos, hypot, isfinite, pi, sin
from typing import ClassVar, Literal

from .._ranges import NumericRange
from ..behaviors import PressBehavior
from ..controls import Control
from ..events import ChangeEvent, Event
from ..geometry import Rect
from ..painting import resolve_style
from ..schema import Dirty, prop


@lru_cache(maxsize=128)
def _ring_points(radius, fraction):
    """Reuse local arc geometry with at most 1/8px chord error at UI sizes.

    The 192-segment ceiling keeps huge constrained controls bounded. Clamping
    the radius in the angle calculation also avoids acos(1) at huge scales.
    Positions and colors are deliberately absent from the key, so equal rings
    share geometry and a live progress update keeps its track tessellation.
    """
    angle = 2 * acos(1 - min(1, 0.125 / max(0.125, min(radius, 1_000_000))))
    steps = max(12 if fraction == 1 else 2, ceil(min(192, 2 * pi * fraction / angle)))
    return tuple((radius * cos(-pi / 2 + 2 * pi * fraction * i / steps),
                  radius * sin(-pi / 2 + 2 * pi * fraction * i / steps))
                 for i in range(steps + 1))


class RangeSlider(Control):
    """Select an ordered interval with independently draggable thumbs.

    ``value`` is one atomic (lower, upper) pair. Click the nearest thumb or
    track position to edit that end; thumbs stop at each other. Space/Enter
    switches the active thumb, arrows step it, PageUp/PageDown move ten steps,
    and Home/End move to its allowed extremes. Tab retains normal navigation.
    The visible end labels identify the values and can be customized.
    """

    cache_paint: bool = prop(default=True)
    value: tuple[float, float] = prop(default=(25.0, 75.0), changed="changed",
                                      affects=Dirty.MEASURE | Dirty.PAINT)
    minimum: float = prop(default=0.0)
    maximum: float = prop(default=100.0)
    step: float = prop(default=1.0, minimum=0.000001)
    read_only: bool = prop(default=False)
    active_thumb: Literal["lower", "upper"] = prop(default="lower")
    lower_label: str = prop(default="Lower", affects=Dirty.MEASURE | Dirty.PAINT)
    upper_label: str = prop(default="Upper", affects=Dirty.MEASURE | Dirty.PAINT)
    show_values: bool = prop(default=True, affects=Dirty.MEASURE | Dirty.PAINT)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[tuple[float, float]]]] = Event(ChangeEvent)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("Slider",)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "track", "fill", "thumb")

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior(keys=())
        self._drag_pointer: int | None = None
        self._drag_update = False

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        values = {**self._values, name: value}
        bounds = NumericRange(values["minimum"], values["maximum"])
        lower, upper = values["value"]
        if (not bounds.is_ordered(strict=True) or not bounds.contains(lower)
                or not bounds.contains(upper) or lower > upper
                or not isfinite(values["maximum"] - values["minimum"])):
            raise ValueError(
                "RangeSlider requires minimum <= lower <= upper <= maximum "
                "and a finite positive range"
            )

    def set_range(
        self, minimum: float, maximum: float, *,
        value: tuple[float, float] | None = None,
    ) -> None:
        """Commit bounds and interval together, clamping omitted endpoints."""
        self._check_live()
        self._schema["minimum"].validate(minimum)
        self._schema["maximum"].validate(maximum)
        bounds = NumericRange(minimum, maximum)
        if not bounds.is_ordered(strict=True):
            raise ValueError("RangeSlider requires minimum < maximum")
        candidate = (
            (bounds.clamp(self.value[0]), bounds.clamp(self.value[1]))
            if value is None else value
        )
        self.update(minimum=minimum, maximum=maximum, value=candidate)

    def _clear_interaction(self):
        self._drag_pointer = None
        self._press.reset(self)

    def _changed(self, field, old, value):
        if (field.name in ("minimum", "maximum", "step", "read_only", "enabled",
                           "visible", "active_thumb")
                or field.name == "value" and not self._drag_update):
            self._clear_interaction()
        super()._changed(field, old, value)

    def _label_height(self):
        return resolve_style(self).font_size * 1.2 + 6 if self.show_values else 0

    def measure(self, host):
        style = resolve_style(self)
        labels = self._labels()
        label_width = sum(host.measure(label, style.font_size,
                                       style.font_family == "mono")[0] for label in labels)
        return max(220, label_width + 24 if self.show_values else 0), (
            self._control_height(host) + self._label_height()
        )

    def _labels(self):
        return tuple(
            f"{label}: {value:g}" if label else f"{value:g}"
            for label, value in zip((self.lower_label, self.upper_label), self.value)
        )

    def _track(self):
        inset = min(9, self._rect.width / 2)
        label_height = min(self._label_height(), max(0, self._rect.height - 18))
        return inset, max(0, self._rect.width - 2 * inset), (
            label_height + (self._rect.height - label_height) / 2
        )

    def _fractions(self):
        span = self.maximum - self.minimum
        return tuple((value - self.minimum) / span for value in self.value)

    def _move_thumb(self, candidate):
        lower, upper = self.value
        self.value = (
            (max(self.minimum, min(upper, candidate)), upper)
            if self.active_thumb == "lower"
            else (lower, min(self.maximum, max(lower, candidate)))
        )

    def _move_pointer(self, event):
        left, width, _ = self._track()
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
            self._move_thumb(candidate)
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
        if self.read_only or not self.effective_enabled or not self.visible:
            self._clear_interaction()
            return
        if (event.kind.startswith("pointer") and self._drag_pointer is not None
                and event.pointer_id != self._drag_pointer):
            return
        if event.kind == "pointer_down":
            if (event.button != 1 or self._drag_pointer is not None
                    or not self._rect.contains(event.x, event.y)):
                return
            left, width, _ = self._track()
            if width <= 0:
                return
            low, high = (left + width * part for part in self._fractions())
            x = event.x - self._rect.x
            self.active_thumb = "lower" if x < (low + high) / 2 else "upper"
            self._drag_pointer = event.pointer_id
            self._press.handle_input(self, event, part=self.active_thumb)
            self._move_pointer(event)
        elif event.kind in ("pointer_move", "pointer_up") and self._drag_pointer is not None:
            if event.kind == "pointer_up" and event.button != 1:
                return
            self._press.handle_input(self, event, part=self.active_thumb)
            self._move_pointer(event)
            if event.kind == "pointer_up":
                self._clear_interaction()
        elif event.kind == "key_down":
            if event.key in ("Space", "Enter"):
                self.active_thumb = "upper" if self.active_thumb == "lower" else "lower"
            elif event.key in ("ArrowLeft", "ArrowRight", "ArrowDown", "ArrowUp",
                               "PageDown", "PageUp", "Home", "End"):
                self._clear_interaction()
                current = self.value[0 if self.active_thumb == "lower" else 1]
                direction = 1 if event.key in ("ArrowRight", "ArrowUp", "PageUp") else -1
                candidate = (
                    self.minimum if event.key == "Home" else
                    self.maximum if event.key == "End" else
                    current + direction * self.step * (10 if event.key.startswith("Page") else 1)
                )
                self._move_thumb(candidate)

    def paint(self, p, /):
        super().paint(p)
        left, width, y = self._track()
        low, high = (left + width * part for part in self._fractions())
        p.surface(Rect(left, y - 3, width, 6), p.style("track"))
        p.surface(Rect(low, y - 3, high - low, 6), p.style("fill"))
        # Draw the active thumb last so overlapping endpoints remain visible.
        order = ("upper", "lower") if self.active_thumb == "lower" else ("lower", "upper")
        for name in order:
            active = name == self.active_thumb and (self.focused or self._drag_pointer is not None)
            x = low if name == "lower" else high
            # A captured drag belongs to one end. Keep its sibling's raised
            # face intact instead of making both thumbs look pressed.
            style = (resolve_style(self, "thumb", state="normal")
                     if self._pressed and self.effective_enabled and name != self.active_thumb
                     else p.style("thumb", selected=active))
            circular = style.radius >= 9
            p.marker(Rect(x - 9, y - 9, 18, 18), style,
                     shape="circle" if circular else "square",
                     checked=active and circular)
            if active and not circular:
                # An active square thumb uses a small grip, not a checkbox tick.
                p.rect(Rect(x - 1, y - 4, 2, 8), style.foreground)
        if self.show_values and self._rect.height >= self._label_height() + 18:
            style = p.style()
            for index, label in enumerate(self._labels()):
                text = p.elide(label, max(0, p.width / 2 - 8),
                               size=style.font_size, font_family=style.font_family)
                text_width, _ = p.measure(text, size=style.font_size,
                                          font_family=style.font_family)
                p.text(text, 0 if index == 0 else p.width - text_width, 0,
                       color=style.foreground, size=style.font_size,
                       font_family=style.font_family)

    def destroy(self):
        self._clear_interaction()
        super().destroy()


class CircularProgress(Control):
    """A determinate, clockwise progress ring with an optional percentage."""

    cache_paint: bool = prop(default=True)
    value: float = prop(default=0.0, minimum=0, changed="changed")
    maximum: float = prop(default=100.0, minimum=0.000001)
    show_text: bool = prop(default=True)
    thickness: float = prop(default=6.0, minimum=0.5,
                            affects=Dirty.MEASURE | Dirty.PAINT)
    changed: ClassVar[Event[ChangeEvent[float]]] = Event(ChangeEvent)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("ProgressBar",)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "track", "fill")

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        values = {**self._values, name: value}
        if not NumericRange(0, values["maximum"]).contains(values["value"]):
            raise ValueError("CircularProgress requires 0 <= value <= maximum")

    def set_range(self, maximum: float, *, value: float | None = None) -> None:
        """Commit a maximum and value together, clamping an omitted value."""
        self._check_live()
        self._schema["maximum"].validate(maximum)
        candidate = NumericRange(0, maximum).clamp(self.value) if value is None else value
        self.update(maximum=maximum, value=candidate)

    def measure(self, host):
        style = resolve_style(self)
        width, height = host.measure("100%", style.font_size, style.font_family == "mono")
        diameter = max(72, width + self.thickness * 2 + 20,
                       height + self.thickness * 2 + 20)
        return diameter, diameter

    def paint(self, p, /):
        super().paint(p)
        diameter = max(0, min(p.width, p.height) - 2)
        if diameter <= 0:
            return
        thickness = min(self.thickness, diameter / 2)
        radius = (diameter - thickness) / 2
        cx, cy = p.width / 2, p.height / 2
        ratio = self.value / self.maximum

        def ring(fraction, style):
            if fraction <= 0:
                return
            # Chord-error tessellation gives smooth curves with far fewer
            # strokes/joints than a fixed two-pixel sampling interval.
            points = tuple((cx + x, cy + y) for x, y in _ring_points(radius, fraction))
            color = style.fill or style.foreground
            p.lines(points, color, thickness)
            # Painter.lines may expand to independent butt-ended strokes.
            # Circular joints cover their outer wedges and antialias seams,
            # including the closing joint or the two open progress caps.
            # Keep the same bounded point set on every host; rounded borders
            # alone become rectangular boxes on character-cell renderers.
            half = thickness / 2
            for x, y in points[:-1] if fraction == 1 else points:
                p.rect(Rect(x - half, y - half, thickness, thickness),
                       color, radius=half)

        ring(1, p.style("track"))
        ring(ratio, p.style("fill"))
        if self.show_text:
            style = p.style()
            text = f"{ratio:.0%}"
            width, height = p.measure(text, size=style.font_size, font_family=style.font_family)
            # Omit text when a constrained ring has no readable inner space.
            if hypot(width, height) <= diameter - thickness * 2 - 4:
                p.text(text, cx - width / 2, cy - height / 2, color=style.foreground,
                       size=style.font_size, font_family=style.font_family)
