"""Menu commands use owned events and the shared Popup control subtree."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from ._text_display import single_line
from .commands import (
    CommandEvent,
    MenuGroup,
    MenuItem,
    normalize_shortcut,
    validate_items,
)
from .controls import Control
from .events import Event
from .geometry import Rect
from .painting import resolve_style
from .popup import Popup
from .schema import Dirty, prop
from .widgets import ListView


class _MenuRows(ListView):
    style_parts: ClassVar[tuple[str, ...]] = (
        "body",
        "row",
        "separator",
        "track",
        "thumb",
    )
    _contained_style_parts: ClassVar[frozenset[str]] = frozenset(
        ("row", "separator", "track", "thumb")
    )

    def _initialize(self):
        super()._initialize()
        self._entries: tuple[MenuItem, ...] = ()
        self._commit: Callable[[MenuItem], None] | None = None
        self._switch: Callable[[int], None] | None = None
        self._hover_item: Callable[[MenuItem], None] | None = None
        self._scrolled: Callable[[], None] | None = None
        self._down = -1

    def _enabled(self, index):
        return (
            0 <= index < len(self._entries)
            and self._entries[index].enabled
            and not self._entries[index].separator
        )

    def handle_input(self, e, /):
        scrollbar = self._scrollbar()
        if (
            e.kind in ("wheel", "blur", "pointer_cancel")
            or self._scroll_drag is not None
            or (
                e.kind.startswith("pointer")
                and scrollbar is not None
                and scrollbar[0].contains(e.x, e.y)
            )
        ):
            self._down = -1
            previous_scroll = self._scroll
            super().handle_input(e)
            if self._scroll != previous_scroll and self._scrolled is not None:
                self._scrolled()
        elif e.kind in ("pointer_down", "pointer_move", "pointer_up"):
            index = self._scroll + int((e.y - self.bounds.y) / self._row_height())
            if not self.bounds.contains(e.x, e.y):
                if e.kind == "pointer_up":
                    self._down = -1
                return
            if e.kind == "pointer_up":
                if index == self._down and self._enabled(index):
                    assert self._commit is not None
                    self._commit(self._entries[index])
                self._down = -1
            elif self._enabled(index):
                self.selected_index = index
                if e.kind == "pointer_down":
                    self._down = index
                elif self._hover_item is not None:
                    self._hover_item(self._entries[index])
        elif e.kind == "key_down":
            previous = self.selected_index
            if e.key in ("ArrowLeft", "ArrowRight") and self._switch is not None:
                self._switch(-1 if e.key == "ArrowLeft" else 1)
                return
            if e.key in ("Enter", "Space") and self._enabled(self.selected_index):
                assert self._commit is not None
                self._commit(self._entries[self.selected_index])
                return
            candidates = [i for i in range(len(self.items)) if self._enabled(i)]
            if not candidates:
                return
            if e.key in ("Home", "End", "ArrowUp", "ArrowDown"):
                position = (
                    candidates.index(self.selected_index)
                    if self.selected_index in candidates
                    else -1
                )
                position = (
                    0
                    if e.key == "Home"
                    else len(candidates) - 1
                    if e.key == "End"
                    else (position + (-1 if e.key == "ArrowUp" else 1))
                    % len(candidates)
                )
                self.selected_index = candidates[position]
            elif len(e.key) == 1 and not e.ctrl:
                matches = [
                    i
                    for i in candidates
                    if self.items[i].casefold().startswith(e.key.casefold())
                ]
                if matches:
                    self.selected_index = next(
                        (i for i in matches if i > self.selected_index), matches[0]
                    )
            count = self._visible_rows()
            self._scroll = max(0, min(self._scroll, self.selected_index))
            if self.selected_index >= self._scroll + count:
                self._scroll = self.selected_index - count + 1
            if previous != self.selected_index and self._hover_item is not None:
                self._hover_item(self._entries[self.selected_index])

    def paint(self, p, /):
        p.body()
        self._clamp_scroll()
        scrollbar = self._scrollbar()
        width = max(0, p.width - (scrollbar[0].width if scrollbar else 0))
        with p._clipped(Rect(0, 0, width, p.height)):
            self._paint_entries(p, width)
        if scrollbar is not None:
            for box, part in zip(scrollbar, ("track", "thumb")):
                p.surface(
                    Rect(
                        box.x - self.bounds.x,
                        box.y - self.bounds.y,
                        box.width,
                        box.height,
                    ),
                    p.style(part),
                )

    def _paint_entries(self, p, width):
        row_height = self._row_height()
        fixed_height = self.effective_row_height(0)
        for index in range(
            self._scroll,
            min(len(self.items), self._scroll + int(p.height / row_height) + 1),
        ):
            item = self._entries[index]
            y = (index - self._scroll) * row_height
            if item.separator:
                p.line(
                    10,
                    y + row_height / 2,
                    width - 10,
                    y + row_height / 2,
                    p.style("separator").border,
                )
                continue
            s = p.style("row", selected=index == self.selected_index)
            p.surface(Rect(1, y, max(0, width - 2), row_height), s)
            color = s.foreground if item.enabled else p.theme.tokens.muted
            shortcut = normalize_shortcut(item.shortcut)
            shortcut_width = (
                p.measure(shortcut, size=12, font_family=s.font_family)[0]
                if shortcut
                else 0
            )
            chevron = 18 if item.children else 0
            icon_space = 24 if item.icon else 0
            if item.icon:
                size = max(0, min(
                    18, fixed_height or row_height - 8, width - shortcut_width - 56
                ))
                if size:
                    p.icon(
                        item.icon,
                        28,
                        y + (row_height - size) / 2,
                        size=size,
                        color=color,
                    )
            p.text(
                p.elide(
                    single_line(item.text),
                    width - shortcut_width - 56 - icon_space - chevron,
                    size=s.font_size,
                    font_family=s.font_family,
                ),
                28 + icon_space,
                y + (row_height - self.effective_row_height(s.font_size * 1.2)) / 2,
                color=color,
                size=s.font_size,
                font_family=s.font_family,
            )
            if shortcut:
                p.text(
                    shortcut,
                    width - shortcut_width - 12 - chevron,
                    y + ((row_height - fixed_height) / 2 if fixed_height else 8),
                    color=color
                    if index == self.selected_index
                    else p.theme.tokens.muted,
                    size=12,
                    font_family=s.font_family,
                )
            if item.checked:
                p.icon("check", 8, y + (row_height - 16) / 2, size=16, color=color)
            if item.children:
                p.icon(
                    "chevron_right",
                    width - 21,
                    y + (row_height - 16) / 2,
                    size=16,
                    color=color,
                )


class _CascadePopup(Popup):
    """Cascades are ordinary sibling rows inside the one active popup subtree."""

    def _initialize(self):
        super()._initialize()
        self._levels: list[_MenuRows] = []
        self._command_owner: Menu | MenuBar | None = None
        self._group_switch: Callable[[int], None] | None = None
        self._group_select: Callable[[int], None] | None = None

    def _trim(self, keep):
        for rows in self._levels[keep:]:
            rows.destroy()
        del self._levels[keep:]

    def handle_owner_input(self, event) -> bool:
        """Route active menu headers without opening the rest of the popup scope."""
        owner, runtime = self._command_owner, self._session
        if (
            not isinstance(owner, MenuBar)
            or runtime is None
            or event.kind not in ("pointer_move", "pointer_down")
            or (event.kind == "pointer_down" and event.button != 1)
            or not runtime.router._visible(owner)
            or any(rows._scroll_drag is not None for rows in self._levels)
            or not owner._clip.contains(event.x, event.y)
            or not any(box.contains(event.x, event.y)
                       for box in (*owner._headers, owner._overflow_rect))
        ):
            return False
        origin, owner._origin = owner._origin, "user"
        try:
            owner.handle_input(event)
        finally:
            owner._origin = origin
        return True

    def dismiss(self, *, restore_focus=True):
        owner = self._command_owner
        super().dismiss(restore_focus=restore_focus)
        if owner is not None and not owner._disposed:
            owner.invalidate()

    def _level(self, items):
        rows = self.add(
            _MenuRows(items=tuple(item.text for item in items), row_height=30)
        )
        rows._entries = items
        rows.selected_index = next(
            (i for i in range(len(items)) if rows._enabled(i)), -1
        )
        self._levels.append(rows)
        level = len(self._levels) - 1

        def choose(item, *, focus=True):
            if level == 0 and self._group_select is not None:
                self._group_select(rows._entries.index(item))
            unchanged = (
                len(self._levels) > level + 1
                and self._levels[level + 1]._entries is item.children
            )
            if unchanged:
                if focus:
                    self._levels[level + 1].focus()
                return
            if not focus and not item.children and len(self._levels) == level + 1:
                return
            self._trim(level + 1)
            if (
                not focus
                and self._session is not None
                and self._session.router.focus is None
            ):
                rows.focus()
            if item.children:
                child = self._level(item.children)
                self.reposition()
                if focus:
                    child.focus()
            elif focus:
                assert self._command_owner is not None
                self._command_owner.command.emit(
                    CommandEvent(
                        source=self._command_owner, key=item.key, origin="user"
                    )
                )
                self.dismiss()
                return
            self.invalidate(Dirty.MEASURE | Dirty.PAINT | Dirty.HIT_TEST)

        def switch(delta):
            if (
                delta > 0
                and rows._enabled(rows.selected_index)
                and rows._entries[rows.selected_index].children
            ):
                choose(rows._entries[rows.selected_index])
            elif delta < 0 and level:
                self._trim(level)
                self._levels[-1].focus()
                self.reposition()
            elif self._group_switch is not None:
                self._group_switch(delta)

        def scrolled():
            if len(self._levels) <= level + 1:
                return
            runtime = self._session
            restore_focus = runtime is not None and any(
                child is runtime.router.focus for child in self._levels[level + 1:]
            )
            self._trim(level + 1)
            if runtime is not None and restore_focus:
                runtime.router.set_focus(rows, reveal=False)
            self.reposition()
            self.invalidate(Dirty.MEASURE | Dirty.PAINT | Dirty.HIT_TEST)

        rows._commit = choose
        rows._switch = switch
        rows._hover_item = lambda item: choose(item, focus=False)
        rows._scrolled = scrolled
        return rows

    def handle_escape(self) -> bool:
        if len(self._levels) <= 1:
            return False
        self._trim(len(self._levels) - 1)
        self._levels[-1].focus()
        self.reposition()
        return True

    def reposition(self):
        runtime = self._session
        if runtime is None or not self._levels:
            return
        assert self._owner is not None
        area = runtime.app.content_bounds(runtime.app.bounds)
        panel_width = min(280, area.width)
        anchor = self._owner.bounds
        x, y = self._point if self._point is not None else (anchor.x, anchor.bottom)
        first = self._levels[0]
        height = min(area.height, first._row_height() * min(12, len(first.items)))
        if isinstance(self._command_owner, MenuBar):
            x = self._command_owner._popup_x(runtime.host)
            # Keep the owner's headings exposed while a long menu scrolls.
            below, above = (
                max(0, area.bottom - anchor.bottom),
                max(0, anchor.y - area.y),
            )
            height = min(height, max(below, above))
            y = anchor.bottom if below >= above else anchor.y - height
        elif self._point is None and y + height > area.bottom:
            y = anchor.y - height
        boxes = [
            Rect(
                max(area.x, min(x, area.right - panel_width)),
                max(area.y, min(y, area.bottom - height)),
                panel_width,
                height,
            )
        ]
        for index, rows in enumerate(self._levels[1:], 1):
            parent, box = self._levels[index - 1], boxes[-1]
            height = min(area.height, rows._row_height() * min(12, len(rows.items)))
            x = (
                box.right
                if box.right + panel_width <= area.right
                else box.x - panel_width
            )
            y = box.y + (parent.selected_index - parent._scroll) * parent._row_height()
            boxes.append(
                Rect(
                    max(area.x, min(x, area.right - panel_width)),
                    max(area.y, min(y, area.bottom - height)),
                    panel_width,
                    height,
                )
            )
        left, top = min(box.x for box in boxes), min(box.y for box in boxes)
        self.left, self.top = left - area.x, top - area.y
        self.width = max(box.right for box in boxes) - left
        self.height = max(box.bottom for box in boxes) - top
        for rows, box in zip(self._levels, boxes):
            rows.left, rows.top = box.x - left, box.y - top
            rows.width, rows.height = box.width, box.height
        children = runtime.app._children
        if children[-1] is not self:
            children.remove(self)
            children.append(self)


def _show(owner, items, anchor, at=None, switch=None):
    previous = getattr(owner, "_popup", None)
    if previous is not None and previous.is_open:
        previous.dismiss()
    if not items:
        return
    row_height = anchor.effective_row_height(30)
    popup = _CascadePopup(
        width=280, height=min(12, len(items)) * row_height, layout="absolute", spacing=0
    )
    popup._command_owner, popup._group_switch = owner, switch
    popup._level(items)
    owner._popup = popup
    try:
        popup.show(anchor, at=at)
    except BaseException:
        popup.destroy()
        raise


def _shortcut(owner, items, shortcut):
    for item in items:
        if item.enabled and item.children and _shortcut(owner, item.children, shortcut):
            return True
        if (
            item.enabled
            and not item.separator
            and item.shortcut
            and normalize_shortcut(item.shortcut) == shortcut
        ):
            owner.command.emit(
                CommandEvent(source=owner, key=item.key, origin=owner._origin)
            )
            return True
    return False


class Menu(Control):
    items: tuple[MenuItem, ...] = prop(default=(), persist=False)
    width: float | None = prop(default=0.0, minimum=0, affects=Dirty.MEASURE)
    height: float | None = prop(default=0.0, minimum=0, affects=Dirty.MEASURE)
    command: ClassVar[Event[CommandEvent]] = Event(CommandEvent)
    _overlay: ClassVar[bool] = True

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "items":
            validate_items(value)

    def _changed(self, field, old, value):
        if field.name == "items":
            popup = getattr(self, "_popup", None)
            if popup is not None and popup.is_open:
                popup.dismiss()
        super()._changed(field, old, value)

    def show(self, anchor: Control, *, at: tuple[float, float] | None = None) -> None:
        self._check_live()
        if self._root() is not anchor._root():
            raise ValueError("Menu and anchor must belong to the same App")
        if self.effective_enabled:
            _show(self, self.items, anchor, at)

    def invoke_shortcut(self, shortcut: str) -> bool:
        return _shortcut(self, self.items, shortcut)

    def destroy(self):
        popup = getattr(self, "_popup", None)
        if popup is not None and popup.is_open:
            popup.dismiss(restore_focus=False)
        super().destroy()


class MenuBar(Control):
    cache_paint: bool = prop(default=True)
    groups: tuple[MenuGroup, ...] = prop(default=(), persist=False)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    command: ClassVar[Event[CommandEvent]] = Event(CommandEvent)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "item")

    def _initialize(self):
        super()._initialize()
        self._headers = []
        self._active_group = 0
        self._overflow_start = 0
        self._overflow_rect = Rect(0, 0, 0, 0)
        self._overflow_open = False

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "groups":
            validate_items(item for group in value for item in group.items)

    def _changed(self, field, old, value):
        if field.name == "groups":
            popup = getattr(self, "_popup", None)
            if popup is not None and popup.is_open:
                popup.dismiss()
            self._active_group = 0
        super()._changed(field, old, value)

    def measure(self, host):
        return 400, self._control_height(host)

    def _open(self, index):
        if not self.groups:
            return
        self._active_group = index % len(self.groups)
        self._overflow_open = False
        _show(
            self,
            self.groups[self._active_group].items,
            self,
            switch=lambda delta: self._open(self._active_group + delta),
        )
        self.invalidate()

    def _open_overflow(self):
        items = tuple(
            MenuItem(f"overflow_{index}", group.text, enabled=bool(group.items),
                     children=group.items)
            for index, group in enumerate(self.groups)
            if index >= self._overflow_start
        )
        self._overflow_open = True
        _show(self, items, self,
              switch=lambda delta: self._open(self._active_group + delta))
        popup = getattr(self, "_popup", None)
        if popup is not None and popup.is_open:
            start = self._overflow_start

            def select_group(index):
                self._active_group = start + index
                self.invalidate()

            popup._group_select = select_group
            select_group(max(0, popup._levels[0].selected_index))
        self.invalidate()

    def invoke_shortcut(self, shortcut: str) -> bool:
        if _shortcut(
            self, (item for group in self.groups for item in group.items), shortcut
        ):
            return True
        if shortcut == "F10" and self.groups:
            self.focus()
            self._open(0)
            return True
        return False

    def handle_input(self, e, /):
        popup = getattr(self, "_popup", None)
        opened = popup is not None and popup.is_open
        if e.kind in ("focus", "blur"):
            self.invalidate()
        if (e.kind == "pointer_down" and e.button == 1) or (
            e.kind == "pointer_move" and opened
        ):
            if self._overflow_rect.contains(e.x, e.y):
                if popup is not None and opened and self._overflow_open:
                    if e.kind == "pointer_down":
                        popup.dismiss()
                else:
                    self._open_overflow()
                return
            for index, box in enumerate(self._headers):
                if box.contains(e.x, e.y):
                    if (popup is not None and opened and not self._overflow_open
                            and index == self._active_group):
                        if e.kind == "pointer_down":
                            popup.dismiss()
                    else:
                        self._open(index)
                    break
        elif e.kind == "key_down" and self.groups:
            if e.key in ("Enter", "Space", "ArrowDown"):
                self._open(self._active_group)
            elif e.key in ("ArrowLeft", "ArrowRight"):
                self._active_group = (
                    self._active_group + (-1 if e.key == "ArrowLeft" else 1)
                ) % len(self.groups)
                self.invalidate()

    def _layout_headers(self, host):
        self._headers = [Rect(0, 0, 0, 0) for _ in self.groups]
        popup = getattr(self, "_popup", None)
        active = self.focused or (popup is not None and popup.is_open)
        styles = [resolve_style(self, "item", selected=active and not self._overflow_open and index == self._active_group)
                  for index in range(len(self.groups))]
        widths = [
            host.measure(single_line(group.text), style.font_size, style.font_family == "mono")[0] + 24
            for group, style in zip(self.groups, styles)
        ]
        overflow = sum(widths) > self.bounds.width
        overflow_width = min(40, self.bounds.width) if overflow else 0
        available = max(0, self.bounds.width - overflow_width)
        x = 0.0
        self._overflow_start = len(self.groups)
        self._overflow_rect = Rect(0, 0, 0, 0)
        for index, width in enumerate(widths):
            if overflow and x + width > available:
                self._overflow_start = index
                break
            self._headers[index] = Rect(self.bounds.x + x, self.bounds.y, width, self.bounds.height)
            x += width
        if overflow:
            self._overflow_rect = Rect(self.bounds.x + x, self.bounds.y,
                                       overflow_width, self.bounds.height)

    def _popup_x(self, host):
        self._layout_headers(host)
        if not self._overflow_open and self._active_group < self._overflow_start:
            return self._headers[self._active_group].x
        return self._overflow_rect.x if self._overflow_rect.width else self.bounds.x

    def paint(self, p, /):
        p.body()
        self._layout_headers(p.host)
        popup = getattr(self, "_popup", None)
        active = self.focused or (popup is not None and popup.is_open)
        for index, group in enumerate(self.groups[:self._overflow_start]):
            box = self._headers[index]
            x, width = box.x - self.bounds.x, box.width
            style = p.style("item", selected=active and not self._overflow_open
                            and index == self._active_group)
            p.surface(Rect(x, 0, width, p.height), style)
            p.text(
                p.elide(
                    single_line(group.text),
                    width - 24,
                    size=style.font_size,
                    font_family=style.font_family,
                ),
                x + 12,
                (p.height - self.effective_row_height(style.font_size * 1.2)) / 2,
                color=style.foreground,
                size=style.font_size,
                font_family=style.font_family,
            )
        if self._overflow_rect.width:
            x = self._overflow_rect.x - self.bounds.x
            overflow_width = self._overflow_rect.width
            style = p.style("item", selected=active and (
                self._overflow_open or self._active_group >= self._overflow_start
            ))
            box = Rect(x, 0, overflow_width, p.height)
            p.surface(box, style)
            label = p.elide("…", overflow_width, size=style.font_size,
                            font_family=style.font_family)
            label_width, label_height = p.measure(label, size=style.font_size,
                                                  font_family=style.font_family)
            p.text(label, x + (overflow_width - label_width) / 2,
                   (p.height - label_height) / 2, color=style.foreground,
                   size=style.font_size, font_family=style.font_family)

    def destroy(self):
        popup = getattr(self, "_popup", None)
        if popup is not None and popup.is_open:
            popup.dismiss(restore_focus=False)
        super().destroy()
