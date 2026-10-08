"""Immutable tree data and a viewport-painted control, using ordinary UI hooks."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from time import monotonic
from types import EllipsisType
from typing import ClassVar, cast

from ._text_display import single_line
from ._scrolling import (
    can_scroll, clamp_scroll, typeahead_prefix,
    scrollbar, handle_scrollbars, paint_scrollbars,
)
from .controls import Control
from .events import ChangeEvent, Event, UiEvent
from .geometry import Rect
from .icons import icon_names
from .schema import Dirty, _persist_record, prop

_KEEP = Ellipsis


@_persist_record
@dataclass(frozen=True)
class TreeNode:
    key: str
    text: str
    children: tuple[TreeNode, ...] = ()
    icon: str = ""

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("TreeNode.key must be a nonempty string")
        if not isinstance(self.text, str):
            raise TypeError("TreeNode.text must be a string")
        if not isinstance(self.icon, str) or (
            self.icon and self.icon not in icon_names()
        ):
            raise ValueError("TreeNode.icon must be empty or a catalog icon name")
        if not isinstance(self.children, tuple) or any(
            not isinstance(child, TreeNode) for child in self.children
        ):
            raise TypeError("TreeNode.children must be a tuple of TreeNode objects")


@dataclass(frozen=True, kw_only=True)
class TreeEvent(UiEvent):
    key: str


def _index(nodes):
    by_key, parents = {}, {}
    pending = [(node, None) for node in reversed(nodes)]
    while pending:
        node, parent = pending.pop()
        if node.key in by_key:
            raise ValueError(f"Duplicate TreeNode key {node.key!r}; use unique keys")
        by_key[node.key], parents[node.key] = node, parent
        pending.extend((child, node.key) for child in reversed(node.children))
    return by_key, parents


class TreeView(Control):
    cache_paint: bool = prop(default=True)
    nodes: tuple[TreeNode, ...] = prop(default=())
    selected_key: str | None = prop(default=None, changed="changed")
    expanded_keys: tuple[str, ...] = prop(
        default=(), changed="expanded_changed"
    )
    row_height: float = prop(default=30.0, minimum=1, affects=Dirty.MEASURE)
    indent: float = prop(default=20.0, minimum=12, affects=Dirty.PAINT)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[str | None]]] = Event(ChangeEvent)
    expanded_changed: ClassVar[Event[ChangeEvent[tuple[str, ...]]]] = Event(ChangeEvent)
    activated: ClassVar[Event[TreeEvent]] = Event(
        TreeEvent, doc="Enter activates the selected node."
    )
    style_parts: ClassVar[tuple[str, ...]] = ("body", "row", "expander", "scrollbar", "track", "thumb")
    _contained_style_parts: ClassVar[frozenset[str]] = frozenset(
        ("row", "expander", "scrollbar", "track", "thumb")
    )
    _style_kind: ClassVar[str] = "TreeView"

    def _initialize(self):
        super()._initialize()
        self._applying_tree = False
        self._reconciling_nodes = False
        self._nodes, self._parents = _index(self.nodes)
        self._scroll = 0
        self._wheel = 0.0
        self._drag = None
        self._prefix, self._prefix_time = "", 0.0
        self._rebuild_rows()
        selected = self.selected_key
        if selected is not None and selected in self._nodes:
            self.reveal(selected)

    def _validation_copy(self, values):
        candidate = super()._validation_copy(values)
        if candidate.nodes is not self.nodes:
            candidate._nodes, candidate._parents = _index(candidate.nodes)
        return candidate

    def _prepare_update(self, values):
        values = dict(super()._prepare_update(values))
        if not values.keys() & {"nodes", "selected_key", "expanded_keys"}:
            return values
        self._check_live()
        nodes = values.get("nodes", self.nodes)
        self._schema["nodes"].validate(nodes, previous=self.nodes)
        by_key, parents = (
            _index(nodes) if nodes is not self.nodes else (self._nodes, self._parents)
        )
        selected = values.get(
            "selected_key", self.selected_key if self.selected_key in by_key else None
        )
        expanded = values.get(
            "expanded_keys",
            tuple(key for key in self.expanded_keys
                  if key in by_key and by_key[key].children),
        )
        self._schema["selected_key"].validate(selected)
        self._schema["expanded_keys"].validate(expanded)
        expansion = list(cast(tuple[str, ...], expanded))
        ancestor = parents.get(selected)
        while ancestor is not None:
            if ancestor not in expansion:
                if "nodes" in values or "selected_key" in values:
                    expansion.append(ancestor)
                else:
                    # Expansion-only updates keep ordinary collapse semantics.
                    selected = ancestor
            ancestor = parents[ancestor]
        values.update(selected_key=selected, expanded_keys=tuple(expansion))
        return values

    def _commit_update(self, properties):
        if not any(
            name in properties and properties[name] != self._values[name]
            for name in ("nodes", "selected_key", "expanded_keys")
        ):
            return super()._commit_update(properties)
        # Stage derived state on an unmounted candidate, then install it before
        # any changed hook can observe the newly committed public properties.
        candidate = self._validation_copy(properties)
        candidate._rebuild_rows()
        if candidate.selected_key is not None:
            candidate._reveal_row(candidate.selected_key)
        for name in ("_nodes", "_parents", "_rows", "_positions", "_expanded", "_scroll"):
            setattr(self, name, getattr(candidate, name))
        self._drag = None
        applying = self._applying_tree
        self._applying_tree = True
        try:
            super()._commit_update(properties)
        finally:
            self._applying_tree = applying

    def set_tree(
        self, nodes: tuple[TreeNode, ...], *,
        selected_key: str | None | EllipsisType = _KEEP,
        expanded_keys: tuple[str, ...] | EllipsisType = _KEEP,
    ) -> None:
        """Replace nodes atomically, retaining surviving selection and expansion keys.

        Explicit keys are checked against the new tree. A selected descendant
        reveals its ancestors, as with ordinary selected_key assignment.
        """
        values: dict[str, object] = {"nodes": nodes}
        if not isinstance(selected_key, EllipsisType):
            values["selected_key"] = selected_key
        if not isinstance(expanded_keys, EllipsisType):
            values["expanded_keys"] = expanded_keys
        self.update(**values)

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "nodes":
            # Construction and candidate copies already indexed their own nodes.
            if value is not self.nodes:
                _index(value)
        elif name == "selected_key":
            if value is not None and value not in self._nodes:
                raise ValueError(
                    f"Unknown selected_key {value!r}; use a key from nodes or None"
                )
        elif name == "expanded_keys":
            if len(value) != len(set(value)):
                raise ValueError("expanded_keys must not contain duplicates")
            for key in value:
                if key not in self._nodes or not self._nodes[key].children:
                    raise ValueError(
                        f"Cannot expand {key!r}; use the key of a branch in nodes"
                    )

    def _rebuild_rows(self):
        expanded = set(self.expanded_keys)
        self._expanded = expanded
        self._rows = []
        pending = [(node, 0) for node in reversed(self.nodes)]
        while pending:
            node, depth = pending.pop()
            self._rows.append((node, depth))
            if node.key in expanded:
                pending.extend((child, depth + 1) for child in reversed(node.children))
        self._positions = {node.key: i for i, (node, _) in enumerate(self._rows)}
        self._clamp_scroll()

    def _reconcile_selection(self):
        key = self.selected_key
        while key is not None and key not in self._positions:
            key = self._parents.get(key)
        self.selected_key = key

    def _changed(self, field, old, value):
        if self._applying_tree:
            super()._changed(field, old, value)
            return
        if field.name == "nodes":
            self._nodes, self._parents = _index(value)
            self._rebuild_rows()
            expanded = tuple(
                key
                for key in self.expanded_keys
                if key in self._nodes and self._nodes[key].children
            )
            # Removed branches cannot contribute visible rows. Keep the normal
            # descriptor/hooks, but avoid flattening the same rows a second time.
            self._expanded = set(expanded)
            reconciling = self._reconciling_nodes
            self._reconciling_nodes = True
            try:
                if expanded != self.expanded_keys:
                    self.expanded_keys = expanded
                else:
                    self._reconcile_selection()
            finally:
                self._reconciling_nodes = reconciling
        elif field.name == "expanded_keys":
            if not self._reconciling_nodes:
                self._rebuild_rows()
            self._reconcile_selection()
        elif field.name == "selected_key" and value is not None:
            self.reveal(value)
        super()._changed(field, old, value)

    @property
    def selected_node(self) -> TreeNode | None:
        self._check_live()
        return self._nodes.get(self.selected_key)

    def key_at(self, x: float, y: float) -> str | None:
        """Return the visible node at app-logical coordinates, excluding scrollbar."""
        self._check_live()
        if not self.bounds.contains(x, y) or not self._clip.contains(x, y):
            return None
        scrollbar = self._scrollbar()
        if scrollbar is not None and x >= self.bounds.x + scrollbar.x:
            return None
        row = self._scroll + int((y - self.bounds.y) / self._row_height())
        return self._rows[row][0].key if 0 <= row < len(self._rows) else None

    def toggle(self, key: str) -> None:
        self._check_live()
        if key not in self._nodes:
            raise KeyError(f"Unknown tree key {key!r}")
        if not self._nodes[key].children:
            return
        self.expanded_keys = (
            tuple(k for k in self.expanded_keys if k != key)
            if key in self.expanded_keys
            else (*self.expanded_keys, key)
        )

    def reveal(self, key: str) -> None:
        """Expand ancestors and scroll to a node without changing selection."""
        self._check_live()
        if key not in self._nodes:
            raise KeyError(f"Unknown tree key {key!r}")
        ancestors = []
        parent = self._parents[key]
        expanded = self._expanded
        while parent is not None:
            if parent not in expanded:
                ancestors.append(parent)
            parent = self._parents[parent]
        if ancestors:
            self.expanded_keys = (*self.expanded_keys, *reversed(ancestors))
        previous = self._scroll
        self._reveal_row(key)
        if self._scroll != previous:
            self.invalidate()

    def _reveal_row(self, key):
        index = self._positions[key]
        self._scroll = max(0, min(self._scroll, index))
        if index >= self._scroll + self._page_rows():
            self._scroll = index - self._page_rows() + 1

    def measure(self, host):
        return 240, 240

    def _row_height(self):
        return self.effective_row_height(self.row_height)

    def _page_rows(self):
        return max(1, int(self._rect.height / self._row_height()))

    def _clamp_scroll(self):
        self._scroll = clamp_scroll(self._scroll, len(self._rows), self._page_rows())

    def _scrollbars(self):
        track = Rect(max(self._rect.x, self._rect.right - 12), self._rect.y,
                     min(12, self._rect.width), self._rect.height)
        bar = scrollbar(track, len(self._rows), self._page_rows(), self._scroll)
        return [bar] if bar is not None else []

    def _scrollbar(self):
        bars = self._scrollbars()
        if not bars:
            return None
        thumb = bars[0].thumb
        return Rect(thumb.x - self._rect.x, thumb.y - self._rect.y, thumb.width, thumb.height)

    def scroll_input(self, e, /) -> bool:
        self._clamp_scroll()
        if e.shift or not can_scroll(
            self._scroll, max(0, len(self._rows) - self._page_rows()), e.delta
        ):
            self._wheel = 0.0
            return False
        self.handle_input(e)
        return True

    def handle_input(self, e, /):
        previous_scroll = self._scroll
        if (e.kind == "focus" and self.selected_key is not None
                and self._should_reveal_focus()):
            self.reveal(self.selected_key)
        self._clamp_scroll()
        if e.kind == "blur":
            self._prefix = ""
        handled, self._drag, values = handle_scrollbars(e, self._scrollbars(), self._drag)
        if 1 in values:
            self._scroll = round(values[1])
            self._clamp_scroll()
        if handled:
            if self._scroll != previous_scroll:
                self.invalidate()
            return
        if e.kind in ("blur", "pointer_up", "pointer_cancel"):
            self._drag = None
            if e.kind == "blur":
                self._prefix = ""
        elif e.kind == "wheel":
            self._wheel += e.delta * 3
            rows = int(round(self._wheel, 12))
            self._wheel -= rows
            self._scroll += rows
            self._clamp_scroll()
        elif e.kind == "pointer_down":
            x, y = e.x - self._rect.x, e.y - self._rect.y
            index = self._scroll + int(y / self._row_height())
            if 0 <= index < len(self._rows):
                node, depth = self._rows[index]
                if (
                    node.children
                    and 6 + depth * self.indent <= x < 6 + (depth + 1) * self.indent
                ):
                    self.toggle(node.key)
                else:
                    self.selected_key = node.key
        elif e.kind == "key_down" and self._rows:
            index = self._positions.get(self.selected_key, -1)
            if e.key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
                         "Home", "End", "PageUp", "PageDown"):
                self._prefix = ""
            if e.key in ("ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"):
                move = {
                    "ArrowUp": -1,
                    "ArrowDown": 1,
                    "PageUp": -self._page_rows(),
                    "PageDown": self._page_rows(),
                }
                index = (
                    0
                    if e.key == "Home"
                    else len(self._rows) - 1
                    if e.key == "End"
                    else max(0, min(len(self._rows) - 1, index + move[e.key]))
                )
                key = self._rows[index][0].key
                self.selected_key = key
                self.reveal(key)
            elif index >= 0:
                node = self._rows[index][0]
                if e.key == "ArrowRight" and node.children:
                    if node.key not in self.expanded_keys:
                        self.toggle(node.key)
                    else:
                        self.selected_key = node.children[0].key
                elif e.key == "ArrowLeft":
                    if node.key in self.expanded_keys:
                        self.toggle(node.key)
                    elif self._parents[node.key] is not None:
                        self.selected_key = self._parents[node.key]
                elif e.key == "Space":
                    self.toggle(node.key)
                elif e.key == "Enter":
                    self.activated.emit(
                        TreeEvent(source=self, key=node.key, origin=self._origin)
                    )
            if len(e.key) == 1 and e.key != " " and not e.ctrl:
                now = monotonic()
                self._prefix, prefix = typeahead_prefix(
                    self._prefix, self._prefix_time, e.key, now
                )
                self._prefix_time = now
                # Repeated letters cycle matches; a longer prefix can refine the current row.
                start = index if len(prefix) > 1 and index >= 0 else index + 1
                for offset in range(len(self._rows)):
                    node = self._rows[(start + offset) % len(self._rows)][0]
                    if node.text.casefold().startswith(prefix):
                        self.selected_key = node.key
                        self.reveal(node.key)
                        break
        if self._scroll != previous_scroll:
            self.invalidate()

    def paint(self, p, /):
        p.body()
        with p._clipped(Rect(0, 0, p.width, p.height)):
            self._paint_nodes(p)

    def _paint_nodes(self, p):
        row_height = self._row_height()
        self._clamp_scroll()
        # Respect an outer ScrollArea's clip as well as this control's own viewport.
        first = self._scroll + int(
            max(0, self._clip.y - self._rect.y) / row_height
        )
        end = min(
            len(self._rows),
            self._scroll
            + ceil(max(0, self._clip.bottom - self._rect.y) / row_height),
        )
        self._painted_rows = 0
        expander = p.style("expander")
        thumb = self._scrollbar()
        for index in range(first, end):
            node, depth = self._rows[index]
            y = (index - self._scroll) * row_height
            selected = node.key == self.selected_key
            style = p.style("row", selected=selected)
            text_height = self.effective_row_height(style.font_size * 1.2)
            p.surface(Rect(1, y, max(0, p.width - 2), row_height), style)
            if selected:
                p.rect(
                    Rect(1, y + 3, 3, max(0, row_height - 6)),
                    p.theme.tokens.accent,
                )
            x, mid = 6 + depth * self.indent, y + row_height / 2
            if node.children:
                p.icon(
                    "chevron_down" if node.key in self._expanded else "chevron_right",
                    x,
                    mid - 8,
                    size=16,
                    color=expander.foreground,
                )
            text_x = x + self.indent
            text = single_line(node.text)
            if node.icon:
                size = max(
                    0,
                    min(
                        self.effective_row_height(min(18, row_height - 6)),
                        p.width - text_x - 8,
                    ),
                )
                if size:
                    p.icon(
                        node.icon,
                        text_x,
                        mid - size / 2,
                        size=size,
                        color=style.foreground,
                    )
                text_x += 24
            text = p.elide(
                text,
                max(0, p.width - text_x - (20 if thumb else 8)),
                size=style.font_size,
                font_family=style.font_family,
            )
            p.text(
                text,
                text_x,
                y + (row_height - text_height) / 2,
                color=style.foreground,
                size=style.font_size,
                font_family=style.font_family,
            )
            self._painted_rows += 1
        paint_scrollbars(p, self._scrollbars())
