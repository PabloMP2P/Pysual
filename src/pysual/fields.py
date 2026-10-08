"""Composite fields assembled from ordinary controls and their input hooks."""

from __future__ import annotations

from typing import ClassVar, Literal

from .controls import Button, Container, Label
from .events import ChangeEvent, Event, UiEvent
from .geometry import Rect
from .schema import Dirty, prop
from .text import TextBox


class _SearchIcon(Label):
    def measure(self, host):
        return 28, self._control_height(host)

    def paint(self, painter, /):
        size = min(18, painter.width, painter.height)
        painter.icon(
            "search", (painter.width - size) / 2, (painter.height - size) / 2,
            size=size, color=painter.theme.tokens.muted,
        )

    def handle_input(self, event, /):
        if event.kind == "pointer_down" and event.button == 1 and self.effective_enabled:
            self.parent.focus()


class _SearchEntry(TextBox):
    def paint(self, painter, /):
        # The owner supplies one continuous field surface. Keep TextBox's
        # selection, caret, composition and horizontal scrolling machinery.
        self._measure_host = painter.host
        self._sync_scrollbars(painter.host)
        with painter._clipped(self._text_viewport()):
            self._paint_text(painter, painter.style(), self._text_viewport())

    def paint_focus(self, painter, /):
        # SearchField paints the shared outline, including the search icon.
        pass

    def _validate_live_update(self, name, value):
        super()._validate_live_update(name, value)
        owner = self._parent
        if name == "text" and isinstance(owner, SearchField) and not owner._syncing:
            # Validate before TextBox commits its text, selection or history.
            owner._validate_candidate({"text": value})

    def _changed(self, field, old, value):
        super()._changed(field, old, value)
        owner = self._parent
        if field.name == "text" and isinstance(owner, SearchField) and not owner._syncing:
            owner._accept_text(value, self._origin)

    def handle_input(self, event, /):
        super().handle_input(event)
        owner = self._parent
        if event.kind in ("focus", "blur") and isinstance(owner, SearchField):
            owner.invalidate()
        if (
            event.kind == "key_down" and event.key == "Enter"
            and isinstance(owner, SearchField) and self.effective_enabled
        ):
            owner.submitted.emit(UiEvent(source=owner, origin=self._origin))


class _ClearButton(Button):
    style_excludes: ClassVar[tuple[str, ...]] = ("Button",)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("TextBox",)

    def paint(self, painter, /):
        owner = self._parent
        if not isinstance(owner, SearchField) or not owner.text:
            return
        edge = min(24, painter.width, painter.height)
        rect = Rect((painter.width - edge) / 2, (painter.height - edge) / 2, edge, edge)
        if self.effective_enabled and (self._hover or self._pressed):
            painter.rect(rect, painter.theme.tokens.selection, radius=min(6, edge / 2))
        size = min(14, edge)
        painter.icon("close", (painter.width - size) / 2, (painter.height - size) / 2,
                     size=size, color=painter.theme.tokens.muted)

    def activate(self):
        self._check_activation()
        owner = self._parent
        if self.effective_enabled and isinstance(owner, SearchField):
            # A rejected clear must preserve selection and emit no click.
            owner._validate_candidate({"text": ""})
            # Clearing can disable this button. Emit its usual click first.
            super().activate()
            owner._clear(self._origin)


class SearchField(Container):
    """A single-line search entry with a search icon and an undoable clear action.

    ``changed`` reports one semantic text change. ``submitted`` fires on Enter;
    read ``text`` for the query. ``focus()`` targets the owned TextBox. Read-only
    fields permit selection and submission, but prevent typing and clearing.
    """

    style_excludes: ClassVar[tuple[str, ...]] = ("Container",)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("TextBox",)
    cache_paint: bool = prop(default=True)
    text: str = prop(default="", affects=Dirty.PAINT, changed="changed")
    placeholder: str = prop(default="Search")
    read_only: bool = prop(default=False)
    layout: Literal["absolute", "stack", "grid", "flow", "dock"] = prop(
        default="stack", affects=Dirty.MEASURE,
    )
    direction: Literal["vertical", "horizontal"] = prop(
        default="horizontal", affects=Dirty.MEASURE,
    )
    spacing: float = prop(default=0.0, minimum=0, affects=Dirty.MEASURE)
    changed: ClassVar[Event[ChangeEvent[str]]] = Event(ChangeEvent)
    submitted: ClassVar[Event[UiEvent]] = Event(UiEvent)

    def __setattr__(self, name, value):
        if name == "text" and isinstance(value, str):
            value = TextBox._normalize_text(value, False)
        super().__setattr__(name, value)

    def _prepare_update(self, values):
        values = super()._prepare_update(values)
        if "text" in values and isinstance(values["text"], str):
            return {**values, "text": TextBox._normalize_text(values["text"], False)}
        return values

    def _initialize(self):
        super()._initialize()
        self._values["text"] = TextBox._normalize_text(self.text, False)
        self._syncing = False
        self.search_icon = _SearchIcon(width=32)
        self.entry = _SearchEntry(
            text=self.text, placeholder=self.placeholder, read_only=self.read_only,
            flex=1,
        )
        self.clear_button = _ClearButton(
            icon="close", tooltip="Clear search", width=36,
            enabled=bool(self.text) and not self.read_only,
        )

    def paint(self, painter, /):
        style = painter.body()
        if self.entry.focused:
            painter.focus_ring(painter.theme.tokens.accent, style.radius or 0)

    def handle_input(self, event, /):
        if event.kind == "pointer_down" and event.button == 1 and self.effective_enabled:
            self.focus()

    def _changed(self, field, old, value):
        if field.name in ("text", "placeholder", "read_only"):
            self._syncing = True
            origin = self.entry._origin
            self.entry._origin = self._origin
            try:
                self.entry.update(
                    text=self.text, placeholder=self.placeholder, read_only=self.read_only,
                )
                self.clear_button.enabled = bool(self.text) and not self.read_only
                if field.name == "text" and bool(old) != bool(value):
                    # Read-only fields keep the clear action disabled, so its
                    # enabled state cannot invalidate the glyph for us.
                    self.clear_button.invalidate()
            finally:
                self.entry._origin = origin
                self._syncing = False
        super()._changed(field, old, value)

    def _accept_text(self, text, origin):
        previous = self._origin
        self._origin = origin
        try:
            self.text = text
        finally:
            self._origin = previous

    def _clear(self, origin):
        if not self.effective_enabled or self.read_only:
            return
        previous = self.entry._origin
        self.entry._origin = origin
        try:
            self.entry.select_all()
            self.entry.replace_selection("")
        finally:
            self.entry._origin = previous
        self.entry.focus()

    def focus(self) -> None:
        """Move keyboard focus into the search entry."""
        self._check_live()
        self.entry.focus()

    @property
    def focused(self) -> bool:
        """Whether the entry or its clear action owns keyboard focus."""
        self._check_live()
        return self.entry.focused or self.clear_button.focused


__all__ = ["SearchField"]
