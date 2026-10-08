"""Choice content shared by dropdowns and editable combos."""

from __future__ import annotations

from typing import Callable, ClassVar
from time import monotonic
from .._text_display import single_line
from .._scrolling import typeahead_prefix
from ..behaviors import PressBehavior
from ..controls import Control
from ..events import ChangeEvent, Event
from ..geometry import Rect
from ..popup import Popup
from ..schema import Dirty, prop
from ..widgets import ListView, TextBox


class _Choices(ListView):
    def _initialize(self):
        super()._initialize()
        self._commit: Callable[[int], None] | None = None
        self._down = -1

    def handle_input(self, e, /):
        super().handle_input(e)
        commit = self._commit
        assert commit is not None
        if e.kind in ("pointer_cancel", "blur"):
            self._down = -1
        elif e.kind == "pointer_down":
            self._down = self._row_at(e.x, e.y) if e.button == 1 else -1
        elif e.kind == "pointer_up":
            index = self._row_at(e.x, e.y)
            if (
                self.bounds.contains(e.x, e.y)
                and index == self._down
                and 0 <= index < len(self.items)
            ):
                commit(index)
            self._down = -1
        elif e.kind == "key_down":
            if e.key == "Enter" and self.selected_index >= 0:
                commit(self.selected_index)


def _open_choices(owner):
    previous = getattr(owner, "_popup", None)
    if previous is not None and previous.is_open:
        previous.dismiss()
        return
    if not owner.items or not owner.effective_enabled:
        return
    row_height = owner.effective_row_height(30)
    popup = Popup(
        width=max(120, owner.bounds.width),
        height=min(owner.visible_rows, len(owner.items)) * row_height,
        layout="stack",
        spacing=0,
    )
    choices = popup.add(
        _Choices(
            items=owner.items,
            row_height=30,
            selected_index=owner.selected_index,
            flex=1,
        )
    )

    def commit(index):
        old_origin = owner._origin
        owner._origin = "user"
        try:
            owner._choose(index)
        finally:
            owner._origin = old_origin
            popup.dismiss()

    choices._commit = commit
    owner._popup = popup
    try:
        popup.show(owner)
    except BaseException:
        popup.destroy()
        raise
    session = popup._session
    assert session is not None
    count = max(1, int(popup._measure_layout(session.host)[1] / choices._row_height()))
    choices._scroll = max(0, choices.selected_index - count + 1)


def _arrow(p):
    x, y = p.width - 17, p.height / 2
    color = p.style("arrow").foreground
    p.icon("chevron_down", x - 8, y - 8, size=16, color=color)


class Dropdown(Control):
    cache_paint: bool = prop(default=True)
    items: tuple[str, ...] = prop(default=())
    selected_index: int = prop(default=-1, minimum=-1, changed="changed")
    placeholder: str = prop(default="Choose…")
    visible_rows: int = prop(default=8, minimum=1)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[int]]] = Event(ChangeEvent)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "arrow")

    def _initialize(self):
        super()._initialize()
        self._prefix, self._typed_at = "", 0.0
        self._press = PressBehavior(keys=())

    def destroy(self) -> None:
        self._press.reset(self)
        super().destroy()

    @property
    def selected_item(self) -> str | None:
        return self.items[self.selected_index] if self.selected_index >= 0 else None

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "selected_index" and value >= len(self.items):
            raise ValueError("Selected index is outside items")

    def _changed(self, field, old, value):
        if field.name == "items":
            self._prefix, self._typed_at = "", 0.0
            popup = getattr(self, "_popup", None)
            if popup is not None and popup.is_open:
                popup.dismiss()
            if self.selected_index >= len(value):
                self.selected_index = -1
        super()._changed(field, old, value)

    def measure(self, host):
        return 200, self._control_height(host)

    def _choose(self, index):
        self.selected_index = index

    def open(self) -> None:
        """Toggle the choices popup without waiting for a selection.
        Opening requires a running App and nonempty items; disabled controls
        do nothing. Picking updates `selected_index` through the user event
        path. See [selection controls](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#selection-controls).
        """
        self._check_live()
        _open_choices(self)

    def handle_input(self, e, /):
        if self._press.handle_input(self, e):
            self.open()
        elif e.kind == "blur":
            self._prefix, self._typed_at = "", 0.0
        elif e.kind == "key_down" and e.key in (
            "Enter",
            "Space",
            "ArrowDown",
            "ArrowUp",
        ):
            self.open()
        elif (e.kind == "key_down" and len(e.key) == 1
              and e.key.isprintable() and not e.ctrl):
            now = monotonic()
            self._prefix, prefix = typeahead_prefix(self._prefix, self._typed_at, e.key, now)
            self._typed_at = now
            start = self.selected_index + (0 if len(prefix) > 1 else 1)
            for offset in range(len(self.items)):
                index = (start + offset) % len(self.items)
                if self.items[index].casefold().startswith(prefix):
                    self._choose(index)
                    break

    def paint(self, p, /):
        s = p.body()
        selected = self.selected_item
        text = self.placeholder if selected is None else selected
        text = p.elide(single_line(text), max(0, p.width - 40), size=s.font_size)
        p.text(
            text,
            10,
            (p.height - s.font_size * 1.2) / 2,
            color=p.theme.tokens.muted if selected is None else s.foreground,
            size=s.font_size,
        )
        _arrow(p)


class ComboBox(TextBox):
    items: tuple[str, ...] = prop(default=())
    visible_rows: int = prop(default=8, minimum=1)
    style_parts: ClassVar[tuple[str, ...]] = (*TextBox.style_parts, "arrow")

    def _text_viewport(self):
        area = super()._text_viewport()
        return Rect(area.x, area.y, max(0, area.width - 30), area.height)

    @property
    def selected_index(self) -> int:
        try:
            return self.items.index(self.text)
        except ValueError:
            return -1

    def _choose(self, index):
        self.select_all()
        self.replace_selection(self.items[index])

    def open(self) -> None:
        """Toggle the choices popup without waiting for a selection.
        Opening requires a running App and nonempty items; disabled or read-only
        controls do nothing. Picking replaces `text` as an undoable user edit.
        See [selection controls](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#selection-controls).
        """
        self._check_live()
        if not self.read_only:
            _open_choices(self)

    def _changed(self, field, old, value):
        if field.name in ("items", "text", "read_only"):
            popup = getattr(self, "_popup", None)
            if popup is not None and popup.is_open:
                popup.dismiss()
        super()._changed(field, old, value)

    def _popup_press_passthrough(self, event):
        return event.x < self.bounds.right - 30

    def handle_input(self, e, /):
        opener = (e.kind == "pointer_down" and e.x >= self.bounds.right - 30) or (
            e.kind == "key_down" and e.key == "ArrowDown"
        )
        if opener and self.items and not self.read_only and self.effective_enabled:
            self.open()
        else:
            super().handle_input(e)

    def paint(self, p, /):
        super().paint(p)
        s = p.style()
        p.rect(
            Rect(max(0, p.width - 30), 1, 29, max(0, p.height - 2)),
            s.fill,
            radius=s.radius,
        )
        _arrow(p)


# Keep public imports, diagnostics, and reflection stable across source moves.
Dropdown.__module__ = "pysual.selection"
ComboBox.__module__ = "pysual.selection"
