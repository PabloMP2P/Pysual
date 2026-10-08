"""Semantic status labels, persistent alerts, and measured levels."""

from __future__ import annotations

from math import isfinite
from typing import ClassVar, Literal

from ._ranges import NumericRange
from ._text_display import single_line
from .behaviors import PressBehavior
from .controls import Control
from .errors import LifecycleError
from .events import ChangeEvent, ClickEvent, Event, UiEvent
from .geometry import Rect
from .painting import resolve_style
from .schema import Dirty, prop


def _caption(p, text, area, style, *, centered=False):
    if area.width <= 0 or area.height <= 0:
        return
    with p._clipped(area):
        text = p.elide(single_line(text), area.width, size=style.font_size,
                       font_family=style.font_family)
        width, height = p.measure(text, size=style.font_size,
                                  font_family=style.font_family)
        p.text(text, area.x + ((area.width - width) / 2 if centered else 0),
               area.y + max(0, (area.height - height) / 2),
               color=style.foreground, size=style.font_size,
               font_family=style.font_family)


class Badge(Control):
    """A compact, noninteractive status caption with an optional presence dot.

    The caption carries the meaning independently of its semantic color.
    Style the selected tone part to customize its surface and text together.
    """

    cache_paint: bool = prop(default=True)
    text: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    tone: Literal["neutral", "info", "success", "warning", "danger"] = prop(
        default="neutral", affects=Dirty.MEASURE | Dirty.PAINT,
    )
    show_dot: bool = prop(default=True, affects=Dirty.MEASURE | Dirty.PAINT)
    style_parts: ClassVar[tuple[str, ...]] = (
        "body", "neutral", "info", "success", "warning", "danger",
    )

    def measure(self, host):
        style = resolve_style(self, self.tone, state="normal")
        width, height = host.measure(single_line(self.text), style.font_size,
                                     style.font_family == "mono")
        padding = max(10, style.padding)
        dot_width = (14 if self.text else 7) if self.show_dot else 0
        return max(24, width + padding * 2 + dot_width), max(24, height + 8)

    def paint(self, p, /):
        style = resolve_style(self, self.tone,
                              state="normal" if self.effective_enabled else "disabled")
        p.surface(Rect(0, 0, p.width, p.height), style)
        padding = min(max(8, style.padding), p.width / 2)
        available = max(0, p.width - padding * 2)
        dot = min(7, available, max(0, p.height - 8)) if self.show_dot else 0
        gap = min(7, max(0, available - dot)) if dot and self.text else 0
        caption = p.elide(single_line(self.text), max(0, available - dot - gap),
                          size=style.font_size, font_family=style.font_family)
        text_width, text_height = (p.measure(caption, size=style.font_size,
                                            font_family=style.font_family)
                                   if caption else (0, 0))
        # Center the complete dot/caption group, including explicit-width pills.
        # An empty or fully elided caption leaves a centered presence indicator.
        if not caption:
            gap = 0
        left = (p.width - dot - gap - text_width) / 2
        if dot:
            p.rect(Rect(left, (p.height - dot) / 2, dot, dot),
                   style.foreground, radius=dot / 2)
        if caption:
            with p._clipped(Rect(left + dot + gap, 0, text_width, p.height)):
                p.text(caption, left + dot + gap, max(0, (p.height - text_height) / 2),
                       color=style.foreground, size=style.font_size,
                       font_family=style.font_family)


class AlertBanner(Control):
    """A persistent single-line condition with optional action and dismissal.

    ``action`` asks the application to perform ``action_text``; it leaves the
    alert visible. ``dismiss()`` hides it and emits ``dismissed`` once. Set
    ``visible=True`` to show it again. Both methods require a running App.
    Left/Right/Home/End choose between action and close, Enter/Space invoke the
    focused part on release, and a matched Escape press/release dismisses.
    """

    cache_paint: bool = prop(default=True)
    text: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    tone: Literal["info", "success", "warning", "danger"] = prop(
        default="info", affects=Dirty.MEASURE | Dirty.PAINT,
    )
    action_text: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    dismissible: bool = prop(default=True, affects=Dirty.MEASURE | Dirty.PAINT)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    action: ClassVar[Event[ClickEvent]] = Event(ClickEvent)
    dismissed: ClassVar[Event[UiEvent]] = Event(UiEvent)
    style_parts: ClassVar[tuple[str, ...]] = (
        "body", "info", "success", "warning", "danger", "action", "close",
    )

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior(keys=("Enter", "Space", "Escape"))
        self._hot_part = self._armed_part = self._key_part = None
        self._pointer_id = None
        self._focus_part = "action" if self.action_text else "close"

    def _clear_interaction(self):
        self._press.reset(self)
        self._hot_part = self._armed_part = self._key_part = None
        self._pointer_id = None

    def _changed(self, field, old, value):
        self._clear_interaction()
        if self._focus_part not in self._targets():
            self._focus_part = next(iter(self._targets()), None)
        super()._changed(field, old, value)

    def _targets(self):
        return (("action",) if self.action_text else ()) + (("close",) if self.dismissible else ())

    def _action_width(self, host=None):
        if not self.action_text:
            return 0
        host = host or getattr(self._root(), "_layout_measure_host", None)
        style = resolve_style(self, "action", state="normal")
        text = single_line(self.action_text)
        width = (host.measure(text, style.font_size, style.font_family == "mono")[0]
                 if host is not None else len(text) * style.font_size * 0.65)
        return min(200, max(48, width + 20))

    def measure(self, host):
        style = resolve_style(self, self.tone, state="normal")
        width, height = host.measure(single_line(self.text), style.font_size,
                                     style.font_family == "mono")
        return max(240, width + 56 + self._action_width(host) + (36 if self.dismissible else 0)), max(
            self.effective_theme.tokens.control_height + 12, height + 24,
        )

    def _areas(self):
        width, height = self._rect.width, self._rect.height
        inset = min(10, width / 2, height / 2)
        inner = Rect(0, 0, width, height).inset(inset)
        right = inner.right
        areas = {}
        if self.dismissible:
            edge = min(28, inner.width, inner.height)
            areas["close"] = Rect(right - edge, (height - edge) / 2, edge, edge)
            right = max(inner.x, right - edge - 8)
        if self.action_text:
            edge = min(30, inner.height)
            available = max(0, right - inner.x)
            # Keep space for a meaningful condition while letting an action
            # use its natural width when the whole banner is unconstrained.
            action_width = min(self._action_width(), available - min(76, available * 0.5))
            areas["action"] = Rect(right - action_width, (height - edge) / 2, action_width, edge)
            right = max(inner.x, right - action_width - 12)
        icon = min(20, inner.height, max(0, right - inner.x))
        areas["icon"] = Rect(inner.x, (height - icon) / 2, icon, icon)
        left = min(right, inner.x + icon + 10)
        areas["text"] = Rect(left, inner.y, max(0, right - left), inner.height)
        return areas

    def _hit_part(self, event):
        if not self._rect.contains(event.x, event.y):
            return None
        for part, area in self._areas().items():
            if part in ("action", "close") and area.contains(
                event.x - self._rect.x, event.y - self._rect.y,
            ):
                return part
        return None

    def _check_action(self):
        self._check_live()
        if self._dispatcher is None or self._dispatcher._stopped:
            raise LifecycleError("Alert actions require a running App")

    def activate(self) -> None:
        """Request the named action without changing alert visibility."""
        self._check_action()
        if self.effective_enabled and self.visible and self.action_text:
            self.action.emit(ClickEvent(source=self, origin=self._origin))

    def dismiss(self) -> None:
        """Hide a dismissible alert and emit one semantic dismissal event."""
        self._check_action()
        if self.effective_enabled and self.visible and self.dismissible:
            self.visible = False
            self.dismissed.emit(UiEvent(source=self, origin=self._origin))

    def handle_input(self, event, /):
        self._check_live()
        if not self.effective_enabled or not self.visible or event.kind == "blur":
            self._clear_interaction()
            return
        if event.kind == "pointer_cancel":
            if self._pointer_id is None or self._pointer_id == event.pointer_id:
                self._clear_interaction()
            return
        if event.kind == "pointer_leave":
            self._hot_part = None
            self.invalidate()
            return
        if event.kind.startswith("pointer"):
            if self._key_part is not None or (
                self._pointer_id is not None and self._pointer_id != event.pointer_id
            ):
                return
            part = self._hit_part(event)
            if self._hot_part != part:
                self._hot_part = part
                self.invalidate()
            if event.kind == "pointer_down":
                if event.button != 1 or part is None or self._pointer_id is not None:
                    return
                self._pointer_id = event.pointer_id
                self._armed_part = self._focus_part = part
        elif event.kind in ("key_down", "key_up"):
            if self._pointer_id is not None:
                return
            targets = self._targets()
            if event.kind == "key_down" and event.key in (
                "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End",
            ):
                self._clear_interaction()
                if targets:
                    index = targets.index(self._focus_part) if self._focus_part in targets else 0
                    self._focus_part = targets[
                        0 if event.key == "Home" else len(targets) - 1 if event.key == "End"
                        else (index + (1 if event.key in ("ArrowRight", "ArrowDown") else -1)) % len(targets)
                    ]
                self.invalidate()
                return
            if event.key not in ("Enter", "Space", "Escape"):
                return
            if event.kind == "key_down" and self._key_part is None:
                part = "close" if event.key == "Escape" else self._focus_part
                if part not in targets:
                    return
                self._key_part = self._armed_part = part
            part = self._key_part
        else:
            return
        activated = self._press.handle_input(self, event, part=part)
        if event.kind == "pointer_up" and event.button == 1:
            self._pointer_id = None
        if activated:
            self._clear_interaction()
            self.activate() if part == "action" else self.dismiss()

    def paint(self, p, /):
        style = resolve_style(self, self.tone,
                              state="normal" if self.effective_enabled else "disabled")
        p.surface(Rect(0, 0, p.width, p.height), style)
        areas = self._areas()
        icon = areas["icon"]
        if icon.width > 0:
            # Line icons stay legible even on hosts without icon-font support.
            if self.tone == "success":
                p.icon("check", icon.x, icon.y, size=icon.width, color=style.foreground)
            else:
                p.rect(icon, "", radius=icon.width / 2, border=style.foreground)
                _caption(p, "i" if self.tone == "info" else "!", icon, style, centered=True)
        _caption(p, self.text, areas["text"], style)
        for part in self._targets():
            state = ("disabled" if not self.effective_enabled else
                     "pressed" if self._pressed and self._armed_part == part else
                     "hover" if self._hot_part == part else "normal")
            face = resolve_style(self, part, state=state,
                                 selected=self.focused and self._focus_part == part)
            area = areas[part]
            if area.width <= 0 or area.height <= 0:
                continue
            p.surface(area, face)
            if part == "action":
                _caption(p, self.action_text, area.inset(min(5, area.width / 2)), face, centered=True)
            else:
                edge = min(16, area.width, area.height)
                p.icon("close", area.x + (area.width - edge) / 2,
                       area.y + (area.height - edge) / 2, size=edge, color=face.foreground)

    def destroy(self):
        self._clear_interaction()
        super().destroy()


class Meter(Control):
    """A noninteractive measurement, continuous or split into equal segments.

    Optional thresholds are absolute values: at/above warning_at or danger_at,
    the filled level changes tone. Danger takes precedence. Thresholds must be
    ordered and inside the range. Value labels display raw units, not progress.
    """

    cache_paint: bool = prop(default=True)
    value: float = prop(default=0.0, changed="changed", affects=Dirty.MEASURE | Dirty.PAINT)
    minimum: float = prop(default=0.0)
    maximum: float = prop(default=100.0)
    label: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    unit: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    show_value: bool = prop(default=True, affects=Dirty.MEASURE | Dirty.PAINT)
    segments: int = prop(default=0, minimum=0)
    warning_at: float | None = prop(default=None)
    danger_at: float | None = prop(default=None)
    changed: ClassVar[Event[ChangeEvent[float]]] = Event(ChangeEvent)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("ProgressBar",)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "track", "fill", "warning", "danger")

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        values = {**self._values, name: value}
        bounds = NumericRange(values["minimum"], values["maximum"])
        if (not bounds.is_ordered(strict=True) or not bounds.contains(values["value"])
                or not isfinite(values["maximum"] - values["minimum"])):
            raise ValueError("Meter requires minimum <= value <= maximum and a finite positive range")
        warning, danger = values["warning_at"], values["danger_at"]
        if (any(threshold is not None and not bounds.contains(threshold) for threshold in (warning, danger))
                or warning is not None and danger is not None and warning > danger):
            raise ValueError("Meter thresholds must be ordered and inside the range")
        if values["segments"] > 100:
            raise ValueError("Meter supports at most 100 segments")

    def set_range(self, minimum: float, maximum: float, *, value: float | None = None) -> None:
        """Commit bounds/value together, clamping an omitted value and thresholds."""
        self._check_live()
        self._schema["minimum"].validate(minimum)
        self._schema["maximum"].validate(maximum)
        bounds = NumericRange(minimum, maximum)
        if not bounds.is_ordered(strict=True):
            raise ValueError("Meter requires minimum < maximum")
        self.update(minimum=minimum, maximum=maximum,
                    value=bounds.clamp(self.value) if value is None else value,
                    warning_at=bounds.clamp(self.warning_at) if self.warning_at is not None else None,
                    danger_at=bounds.clamp(self.danger_at) if self.danger_at is not None else None)

    def _value_text(self):
        return f"{self.value:g}{(' ' + self.unit) if self.unit else ''}"

    def measure(self, host):
        style = resolve_style(self)
        width, height = host.measure(single_line(self.label), style.font_size,
                                     style.font_family == "mono")
        if self.show_value:
            width += host.measure(single_line(self._value_text()), style.font_size,
                                  style.font_family == "mono")[0] + 20
        return max(160, width), 14 + (height + 8 if self.label or self.show_value else 0)

    def paint(self, p, /):
        style = p.style()
        label_height = min(style.font_size * 1.2 + 8, max(0, p.height - 8)) if self.label or self.show_value else 0
        if label_height and p.height >= style.font_size * 1.2 + 8:
            value_width = 0
            if self.show_value:
                value_width = min(p.width, p.measure(single_line(self._value_text()),
                                                    size=style.font_size, font_family=style.font_family)[0])
                _caption(p, self._value_text(), Rect(p.width - value_width, 0, value_width,
                                                    label_height - 6), style)
            _caption(p, self.label, Rect(0, 0, max(0, p.width - value_width - (12 if value_width else 0)),
                                        label_height - 6), style)
        height = min(12, max(0, p.height - label_height))
        if height <= 0 or p.width <= 0:
            return
        y = label_height + (p.height - label_height - height) / 2
        ratio = (self.value - self.minimum) / (self.maximum - self.minimum)
        tone = ("danger" if self.danger_at is not None and self.value >= self.danger_at else
                "warning" if self.warning_at is not None and self.value >= self.warning_at else "fill")
        state = "normal" if self.effective_enabled else "disabled"
        track = resolve_style(self, "track", state=state)
        fill = resolve_style(self, tone, state=state)
        count = max(1, self.segments)
        gap = min(3, p.width / (count * 2)) if count > 1 else 0
        width = max(0, (p.width - gap * (count - 1)) / count)
        for index in range(count):
            area = Rect(index * (width + gap), y, width, height)
            p.surface(area, track)
            portion = max(0, min(1, ratio * count - index))
            if portion > 0:
                p.surface(Rect(area.x, area.y, area.width * portion, area.height), fill)
