"""Short interaction transitions; settled controls own no animation work."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from math import inf
from time import perf_counter
from typing import TYPE_CHECKING, Literal

from .painting import resolve_style
from .schema import Dirty
from .theme import ResolvedStyle, Style, Theme, as_resolved, style_instance

if TYPE_CHECKING:
    from .controls import Control
    from .runtime import Runtime


Interaction = tuple[Literal["disabled", "pressed", "hover", "normal"], bool]


def interaction_state(control: Control) -> Interaction:
    return (
        "disabled" if not control.effective_enabled else
        "pressed" if control._pressed else
        "hover" if control._hover else "normal",
        bool(getattr(control, "checked", False)),
    )


def _color(first: str, last: str, fraction: float) -> str:
    # Premultiplied interpolation avoids dark fringes when an effect fades out.
    a = bytes.fromhex(first[1:] + ("ff" if len(first) == 7 else ""))
    b = bytes.fromhex(last[1:] + ("ff" if len(last) == 7 else ""))
    alpha = a[3] + (b[3] - a[3]) * fraction
    rgb = (
        round((x * a[3] * (1 - fraction) + y * b[3] * fraction) / alpha)
        if alpha else y
        for x, y in zip(a[:3], b[:3])
    )
    return "#" + "".join(f"{channel:02x}" for channel in (*rgb, round(alpha)))


def _blend_color(first: str | None, last: str | None, fraction: float) -> str | None:
    if first == last:
        return last
    if first and last:
        return _color(first, last, fraction)
    return last


def _blend_fade(first: str | None, last: str | None, fraction: float) -> str | None:
    if first == last:
        return last
    # Optional light and shadow layers retain their hue while their alpha fades.
    # Dropping the layer at the start causes a flash on hover exit, even while
    # the rest of the surface is still moving.
    if first is None:
        return None if last is None else _color(last[:7] + "00", last, fraction)
    if last is None:
        return _color(first, first[:7] + "00", fraction)
    return _color(first, last, fraction)


def _blend_gradient(
    first: str | None,
    last: str | None,
    first_base: str | None,
    last_base: str | None,
    fraction: float,
) -> str | None:
    if first == last:
        return last
    # An absent endpoint means a solid face, not a transparent one. Keep both
    # ends in the same interpolation to prevent a sudden gradient when entering
    # or leaving an illuminated state.
    blended_first = first or first_base
    blended_last = last or last_base
    if blended_first and blended_last:
        return _color(blended_first, blended_last, fraction)
    return blended_last


def _blend_distance(
    first: float | None, last: float | None, fraction: float, *, rounded: bool = False
) -> float | None:
    if first == last:
        return last
    value = (first or 0) + ((last or 0) - (first or 0)) * fraction
    # Shadow masks are keyed by shape. Integer feathers avoid allocating a
    # distinct raster for imperceptible subpixel blur changes.
    if rounded:
        return round(value)
    return value


def _blend(first: ResolvedStyle, last: ResolvedStyle, fraction: float) -> Style:
    if fraction <= 0:
        return style_instance(first)
    if fraction >= 1:
        return style_instance(last)
    start = style_instance(first)
    end = style_instance(last)
    # Layout metrics and optional effect topology remain at their target.
    return replace(
        end,
        fill=_blend_color(start.fill, end.fill, fraction),
        foreground=_blend_color(start.foreground, end.foreground, fraction),
        border=_blend_color(start.border, end.border, fraction),
        radius=_blend_distance(start.radius, end.radius, fraction),
        border_width=_blend_distance(start.border_width, end.border_width, fraction),
        fill_end=_blend_gradient(
            start.fill_end, end.fill_end, start.fill, end.fill, fraction
        ),
        border_end=_blend_gradient(
            start.border_end, end.border_end, start.border, end.border, fraction
        ),
        bevel_width=_blend_distance(start.bevel_width, end.bevel_width, fraction),
        bevel_light=_blend_color(start.bevel_light, end.bevel_light, fraction),
        bevel_dark=_blend_color(start.bevel_dark, end.bevel_dark, fraction),
        highlight=_blend_fade(start.highlight, end.highlight, fraction),
        inner_border=_blend_fade(start.inner_border, end.inner_border, fraction),
        glow=_blend_fade(start.glow, end.glow, fraction),
        glow_width=_blend_distance(start.glow_width, end.glow_width, fraction),
        pattern_color=_blend_color(start.pattern_color, end.pattern_color, fraction),
        shadow=_blend_fade(start.shadow, end.shadow, fraction),
        shadow_blur=_blend_distance(
            start.shadow_blur, end.shadow_blur, fraction, rounded=True
        ),
        shadow_x=_blend_distance(start.shadow_x, end.shadow_x, fraction),
        shadow_y=_blend_distance(start.shadow_y, end.shadow_y, fraction),
    )


class _Transition:
    def __init__(
        self,
        control: Control,
        after: Interaction,
        styles: dict[str, tuple[ResolvedStyle, ResolvedStyle]],
        checked: float,
        started: float,
    ) -> None:
        self.state = after
        self.theme: Theme = control.effective_theme
        self.styles = styles
        self.start_checked = checked
        self.end_checked = float(after[1])
        self.started = started
        self.duration = 0.07 if after[0] == "pressed" else 0.16
        self.progress = 0.0
        self._samples: dict[str, Style] = {}

    @property
    def checked(self) -> float:
        return self.start_checked + (self.end_checked - self.start_checked) * self.progress

    def style(self, part: str, target: ResolvedStyle) -> ResolvedStyle:
        pair = self.styles.get(part)
        if pair is None or target != pair[1]:
            return target
        if part not in self._samples:
            self._samples[part] = _blend(pair[0], pair[1], self.progress)
        return as_resolved(self._samples[part])


class Motion:
    """Only the active set is visited, and finite motion never sets continuous mode."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self._active: dict[Control, _Transition] = {}
        self.next_frame_at = inf

    def changed(
        self, control: Control, before: Interaction, *, now: float | None = None
    ) -> None:
        if control._disposed:
            self.remove(control)
            return
        after = interaction_state(control)
        if before == after:
            return
        host = self.runtime.host
        if getattr(host, "native_retained", False):
            self.remove(control)
            if (
                control is not self.runtime.app
                and not self.runtime.app.reduce_motion
                and after[0] != "disabled"
            ):
                request: Callable[[float], None] = getattr(host, "request_transition")
                request(0.07 if after[0] == "pressed" else 0.16)
            # Submit target styles once. C samples the transition and redraws;
            # retaining a Python _Transition would generate old/intermediate data.
            return
        if (
            control is self.runtime.app
            or self.runtime.app.reduce_motion
            or after[0] == "disabled"
        ):
            self.remove(control)
            return
        previous = control._motion
        styles: dict[str, tuple[ResolvedStyle, ResolvedStyle]] = {}
        for part in control.style_parts:
            first = resolve_style(control, part, state=before[0], checked=before[1])
            if previous is not None:
                first = previous.style(part, first)
            last = resolve_style(control, part, state=after[0], checked=after[1])
            if first != last:
                styles[part] = first, last
        checked = previous.checked if previous is not None else float(before[1])
        if not styles and checked == float(after[1]):
            self.remove(control)
            return
        started = perf_counter() if now is None else now
        control._motion = _Transition(control, after, styles, checked, started)
        self._active[control] = control._motion
        self.next_frame_at = min(self.next_frame_at, started + 1 / 60)

    def remove(self, control: Control) -> None:
        self._active.pop(control, None)
        control._motion = None
        if not self._active:
            self.next_frame_at = inf

    def _invalidate(self, control: Control) -> None:
        # The runtime already chose to paint this frame. Do not request another
        # immediate frame through _wake; next_frame_at provides the finite demand.
        retained = self.runtime._retained_paint
        if retained is not None:
            retained.invalidate(control, Dirty.PAINT)
        node: Control | None = control
        while node is not None:
            node._dirty |= Dirty.PAINT
            node._paint_revision += 1
            node = node._parent

    def tick(self, now: float) -> None:
        if not self._active or now < self.next_frame_at:
            return
        for control, transition in tuple(self._active.items()):
            if (
                control._disposed or control._root() is not self.runtime.app
                or self.runtime.app.reduce_motion
                or control.effective_theme is not transition.theme
                or interaction_state(control) != transition.state
                or not self.runtime.router._visible(control)
            ):
                self.remove(control)
                if not control._disposed:
                    self._invalidate(control)
                continue
            elapsed = max(0.0, (now - transition.started) / transition.duration)
            if elapsed >= 1:
                self.remove(control)
                self._invalidate(control)
                continue
            progress = round((1 - (1 - elapsed) ** 3) * 64) / 64
            if progress != transition.progress:
                transition.progress = progress
                transition._samples.clear()
                self._invalidate(control)
        self.next_frame_at = now + 1 / 60 if self._active else inf

    def clear(self) -> None:
        for control in tuple(self._active):
            self.remove(control)
            if not control._disposed:
                control.invalidate()
