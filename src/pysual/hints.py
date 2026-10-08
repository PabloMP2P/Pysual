"""A delayed, noninteractive hint owned by the active UI session."""

from time import perf_counter as monotonic
from math import inf
from typing import ClassVar
from ._text_display import single_line
from .controls import Label


class _Hint(Label):
    _overlay: ClassVar[bool] = True

    def _layout_size(self, width, height):
        if self.parent is not None:
            area = self.parent.content_bounds(self.parent.bounds)
            return min(area.width, width), min(area.height, height)
        return width, height

    def paint(self, p, /):
        style = p.body()
        p.text(
            p.elide(
                single_line(self.text), max(0, p.width - 16), size=style.font_size
            ),
            8,
            (p.height - style.font_size * 1.2) / 2,
            size=style.font_size,
            color=style.foreground,
        )


class Hints:
    def __init__(self, runtime):
        self.runtime = runtime
        self.owner = self.control = None
        self.active = False
        self._keyboard = False
        self._since = 0.0
        self._text = ""

    @property
    def next_deadline(self):
        """One hint delay, never a recurring frame or input polling timer."""
        if self.active and self.owner is not None and self.control is None:
            return self._since + 0.5
        return inf

    def clear(self):
        if self.control is not None:
            self.control.destroy()
        self.owner = self.control = None
        self._text = ""

    def observe(self, event):
        if self._keyboard and event.kind == "key_up":
            return
        focus = self.runtime.router.focus
        keyboard = (
            event.kind == "key_down"
            and event.key == "Tab"
            and (event.ctrl or not (focus and getattr(focus, "multiline", False)))
        )
        if keyboard or self._keyboard or event.kind != "pointer_move":
            self.clear()
        self._keyboard = keyboard
        self.active = keyboard or event.kind == "pointer_move"

    def update(self):
        runtime = self.runtime
        owner = runtime.router.focus if self._keyboard else runtime.router.hover
        if (
            not self.active
            or runtime.popup is not None
            or runtime.router.capture is not None
            or owner is None
            or not runtime.router._visible(owner)
            or not owner.tooltip
            or owner._clip.width <= 0
            or owner._clip.height <= 0
        ):
            self.clear()
            return
        now = monotonic()
        if owner is not self.owner or owner.tooltip != self._text:
            self.clear()
            self.owner, self._text, self._since = owner, owner.tooltip, now
        if self.control is None:
            if now - self._since < 0.5:
                return
            self.control = runtime.app.add(
                _Hint(
                    text=self._text,
                    height=32,
                    enabled=False,
                )
            )
        area = runtime.app.content_bounds(runtime.app.bounds)
        hint = self.control
        theme = owner.effective_theme
        hint.theme_override = theme
        hint.foreground = theme.tokens.foreground
        hint.background = theme.tokens.raised
        hint.width = min(
            360,
            runtime.host.measure(
                single_line(self._text), theme.tokens.font_size,
                theme.tokens.font_family == "mono",
            )[
                0
            ]
            + 16,
        )
        width, height = hint._layout_size(hint.width, 32)
        x, y = owner.bounds.x, owner.bounds.bottom + 6
        if y + height > area.bottom:
            y = owner.bounds.y - height - 6
        hint.left = max(area.x, min(x, area.right - width)) - area.x
        hint.top = max(area.y, min(y, area.bottom - height)) - area.y
