"""Checked, toggle, and exclusive-choice actions."""

from __future__ import annotations

from typing import ClassVar
from ..controls import Button
from ..events import ChangeEvent, Event
from ..geometry import Rect
from ..painting import resolve_style
from ..schema import prop


class CheckBox(Button):
    style_excludes: ClassVar[tuple[str, ...]] = ("Button",)
    checked: bool = prop(default=False, changed="changed")
    changed: ClassVar[Event[ChangeEvent[bool]]] = Event(ChangeEvent)
    _style_kind: ClassVar[str] = "CheckBox"
    style_parts: ClassVar[tuple[str, ...]] = ("body", "check")

    def measure(self, host):
        style = resolve_style(self)
        width, height = host.measure(
            self.text, style.font_size, style.font_family == "mono"
        )
        return max(90, width + 30), self._control_height(host, self.text)

    def activate(self):
        self._check_activation()
        if self.effective_enabled:
            self.checked = not self.checked
            super().activate()

    def paint(self, p, /):
        check = p.style("check")
        s = p.style()
        y = (p.height - 20) / 2
        p.marker(Rect(0, y, 20, 20), check, checked=self.checked)
        _, text_height = p.measure(
            self.text, size=s.font_size, font_family=s.font_family
        )
        p.text(
            p.elide(self.text, max(0, p.width - 30), size=s.font_size,
                    font_family=s.font_family),
            30,
            max(0, (p.height - text_height) / 2),
            color=s.foreground,
            size=s.font_size,
        )


class Toggle(CheckBox):
    style_parts: ClassVar[tuple[str, ...]] = ("body", "track", "thumb")

    def measure(self, host):
        style = resolve_style(self)
        width, height = host.measure(
            self.text,
            style.font_size,
            style.font_family == "mono",
        )
        return width + 64, self._control_height(host, self.text)

    def paint(self, p, /):
        s, track, thumb = p.style(), p.style("track"), p.style("thumb")
        y = (p.height - 24) / 2
        p.surface(Rect(0, y, 44, 24), track)
        checked = self._motion.checked if self._motion is not None else float(self.checked)
        # Respect the theme's geometry: classic square thumbs keep their bevel
        # instead of having marker() force a circular surface over that style.
        p.marker(Rect(3 + checked * 19, y + 3, 18, 18), thumb,
                 shape="circle" if thumb.radius >= 9 else "square")
        _, text_height = p.measure(
            self.text, size=s.font_size, font_family=s.font_family
        )
        p.text(
            p.elide(self.text, max(0, p.width - 56), size=s.font_size,
                    font_family=s.font_family),
            56,
            (p.height - text_height) / 2,
            color=s.foreground,
            size=s.font_size,
        )


class RadioButton(CheckBox):
    group: str = prop(default="")
    style_parts: ClassVar[tuple[str, ...]] = ("body", "check")

    def _exclusive(self):
        if self.checked and self.parent is not None:
            for sibling in self.parent.children:
                if (
                    isinstance(sibling, RadioButton)
                    and sibling is not self
                    and sibling.group == self.group
                ):
                    origin = sibling._origin
                    sibling._origin = self._origin
                    try:
                        sibling.checked = False
                    finally:
                        sibling._origin = origin

    def on_attached(self):
        self._exclusive()

    def _changed(self, field, old, value):
        if field.name in ("checked", "group"):
            self._exclusive()
        super()._changed(field, old, value)

    def activate(self):
        self._check_activation()
        if self.effective_enabled:
            self.checked = True
            Button.activate(self)

    def handle_input(self, event, /):
        if event.kind == "key_down" and event.key in (
            "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown"
        ):
            if self.parent is None or not self.effective_enabled:
                return
            peers = [
                child for child in self.parent.children
                if isinstance(child, RadioButton) and child.group == self.group
                and child.visible and child.focusable and child.effective_enabled
            ]
            if self not in peers:
                return
            delta = -1 if event.key in ("ArrowLeft", "ArrowUp") else 1
            target = peers[(peers.index(self) + delta) % len(peers)]
            previous = target._origin
            target._origin = self._origin
            try:
                target.focus()
                target.activate()
            finally:
                target._origin = previous
            return
        super().handle_input(event)

    def paint(self, p, /):
        s, check = p.style(), p.style("check")
        y = (p.height - 20) / 2
        p.marker(Rect(0, y, 20, 20), check, shape="circle", checked=self.checked)
        _, text_height = p.measure(
            self.text, size=s.font_size, font_family=s.font_family
        )
        p.text(
            p.elide(self.text, max(0, p.width - 30), size=s.font_size,
                    font_family=s.font_family),
            30,
            (p.height - text_height) / 2,
            color=s.foreground,
            size=s.font_size,
        )


# Keep public imports, diagnostics, and reflection stable across source moves.
CheckBox.__module__ = "pysual.widgets"
Toggle.__module__ = "pysual.selection"
RadioButton.__module__ = "pysual.selection"
