"""Selectable cards and expandable sections built on ordinary control contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Literal

from ._text_display import single_line
from .behaviors import PressBehavior
from .controls import Container, Control
from .errors import BindingError
from .events import ChangeEvent, Event
from .geometry import Rect
from .icons import icon_names
from .painting import resolve_style
from .schema import Dirty, _init_argument, prop


def _line(painter, text, area, style):
    """A single, clipped line that respects the chosen theme's font metrics."""
    if area.width <= 0 or area.height <= 0:
        return
    with painter._clipped(area):
        text = painter.elide(single_line(text), area.width, size=style.font_size,
                             font_family=style.font_family)
        _, height = painter.measure(text, size=style.font_size,
                                    font_family=style.font_family)
        painter.text(text, area.x, area.y + max(0, (area.height - height) / 2),
                     size=style.font_size, color=style.foreground,
                     font_family=style.font_family)


def _details(painter, text, area, style):
    """Wrap a tile's explanation into at most three measured, clipped lines."""
    if not text or area.width <= 0 or area.height <= 0:
        return
    height = painter.measure("Ag", size=style.font_size,
                             font_family=style.font_family)[1]
    count = min(3, int((area.height + 3) / (height + 3)))
    remaining = single_line(text).strip()
    for index in range(count):
        fitted = painter.elide(remaining, area.width, size=style.font_size,
                               font_family=style.font_family)
        if fitted == remaining or index == count - 1:
            line = fitted
        else:
            # Elision already respects grapheme boundaries. Prefer a word
            # break when it leaves useful text; long words still make progress.
            prefix = fitted.removesuffix("…")
            boundary = prefix.rfind(" ")
            line = prefix[:boundary] if boundary > 0 else prefix
        if not line:
            break
        _line(painter, line, Rect(area.x, area.y + index * (height + 3),
                                 area.width, height), style)
        if fitted == remaining or index == count - 1:
            break
        remaining = remaining[len(line):].lstrip()


class ChoiceCard(Control):
    """An exclusive sibling choice with an icon, title and short explanation.

    Cards with the same ``group`` and parent are exclusive, including the
    default empty group. Enter/Space selects on release; arrows wrap through
    available peers and Home/End select the first/last. ``read_only`` prevents
    user selection while allowing programmatic changes to ``checked``.
    ``orientation="vertical"`` places the icon above the title and wraps the
    explanation into up to three lines, suitable for a grid of option tiles.
    """

    cache_paint: bool = prop(default=True)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    text: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    description: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    orientation: Literal["horizontal", "vertical"] = prop(
        default="horizontal", affects=Dirty.MEASURE | Dirty.PAINT,
    )
    icon: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    checked: bool = prop(default=False, changed="changed")
    group: str = prop(default="")
    read_only: bool = prop(default=False)
    changed: ClassVar[Event[ChangeEvent[bool]]] = Event(ChangeEvent)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("Button",)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "icon", "description", "check")

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior()

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "icon" and value and value not in icon_names():
            raise ValueError(f"Unknown icon {value!r}")

    def _exclusive(self):
        if self.checked and self._parent is not None:
            for sibling in self._parent.children:
                if (isinstance(sibling, ChoiceCard) and sibling is not self
                        and sibling.group == self.group and sibling.checked):
                    previous = sibling._origin
                    sibling._origin = self._origin
                    try:
                        sibling.checked = False
                    finally:
                        sibling._origin = previous

    def on_attached(self):
        self._exclusive()

    def _changed(self, field, old, value):
        self._press.reset(self)
        if field.name in ("checked", "group"):
            self._exclusive()
        super()._changed(field, old, value)

    def select(self) -> None:
        """Select this card when enabled and writable; never toggle it off."""
        self._check_live()
        if self.effective_enabled and not self.read_only:
            self.checked = True

    def handle_input(self, event, /):
        self._check_live()
        if not self.effective_enabled or self.read_only:
            self._press.reset(self)
            return
        if event.kind == "key_down" and event.key in (
            "ArrowLeft", "ArrowUp", "ArrowRight", "ArrowDown", "Home", "End",
        ):
            self._press.reset(self)
            peers = [child for child in self._parent.children
                     if isinstance(child, ChoiceCard) and child.group == self.group
                     and child.visible and child.focusable and child.effective_enabled and not child.read_only
                     ] if self._parent is not None else [self]
            if self not in peers:
                return
            index = (0 if event.key == "Home" else len(peers) - 1 if event.key == "End"
                     else (peers.index(self) + (-1 if event.key in ("ArrowLeft", "ArrowUp")
                                               else 1)) % len(peers))
            target = peers[index]
            previous = target._origin
            target._origin = self._origin
            try:
                if getattr(self._root(), "_runtime", None) is not None:
                    target.focus()
                target.select()
            finally:
                target._origin = previous
            return
        if self._press.handle_input(self, event):
            self.select()

    def measure(self, host):
        style, description = resolve_style(self), resolve_style(self, "description")
        title_width, title_height = host.measure(single_line(self.text), style.font_size,
                                                 style.font_family == "mono")
        detail_width, detail_height = host.measure(single_line(self.description),
            description.font_size, description.font_family == "mono")
        padding = max(12, style.padding)
        if self.orientation == "vertical":
            # Keep a useful tile width even when the explanation is long. The
            # fixed line budget keeps measuring independent of caption length.
            return max(160, min(260, max(title_width, detail_width) + padding * 2)), max(
                144, padding * 2 + (48 if self.icon else 30) + title_height
                + (5 + detail_height * 3 + 6 if self.description else 0),
            )
        text_height = title_height + (detail_height + 5 if self.description else 0)
        height = max(36 if self.icon else 20, text_height) + padding * 2
        return max(180, max(title_width, detail_width) + padding * 2 + 30
                   + (48 if self.icon else 0)), max(72, height)

    def paint(self, painter, /):
        style = painter.style(selected=self.checked)
        bounds = Rect(0, 0, painter.width, painter.height)
        painter.surface(bounds, style)
        padding = min(max(12, style.padding), painter.width / 2, painter.height / 2)
        area = bounds.inset(padding)
        with painter._clipped(area):
            if self.orientation == "vertical":
                self._paint_vertical(painter, area, style)
                return
            check_size = min(20, area.width, area.height)
            check = Rect(area.right - check_size, area.y + (area.height - check_size) / 2,
                         check_size, check_size)
            painter.marker(check, painter.style("check", selected=self.checked),
                           checked=self.checked)
            right = max(area.x, check.x - 10)
            left = area.x
            if self.icon:
                icon_size = min(32, max(0, right - left), area.height)
                icon_style = painter.style("icon", selected=self.checked)
                icon_area = Rect(left, area.y + (area.height - icon_size) / 2,
                                 icon_size, icon_size)
                painter.surface(icon_area, icon_style)
                painter.icon(self.icon, left, area.y + (area.height - icon_size) / 2,
                             size=icon_size, color=icon_style.foreground)
                left = min(right, left + icon_size + 12)
            detail = painter.style("description", selected=self.checked)
            title_height = painter.measure(single_line(self.text), size=style.font_size,
                                            font_family=style.font_family)[1]
            detail_height = (painter.measure(single_line(self.description), size=detail.font_size,
                                            font_family=detail.font_family)[1]
                             if self.description else 0)
            total = title_height + (detail_height + 5 if self.description else 0)
            top = area.y + max(0, (area.height - total) / 2)
            _line(painter, self.text, Rect(left, top, max(0, right - left), title_height), style)
            if self.description:
                _line(painter, self.description,
                      Rect(left, top + title_height + 5, max(0, right - left), detail_height), detail)

    def _paint_vertical(self, painter, area, style):
        check_size = min(20, area.width, area.height)
        check = Rect(area.right - check_size, area.y, check_size, check_size)
        painter.marker(check, painter.style("check", selected=self.checked),
                       checked=self.checked)
        top = area.y + check_size + 10
        if self.icon:
            size = min(40, max(0, area.width - check_size - 10), area.height)
            icon_area = Rect(area.x, area.y, size, size)
            icon_style = painter.style("icon", selected=self.checked)
            painter.surface(icon_area, icon_style)
            inset = min(7, size / 2)
            if size > 0:
                painter.icon(self.icon, icon_area.x + inset, icon_area.y + inset,
                             size=max(0, size - inset * 2), color=icon_style.foreground)
            top = area.y + max(size, check_size) + 10
        title_height = painter.measure(single_line(self.text), size=style.font_size,
                                        font_family=style.font_family)[1]
        _line(painter, self.text, Rect(area.x, top, area.width, title_height), style)
        top += title_height + 5
        _details(painter, self.description,
                 Rect(area.x, top, area.width, max(0, area.bottom - top)),
                 painter.style("description", selected=self.checked))

    def destroy(self):
        self._press.reset(self)
        super().destroy()


class _DisclosureHeader(Control):
    """A normal focus/gesture target; the containing section owns its state."""

    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior()

    def _part_style(self, part):
        state = ("disabled" if not self.effective_enabled else "pressed" if self._pressed
                 else "hover" if self._hover else "normal")
        return resolve_style(self._parent, part, selected=self._parent.expanded, state=state)

    def measure(self, host):
        owner = self._parent
        style, summary = self._part_style("header"), self._part_style("summary")
        width, height = host.measure(single_line(owner.title), style.font_size,
                                     style.font_family == "mono")
        if owner.summary:
            sw, sh = host.measure(single_line(owner.summary), summary.font_size,
                                  summary.font_family == "mono")
            width, height = max(width, sw), height + sh + 4
        padding = max(10, style.padding)
        return max(160, width + 28 + padding * 2), max(40, height + padding * 2)

    def handle_input(self, event, /):
        self._check_live()
        if not self.effective_enabled:
            self._press.reset(self)
            return
        owner = self._parent
        previous = owner._origin
        owner._origin = self._origin
        try:
            if event.kind == "key_down" and event.key in ("ArrowLeft", "ArrowRight"):
                self._press.reset(self)
                owner.expanded = event.key == "ArrowRight"
            elif self._press.handle_input(self, event):
                owner.toggle_expanded()
        finally:
            owner._origin = previous

    def paint(self, painter, /):
        owner = self._parent
        style = self._part_style("header")
        bounds = Rect(0, 0, painter.width, painter.height)
        with painter._clipped(bounds):
            painter.surface(bounds, style)
            padding = min(max(10, style.padding), painter.width / 2, painter.height / 2)
            area = bounds.inset(padding)
            size = min(18, area.width, area.height)
            painter.icon("chevron_down" if owner.expanded else "chevron_right",
                         area.right - size, area.y + (area.height - size) / 2,
                         size=size, color=self._part_style("chevron").foreground)
            width = max(0, area.width - size - 12)
            summary = self._part_style("summary")
            height = painter.measure(single_line(owner.title), size=style.font_size,
                                     font_family=style.font_family)[1]
            detail = (painter.measure(single_line(owner.summary), size=summary.font_size,
                                      font_family=summary.font_family)[1] if owner.summary else 0)
            top = area.y + max(0, (area.height - height - (detail + 4 if detail else 0)) / 2)
            _line(painter, owner.title, Rect(area.x, top, width, height), style)
            if owner.summary:
                _line(painter, owner.summary, Rect(area.x, top + height + 4, width, detail), summary)

    def destroy(self):
        self._press.reset(self)
        super().destroy()


class Disclosure(Container):
    """A heading that reveals an owned container without discarding its state.

    Add children to ``content``, a normal vertical-stack Container. An optional
    detached control passed as ``content=`` is adopted inside it. Ownership
    transfers only after properties validate. Collapsing hides the entire
    subtree from paint, picking and tab navigation, preserving child visibility.
    Enter/Space toggles on release; Left collapses and Right expands.
    """

    title: str = prop(default="Details", affects=Dirty.MEASURE | Dirty.PAINT)
    summary: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    expanded: bool = prop(default=False, affects=Dirty.MEASURE | Dirty.HIT_TEST | Dirty.PAINT,
                          changed="changed")
    changed: ClassVar[Event[ChangeEvent[bool]]] = Event(ChangeEvent)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "header", "summary", "chevron")
    _contained_style_parts: ClassVar[tuple[str, ...]] = ("header", "summary", "chevron")

    if TYPE_CHECKING:
        _content_input: Control | None = _init_argument(default=None, alias="content")
    else:
        def __init__(self, *, content: Control | None = None, **properties):
            if content is not None:
                if not isinstance(content, Control):
                    raise TypeError("content must be a detached Control or None")
                content._check_live()
                if content._parent is not None or getattr(content, "_is_app", False):
                    raise BindingError("Disclosure content must be detached and cannot be an App")
            try:
                super().__init__(**properties)
                if content is not None:
                    self._content.add(content)
            except BaseException:
                if hasattr(self, "_children"):
                    self.destroy()
                raise

    def _initialize(self):
        super()._initialize()
        self._header = _DisclosureHeader(parent=self)
        self._content = Container(parent=self, layout="stack", padding=16, spacing=12,
                                  visible=self.expanded)

    def _validate_live_update(self, name, value):
        super()._validate_live_update(name, value)
        if name == "expanded":
            # A modal dialog may own, or live inside, this subtree. Preflight
            # its visibility while the section's complete model is unchanged.
            self._content._validate_candidate({"visible": value})

    @property
    def content(self) -> Container:
        """The section's owned content container, available even when collapsed."""
        self._check_live()
        return self._content

    def _changed(self, field, old, value):
        self._header._press.reset(self._header)
        if field.name == "expanded":
            runtime = getattr(self._root(), "_runtime", None)
            focused = runtime.router.focus if runtime is not None else None
            node = focused
            while node is not None and node is not self._content:
                node = node._parent
            if not value and node is self._content:
                self._header.focus()
            self._content.visible = value
        if field.name in ("title", "summary", "expanded", "font_size", "font_family",
                          "foreground", "background", "theme_override", "enabled"):
            self._header.invalidate(Dirty.MEASURE | Dirty.PAINT)
        super()._changed(field, old, value)

    def toggle_expanded(self) -> None:
        """Expand or collapse this section when enabled."""
        self._check_live()
        if self.effective_enabled:
            self.expanded = not self.expanded

    def focus(self) -> None:
        """Focus the section heading."""
        self._check_live()
        self._header.focus()

    @property
    def focused(self) -> bool:
        self._check_live()
        return self._header.focused

    def handle_input(self, event, /):
        self._check_live()
        previous = self._header._origin
        self._header._origin = self._origin
        try:
            self._header.handle_input(event)
        finally:
            self._header._origin = previous

    def measure(self, host):
        return self.measure_available(host)

    def measure_available(self, host, *, width=None, height=None):
        from .layout import _size

        inner_width = None if width is None else max(0, width - self.padding * 2)
        hw, hh = _size(self._header, host, width=inner_width)
        cw, ch = _size(self._content, host, width=inner_width) if self.expanded else (0, 0)
        return max(hw, cw) + self.padding * 2, hh + ch + self.padding * 2

    def arrange_children(self, area, measure):
        header_height = min(area.height, measure(self._header)[1])
        boxes = {self._header: Rect(area.x, area.y, area.width, header_height)}
        if self._content.visible:
            boxes[self._content] = Rect(area.x, area.y + header_height, area.width,
                                        max(0, area.height - header_height))
        # Other explicitly added children remain ordinary children. Applications
        # should mount settings in content to participate in expansion.
        for child in self._children:
            if child not in (self._header, self._content) and child.visible and not child._overlay:
                w, h = measure(child)
                boxes[child] = Rect(area.x + child.left, area.y + child.top, w, h)
        return boxes

    def paint(self, painter, /):
        painter.body()


__all__ = ["ChoiceCard", "Disclosure"]
