"""Labels and command buttons."""

from __future__ import annotations

from typing import ClassVar

from ..behaviors import PressBehavior
from ..commands import normalize_shortcut
from ..controls import Control
from ..errors import LifecycleError
from ..geometry import Rect
from ..events import ClickEvent, Event
from ..icons import icon_names
from ..painting import resolve_style, Painter
from ..schema import Dirty, expect_text, prop
from ..host import Host, Input


class Label(Control):
    cache_paint: bool = prop(default=True)
    text: str = prop(
        default="", affects=Dirty.MEASURE | Dirty.PAINT, doc="Displayed text"
    )
    _style_kind: ClassVar[str] = "Label"

    def measure(self, host: Host) -> tuple[float, float]:
        style = resolve_style(self)
        return host.measure(self.text, style.font_size, style.font_family == "mono")

    def paint(self, painter: Painter, /) -> None:
        super().paint(painter)
        style = painter.style()
        multiline = "\n" in self.text
        painter.caption(self.text, color=style.foreground, size=style.font_size,
                        font_family=style.font_family, ellipsis=not multiline,
                        line_height=None if multiline else style.font_size * 1.2)


class Button(Label):
    icon: str = prop(
        default="",
        affects=Dirty.MEASURE | Dirty.PAINT,
        doc="Optional line-icon catalog name",
    )
    shortcut: str = prop(default="", affects=Dirty.NONE)

    def _validate_update(self, name: str, value: object) -> None:
        super()._validate_update(name, value)
        if name == "icon":
            icon = expect_text(value)
            if icon and icon not in icon_names():
                raise ValueError(f"Unknown icon {icon!r}")
        if name == "shortcut":
            normalize_shortcut(expect_text(value))

    def invoke_shortcut(self, shortcut: str) -> bool:
        if (
            self.shortcut
            and normalize_shortcut(self.shortcut) == shortcut
            and self.effective_enabled
        ):
            self.activate()
            return True
        return False

    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    _style_kind: ClassVar[str] = "Button"
    click: ClassVar[Event[ClickEvent]] = Event(ClickEvent, doc="Semantic activation")

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior()

    def measure(self, host: Host) -> tuple[float, float]:
        w, h = super().measure(host)
        padding = resolve_style(self).padding
        if self.icon and not self.text:
            edge = max(20 + padding * 2, self.effective_theme.tokens.control_height)
            return edge, edge
        return max(90, w + padding * 2 + (28 if self.icon else 0)), max(
            h + padding * 2, self.effective_theme.tokens.control_height
        )

    def paint(self, painter: Painter, /) -> None:
        s = painter.body()
        content = painter.content_rect()
        painter.caption(self.text, Rect(content.x, 0, content.width, painter.height),
                        align="center", icon=self.icon, color=s.foreground,
                        size=s.font_size, font_family=s.font_family)

    def _check_activation(self):
        self._check_live()
        if self._dispatcher is None or self._dispatcher._stopped:
            raise LifecycleError(
                "Activation requires a running App; use the loaded handler for startup actions"
            )

    def activate(self):
        """Emit `click` when enabled, through the pointer/keyboard event path.
        Requires a running App; use its loaded handler for startup actions.
        Returns immediately; event handlers run through the App's dispatcher.
        The event retains the current origin, normally `program` for a direct
        call. See
        [events and lifetime](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#owned-tasks-and-callbacks).
        """
        self._check_activation()
        if self.effective_enabled:
            self.click.emit(ClickEvent(source=self, origin=self._origin))

    def handle_input(self, event: Input, /) -> bool | None:
        if self._press.handle_input(self, event):
            self.activate()

    def destroy(self) -> None:
        self._press.reset(self)
        super().destroy()


# Keep public imports, diagnostics, and reflection stable across source moves.
Label.__module__ = "pysual.controls"
Button.__module__ = "pysual.controls"
