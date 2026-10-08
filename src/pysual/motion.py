"""Short interaction transitions; settled controls own no animation work."""

from dataclasses import fields
from math import inf
from time import perf_counter

from .painting import resolve_style
from .schema import Dirty
from .theme import Style


def interaction_state(control):
    return (
        "disabled" if not control.effective_enabled else
        "pressed" if control._pressed else
        "hover" if control._hover else "normal",
        bool(getattr(control, "checked", False)),
    )


_COLORS = frozenset((
    "fill", "foreground", "border", "fill_end", "border_end", "bevel_light",
    "bevel_dark", "highlight", "inner_border", "glow", "pattern_color", "shadow",
))
_FADE_COLORS = frozenset(("highlight", "inner_border", "glow", "shadow"))
_GRADIENT_COLORS = {"fill_end": "fill", "border_end": "border"}
_DISTANCES = frozenset((
    "radius", "border_width", "bevel_width", "glow_width", "shadow_blur",
    "shadow_x", "shadow_y",
))
_FIELDS = tuple(field.name for field in fields(Style))


def _color(first, last, fraction):
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


def _blend(first, last, fraction):
    if fraction <= 0:
        return first
    if fraction >= 1:
        return last
    values = {}
    for name in _FIELDS:
        a, b = getattr(first, name), getattr(last, name)
        if a == b:
            values[name] = b
        elif name in _COLORS:
            if name in _GRADIENT_COLORS:
                # An absent endpoint means a solid face, not a transparent one.
                # Keep both ends in the same interpolation to prevent a sudden
                # gradient when entering or leaving an illuminated state.
                base = _GRADIENT_COLORS[name]
                a = a or getattr(first, base)
                b = b or getattr(last, base)
            elif name in _FADE_COLORS:
                # Optional light and shadow layers retain their hue while their
                # alpha fades. Dropping the layer at the start causes a flash on
                # hover exit, even while the rest of the surface is still moving.
                a = a or b[:7] + "00"
                b = b or a[:7] + "00"
            values[name] = _color(a, b, fraction) if a and b else b
        elif name in _DISTANCES:
            values[name] = (a or 0) + ((b or 0) - (a or 0)) * fraction
            # Shadow masks are keyed by shape. Integer feathers avoid allocating
            # a distinct raster for imperceptible subpixel blur changes.
            if name == "shadow_blur":
                values[name] = round(values[name])
        else:
            # Layout metrics and optional effect topology remain at their target.
            values[name] = b
    return Style(**values)


class _Transition:
    def __init__(self, control, after, styles, checked, started):
        self.state = after
        self.theme = control.effective_theme
        self.styles = styles
        self.start_checked = checked
        self.end_checked = float(after[1])
        self.started = started
        self.duration = 0.07 if after[0] == "pressed" else 0.16
        self.progress = 0.0
        self._samples = {}

    @property
    def checked(self):
        return self.start_checked + (self.end_checked - self.start_checked) * self.progress

    def style(self, part, target):
        pair = self.styles.get(part)
        if pair is None or target != pair[1]:
            return target
        if part not in self._samples:
            self._samples[part] = _blend(pair[0], pair[1], self.progress)
        return self._samples[part]


class Motion:
    """Only the active set is visited, and finite motion never sets continuous mode."""

    def __init__(self, runtime):
        self.runtime = runtime
        self._active = {}
        self.next_frame_at = inf

    def changed(self, control, before, *, now=None):
        if control._disposed:
            self.remove(control)
            return
        after = interaction_state(control)
        if before == after:
            return
        if getattr(self.runtime.host, "native_retained", False):
            self.remove(control)
            if (
                control is not self.runtime.app
                and not self.runtime.app.reduce_motion
                and after[0] != "disabled"
            ):
                self.runtime.host.request_transition(0.07 if after[0] == "pressed" else 0.16)
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
        styles = {}
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

    def remove(self, control):
        self._active.pop(control, None)
        control._motion = None
        if not self._active:
            self.next_frame_at = inf

    def _invalidate(self, control):
        # The runtime already chose to paint this frame. Do not request another
        # immediate frame through _wake; next_frame_at provides the finite demand.
        control._paint_local_revision += 1
        retained = self.runtime._retained_paint
        if retained is not None:
            retained.invalidate(control, Dirty.PAINT)
        while control is not None:
            control._dirty |= Dirty.PAINT
            control._paint_revision += 1
            control = control._parent

    def tick(self, now):
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

    def clear(self):
        for control in tuple(self._active):
            self.remove(control)
            if not control._disposed:
                control.invalidate()
