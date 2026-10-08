"""Tabs and split panes own arrangement through the ordinary container hooks."""

from typing import ClassVar, Literal, cast

from ._text_display import single_line
from .controls import Container
from .events import ChangeEvent, Event
from .geometry import Rect
from .icons import icon_names
from .painting import resolve_style
from .schema import Dirty, prop


class TabPage(Container):
    """A disabled page keeps its current selection but cannot be selected by input."""
    title: str = prop(default="Page", affects=Dirty.MEASURE)
    icon: str = prop(default="", doc="Optional catalog icon in the tab header")

    def _changed(self, field, old, value):
        super()._changed(field, old, value)
        # The header belongs to TabControl, including headers of hidden pages.
        # Edits inside a page otherwise remain local to that page's subtree.
        if field.name in ("title", "icon", "enabled") and isinstance(self._parent, TabControl):
            self._parent.invalidate()

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "icon" and value and value not in icon_names():
            raise ValueError(f"Unknown tab icon {value!r}")
        if (
            name == "visible"
            and isinstance(self.parent, TabControl)
            and not self.parent._syncing
        ):
            raise ValueError(
                "TabControl owns page visibility; set selected_index instead"
            )

    def destroy(self):
        if self._disposed:
            return
        parent = self.parent
        selected = (
            parent.children[parent.selected_index]
            if isinstance(parent, TabControl)
            and not parent._destroying
            and parent.children
            else None
        )
        super().destroy()
        if isinstance(parent, TabControl) and not parent._destroying:
            parent._pages_changed(selected)


class TabControl(Container):
    cache_paint: bool = prop(default=True)
    selected_index: int = prop(default=0, minimum=0, changed="changed", persist=False)
    header_height: float = prop(default=36.0, minimum=24, affects=Dirty.MEASURE)
    tab_width: float = prop(default=140.0, minimum=40, affects=Dirty.MEASURE)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[int]]] = Event(ChangeEvent)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "tab", "indicator")

    def _initialize(self):
        super()._initialize()
        self._syncing = False
        self._destroying = False
        self._scroll = 0
        self._header_capacity = None
        self._reveal_header = True

    @property
    def pages(self) -> tuple[TabPage, ...]:
        return cast(tuple[TabPage, ...], self.children)

    def add(self, control):
        if not isinstance(control, TabPage):
            raise TypeError(
                "Add TabPage children to TabControl, then add controls to each page"
            )
        result = super().add(control)
        self._pages_changed()
        return result

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "selected_index" and value >= max(1, len(self.children)):
            raise ValueError("Selected tab index is outside the pages")
        if name == "selected_index":
            runtime = getattr(self._root(), "_runtime", None)
            modality = getattr(runtime, "_modality", None)
            if modality is not None:
                for index, page in enumerate(self.children):
                    modality.validate_visibility(page, index == value)

    def _pages_changed(self, selected=None):
        self.selected_index = (
            self.children.index(selected)
            if selected in self.children
            else min(self.selected_index, max(0, len(self.children) - 1))
        )
        self._sync_pages()

    def _sync_pages(self):
        self._syncing = True
        try:
            for index, page in enumerate(self.children):
                page.visible = index == self.selected_index
        finally:
            self._syncing = False
        self.invalidate(Dirty.MEASURE | Dirty.HIT_TEST)

    def _changed(self, field, old, value):
        if field.name == "selected_index":
            self._reveal_header = True
            self._sync_pages()
        super()._changed(field, old, value)

    def _header_height(self):
        return self.effective_row_height(self.header_height)

    def content_bounds(self, bounds):
        area = super().content_bounds(bounds)
        return Rect(
            area.x,
            area.y + self._header_height(),
            area.width,
            max(0, area.height - self._header_height()),
        )

    def arrange_children(self, area, measure):
        return {page: area for page in self.children if page.visible}

    def measure(self, host):
        return 400, 260

    def _headers(self):
        capacity = max(1, int(self.bounds.width / self.tab_width))
        if len(self.children) > capacity:
            capacity = max(1, int(max(0, self.bounds.width - 48) / self.tab_width))
        self._scroll = max(0, min(self._scroll, len(self.children) - capacity))
        if self._reveal_header or capacity != self._header_capacity:
            self._scroll = min(self._scroll, self.selected_index)
            if self.selected_index >= self._scroll + capacity:
                self._scroll = self.selected_index - capacity + 1
        self._header_capacity = capacity
        self._reveal_header = False
        return capacity

    def scroll_input(self, e, /) -> bool:
        if (self.bounds.y <= e.y < self.bounds.y + self._header_height()
                and len(self.children) > self._headers()):
            previous = self._scroll
            self.handle_input(e)
            return self._scroll != previous
        return False

    def handle_input(self, e, /):
        if not self.children:
            return
        capacity = self._headers()
        if (e.kind == "wheel" and self.bounds.y <= e.y < self.bounds.y + self._header_height()
                and len(self.children) > capacity):
            previous = self._scroll
            self._scroll = max(0, min(len(self.children) - capacity,
                                     self._scroll + (1 if e.delta > 0 else -1 if e.delta < 0 else 0)))
            if self._scroll != previous:
                self.invalidate()
        elif e.kind == "pointer_down" and e.y < self.bounds.y + self._header_height():
            x = e.x - self.bounds.x
            if len(self.children) > capacity and x >= self.bounds.width - 48:
                previous = self._scroll
                self._scroll = max(
                    0,
                    min(
                        len(self.children) - capacity,
                        self._scroll + (-1 if x < self.bounds.width - 24 else 1),
                    ),
                )
                if previous != self._scroll:
                    self.invalidate()
            else:
                index = self._scroll + int(x / self.tab_width)
                if (
                    0 <= index < min(len(self.children), self._scroll + capacity)
                    and self.pages[index].enabled
                ):
                    self.selected_index = index
        elif e.kind == "key_down" and e.key in (
            "ArrowLeft",
            "ArrowRight",
            "Home",
            "End",
        ):
            indices = (
                range(len(self.pages)) if e.key == "Home"
                else range(len(self.pages) - 1, -1, -1) if e.key == "End"
                else range(self.selected_index - 1, -1, -1) if e.key == "ArrowLeft"
                else range(self.selected_index + 1, len(self.pages))
            )
            for index in indices:
                if self.pages[index].enabled:
                    self._reveal_header = True
                    self.selected_index = index
                    break

    def paint(self, p, /):
        p.body()
        capacity = self._headers()
        available = p.width - (48 if len(self.children) > capacity else 0)
        for index in range(
            self._scroll, min(len(self.children), self._scroll + capacity)
        ):
            x = (index - self._scroll) * self.tab_width
            width = min(self.tab_width, available - x)
            page = self.pages[index]
            style = (
                p.style("tab", selected=index == self.selected_index)
                if page.enabled else resolve_style(
                    self, "tab", selected=index == self.selected_index, state="disabled"
                )
            )
            p.surface(Rect(x, 0, max(0, width), self._header_height()), style)
            icon_space = 24 if page.icon else 0
            if page.icon:
                size = max(0, min(
                    self.effective_row_height(min(18, self._header_height() - 8)),
                    width - 20,
                ))
                if size:
                    p.icon(
                        page.icon,
                        x + 10,
                        (self._header_height() - size) / 2,
                        size=size,
                        color=style.foreground,
                    )
            title = p.elide(
                single_line(page.title),
                width - 20 - icon_space,
                size=style.font_size,
                font_family=style.font_family,
            )
            p.text(
                title,
                x + 10 + icon_space,
                (self._header_height() - self.effective_row_height(style.font_size * 1.2)) / 2,
                color=style.foreground,
                size=style.font_size,
                font_family=style.font_family,
            )
            if index == self.selected_index:
                indicator = p.style("indicator")
                thickness = min(self._header_height(), indicator.border_width or 0)
                if thickness:
                    p.rect(
                        Rect(
                            x, self._header_height() - thickness, max(0, width), thickness
                        ),
                        indicator.fill,
                    )
        if len(self.children) > capacity:
            y = 0 if self.effective_row_height(0) else 3
            p.text("‹", p.width - 40, y, size=22)
            p.text("›", p.width - 18, y, size=22)

    def destroy(self):
        self._destroying = True
        super().destroy()


class SplitPane(Container):
    cache_paint: bool = prop(default=True)
    orientation: Literal["horizontal", "vertical"] = prop(
        default="horizontal", affects=Dirty.MEASURE
    )
    position: float = prop(
        default=0.5, minimum=0, affects=Dirty.MEASURE, changed="changed"
    )
    divider_size: float = prop(default=6.0, minimum=4, affects=Dirty.MEASURE)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[float]]] = Event(ChangeEvent)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "divider")

    def _initialize(self):
        super()._initialize()
        self._drag = None
        self._divider = Rect()

    def _paint_layout_key(self):
        # Child visibility and size constraints can move/remove the divider
        # without changing the pane's own properties or outer rectangle.
        return self._divider

    @property
    def first(self) -> Container:
        self._check_live()
        if not self.children:
            self.add(Container())
        return cast(Container, self.children[0])

    @property
    def second(self) -> Container:
        self._check_live()
        _ = self.first
        if len(self.children) == 1:
            self.add(Container())
        return cast(Container, self.children[-1])

    def add(self, control):
        if not isinstance(control, Container) or len(self.children) >= 2:
            raise TypeError(
                "SplitPane takes two containers; add content to split.first or split.second"
            )
        return super().add(control)

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "position" and value > 1:
            raise ValueError("SplitPane.position must be between 0 and 1")

    def arrange_children(self, area, measure):
        children = [child for child in self.children if child.visible]
        self._divider = Rect()
        if len(children) < 2:
            return {child: area for child in children}
        horizontal = self.orientation == "horizontal"
        length = area.width if horizontal else area.height
        divider = min(length, self.divider_size)
        available = max(0, length - divider)
        a, b = children
        first = available * self._effective_position(self.position, available)
        if horizontal:
            self._divider = Rect(area.x + first, area.y, divider, area.height)
            return {
                a: Rect(area.x, area.y, first, area.height),
                b: Rect(
                    area.x + first + divider, area.y, available - first, area.height
                ),
            }
        self._divider = Rect(area.x, area.y + first, area.width, divider)
        return {
            a: Rect(area.x, area.y, area.width, first),
            b: Rect(area.x, area.y + first + divider, area.width, available - first),
        }

    def measure(self, host):
        return 400, 260

    def _effective_position(self, position, available):
        children = [child for child in self.children if child.visible]
        if len(children) < 2 or available <= 0:
            return max(0, min(1, position))
        horizontal = self.orientation == "horizontal"
        a, b = children
        amin = a.min_width if horizontal else a.min_height
        bmin = b.min_width if horizontal else b.min_height
        if amin + bmin > available:
            return amin / (amin + bmin)
        amax = a.max_width if horizontal else a.max_height
        bmax = b.max_width if horizontal else b.max_height
        lower = max(amin, available - bmax) if bmax is not None else amin
        upper = min(available - bmin, amax) if amax is not None else available - bmin
        if lower <= upper:
            return max(lower / available, min(upper / available, position))
        # Incompatible caps retain the established minimum-size fallback.
        return max(amin / available, min(1 - bmin / available, position))

    def handle_input(self, e, /):
        if e.kind == "pointer_down" and self._divider.contains(e.x, e.y):
            self._drag = True
        elif e.kind in ("pointer_up", "blur"):
            self._drag = None
        elif e.kind == "pointer_move" and self._drag:
            area = self.content_bounds(self.bounds)
            horizontal = self.orientation == "horizontal"
            offset = (e.x - area.x) if horizontal else (e.y - area.y)
            length = area.width if horizontal else area.height
            available = max(0, length - self.divider_size)
            self.position = self._effective_position(
                (offset - self.divider_size / 2) / max(1, available), available,
            )
        elif e.kind == "key_down" and e.key in (
            "ArrowLeft",
            "ArrowRight",
            "ArrowUp",
            "ArrowDown",
            "Home",
            "End",
        ):
            area = self.content_bounds(self.bounds)
            length = area.width if self.orientation == "horizontal" else area.height
            available = max(0, length - self.divider_size)
            current = self._effective_position(self.position, available)
            self.position = self._effective_position(
                0.0
                if e.key == "Home"
                else 1.0
                if e.key == "End"
                else max(
                    0,
                    min(
                        1,
                        current
                        + (-0.05 if e.key in ("ArrowLeft", "ArrowUp") else 0.05),
                    ),
                ),
                available,
            )

    def paint(self, p, /):
        super().paint(p)
        if len([child for child in self.children if child.visible]) == 2:
            r = self._divider
            p.rect(
                Rect(r.x - self.bounds.x, r.y - self.bounds.y, r.width, r.height),
                p.style("divider").border,
            )
