"""Bounded typed table data, viewport painting and ordinary popup cell editing."""

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from math import ceil, isfinite
from time import monotonic
from typing import ClassVar, Literal, TypeAlias

from ._text_display import single_line
from ._scrolling import can_scroll, clamp_scroll, scrollbar, handle_scrollbars, paint_scrollbars
from .controls import Control, Label
from .events import ChangeEvent, Event, UiEvent
from .geometry import Rect
from .popup import Popup
from .schema import Dirty, _persist_record, prop
from .widgets import ListView, TextBox

CellValue: TypeAlias = str | int | float | bool | None


@_persist_record
@dataclass(frozen=True)
class GridColumn:
    key: str
    title: str
    width: float = 140
    kind: Literal["text", "number", "bool"] = "text"
    editable: bool = False
    align: Literal["left", "center", "right"] = "left"
    format_spec: str = ""
    prefix: str = ""
    suffix: str = ""

    def __post_init__(self):
        if (
            not isinstance(self.key, str)
            or not self.key
            or not isinstance(self.title, str)
        ):
            raise ValueError("GridColumn needs a nonempty key and title string")
        if (
            type(self.width) not in (int, float)
            or not isfinite(self.width)
            or self.width < 48
        ):
            raise ValueError("GridColumn.width must be finite and at least 48")
        if (
            self.kind not in ("text", "number", "bool")
            or type(self.editable) is not bool
        ):
            raise ValueError(
                "GridColumn kind is text/number/bool; editable is a boolean"
            )
        if self.align not in ("left", "center", "right"):
            raise ValueError("GridColumn.align must be left, center, or right")
        if not isinstance(self.format_spec, str) or (
            self.format_spec
            and (
                self.kind != "number"
                or re.fullmatch(
                    r"[+ -]?[,_]?(?:\.(?:[0-9]|1[0-6]))?[eEfFgG%]",
                    self.format_spec,
                )
                is None
            )
        ):
            raise ValueError(
                "GridColumn.format_spec needs a number column and a bounded "
                "numeric format: optional sign, grouping, precision 0-16, "
                "and e/E/f/F/g/G/%"
            )
        if any(
            not isinstance(value, str)
            or len(value) > 64
            or "\n" in value
            or "\r" in value
            for value in (self.prefix, self.suffix)
        ):
            raise ValueError(
                "Column prefix/suffix must be single-line text up to 64 characters"
            )


@_persist_record
@dataclass(frozen=True)
class GridRow:
    key: str
    cells: tuple[CellValue, ...]

    def __post_init__(self):
        if not isinstance(self.key, str) or not self.key:
            raise ValueError("GridRow.key must be a nonempty string")
        if not isinstance(self.cells, tuple) or any(
            type(v) not in (str, int, float, bool, type(None)) for v in self.cells
        ):
            raise TypeError("GridRow.cells must be a tuple of immutable scalar values")
        if any(
            (isinstance(v, str) and len(v) > 4096)
            or (type(v) is float and not isfinite(v))
            for v in self.cells
        ):
            raise ValueError(
                "Cells need finite numbers and text of at most 4096 characters"
            )

    def __eq__(self, other):
        if type(other) is not type(self):
            return NotImplemented
        return (
            self.key == other.key
            and len(self.cells) == len(other.cells)
            and all(
                type(first) is type(second) and first == second
                for first, second in zip(self.cells, other.cells)
            )
        )


@dataclass(frozen=True, kw_only=True)
class GridCellEvent(UiEvent):
    row_key: str
    column_key: str
    value: CellValue


@dataclass(frozen=True, kw_only=True)
class GridEditEvent(UiEvent):
    row_key: str
    column_key: str
    old_value: CellValue
    new_value: CellValue


def _validate_data(columns, rows, previous_rows=()):
    if len(columns) > 64 or len(rows) > 20_000 or len(columns) * len(rows) > 200_000:
        raise ValueError(
            "DataGrid supports at most 64 columns, 20,000 rows and 200,000 cells"
        )
    if not isfinite(sum(float(column.width) for column in columns)):
        raise ValueError("Combined column widths must remain finite")
    if len({c.key for c in columns}) != len(columns) or len(
        {r.key for r in rows}
    ) != len(rows):
        raise ValueError("DataGrid column and row keys must be unique")
    for index, row in enumerate(rows):
        # With unchanged columns, immutable rows already passed these checks.
        if index < len(previous_rows) and row is previous_rows[index]:
            continue
        if len(row.cells) != len(columns):
            raise ValueError(f"Row {row.key!r} needs exactly {len(columns)} cells")
        for column, value in zip(columns, row.cells):
            if (
                value is not None
                and type(value)
                not in {"text": (str,), "number": (int, float), "bool": (bool,)}[
                    column.kind
                ]
            ):
                raise TypeError(
                    f"Cell {row.key!r}/{column.key!r} must match {column.kind!r}"
                )
            if column.format_spec and value is not None:
                try:
                    format(value, column.format_spec)
                except (ValueError, OverflowError) as error:
                    raise ValueError(
                        f"Cell {row.key!r}/{column.key!r} cannot use this numeric format"
                    ) from error


def _display(value, column=None):
    if value is None:
        return ""
    if column is None:
        return str(value)
    text = format(value, column.format_spec) if column.format_spec else str(value)
    return column.prefix + text + column.suffix


def _aligned_x(painter, text, column, x, size, font_family, *, trailing=0):
    if column.align == "left":
        return x + 8
    spare = max(
        0,
        column.width
        - 16
        - trailing
        - painter.measure(text, size=size, font_family=font_family)[0],
    )
    return x + 8 + spare * (0.5 if column.align == "center" else 1)


def _parse_number(text, column=None):
    text = text.strip()
    if not text:
        return None
    digits = text[1:] if text.startswith(("+", "-")) else text
    try:
        return int(text) if digits.isdecimal() else float(text)
    except ValueError:
        if column is None or (column.format_spec and column.format_spec[-1] not in "fF%"):
            raise
    # Accept the decimal or percentage presentation copied from this column.
    # Raw numeric entry above stays independent of display formatting.
    prefix, suffix = column.prefix.lstrip(), column.suffix.rstrip()
    if prefix and text.startswith(prefix):
        text = text[len(prefix):]
    if suffix and text.endswith(suffix):
        text = text[:-len(suffix)]
    text = text.strip()
    percentage = column.format_spec.endswith("%")
    if percentage:
        if not text.endswith("%"):
            raise ValueError("Use a percentage with the column's formatting")
        text = text[:-1]
    separator = next((s for s in (",", "_") if s in column.format_spec), "")
    integer = r"\d+"
    if separator:
        integer = rf"(?:\d+|\d{{1,3}}(?:{re.escape(separator)}\d{{3}})+)"
    if re.fullmatch(rf"[+-]?(?:{integer}(?:\.\d*)?|\.\d+)", text) is None:
        raise ValueError("Use a decimal number with the column's formatting")
    number = text.replace(separator, "") if separator else text
    return float(number) / 100 if percentage else _parse_number(number)


class _CellEditor(TextBox):
    def _initialize(self):
        super()._initialize()
        self._commit: Callable[[], None] | None = None

    def handle_input(self, e, /):
        if e.kind == "key_down" and e.key == "Enter":
            assert self._commit is not None
            self._commit()
        else:
            super().handle_input(e)


class _BooleanEditor(ListView):
    def _initialize(self):
        super()._initialize()
        self._commit: Callable[[], None] | None = None
        self._down_index = None

    def handle_input(self, e, /):
        super().handle_input(e)
        if e.kind == "pointer_down":
            self._down_index = self.index_at(e.x, e.y) if e.button == 1 else None
        commit = e.kind == "key_down" and e.key in ("Enter", "Space")
        if e.kind == "pointer_up":
            commit = (
                self._down_index == self.selected_index == self.index_at(e.x, e.y)
                and self.selected_index >= 0
            )
            self._down_index = None
        elif e.kind in ("blur", "pointer_cancel"):
            self._down_index = None
        if commit:
            assert self._commit is not None
            self._commit()


class _CellMessage(Label):
    def paint(self, p, /):
        style = p.body()
        p.text(
            p.elide(self.text, p.width, size=style.font_size),
            0,
            max(0, (p.height - style.font_size * 1.2) / 2),
            color=style.foreground,
            size=style.font_size,
        )


class _CellPopup(Popup):
    def reposition(self):
        if self._session is not None:
            grid = self._owner
            assert isinstance(grid, DataGrid) and grid.selected_key is not None
            self._point = (
                grid.bounds.x + sum(c.width for c in grid.columns[:grid.selected_column])
                - grid._scroll_x,
                grid.bounds.y + grid._header_height()
                + (grid._positions[grid.selected_key] - grid._scroll) * grid._row_height(),
            )
        super().reposition()


class DataGrid(Control):
    _cell_copy: ClassVar[bool] = True
    cache_paint: bool = prop(default=True)
    columns: tuple[GridColumn, ...] = prop(
        default=(), affects=Dirty.MEASURE
    )
    rows: tuple[GridRow, ...] = prop(default=())
    selected_key: str | None = prop(default=None, changed="changed", persist=False)
    selected_column: int = prop(
        default=0, minimum=0, changed="column_changed", persist=False
    )
    sort_key: str | None = prop(default=None, persist=False)
    sort_descending: bool = prop(default=False)
    row_height: float = prop(default=30.0, minimum=20, affects=Dirty.MEASURE)
    header_height: float = prop(default=34.0, minimum=24, affects=Dirty.MEASURE)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[str | None]]] = Event(ChangeEvent)
    column_changed: ClassVar[Event[ChangeEvent[int]]] = Event(ChangeEvent)
    activated: ClassVar[Event[GridCellEvent]] = Event(GridCellEvent)
    edited: ClassVar[Event[GridEditEvent]] = Event(GridEditEvent)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "header", "cell", "scrollbar", "track", "thumb")
    # Header/cell surfaces are explicitly viewport-clipped during paint. The
    # scrollbar uses an inset rect, not surface effects. These parts cannot
    # extend the body and must not inflate its render-cache allocation.
    _contained_style_parts: ClassVar[frozenset[str]] = frozenset(
        ("header", "cell", "scrollbar", "track", "thumb")
    )

    def _initialize(self):
        super()._initialize()
        _validate_data(self.columns, self.rows)
        self._scroll, self._scroll_x = 0, 0.0
        self._pending_reveal = True
        self._wheel = 0.0
        self._drag = None
        self._resize = None
        self._resize_columns = None
        self._applying_data = False
        self._pending_order = None
        self._pending_cell = None
        self._last_click = None
        self._popup = None
        self._rebuild()

    def _validation_copy(self, values):
        candidate = super()._validation_copy(values)
        if candidate.columns is not self.columns or candidate.rows is not self.rows:
            _validate_data(candidate.columns, candidate.rows,
                           self.rows if candidate.columns == self.columns else ())
            candidate._by_key = {row.key: row for row in candidate.rows}
        return candidate

    def _prepare_update(self, values):
        values = dict(super()._prepare_update(values))
        if not values.keys() & {"columns", "rows"}:
            return values
        self._check_live()
        columns = values.get("columns", self.columns)
        rows = values.get("rows", self.rows)
        for name, value in (("columns", columns), ("rows", rows)):
            self._schema[name].validate(value, previous=getattr(self, name))
        row_keys = {row.key for row in rows}
        keys = [column.key for column in columns]
        selected_column = (
            self.columns[self.selected_column].key if self.columns else None
        )
        # Omitted dependent fields retain surviving keys. Explicit requests are
        # left intact so candidate validation can reject invalid selections.
        values.setdefault("sort_key", self.sort_key if self.sort_key in keys else None)
        values.setdefault(
            "selected_key", self.selected_key if self.selected_key in row_keys else None
        )
        values.setdefault(
            "selected_column",
            keys.index(selected_column) if selected_column in keys
            else min(self.selected_column, max(0, len(columns) - 1)),
        )
        return values

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name in ("columns", "rows"):
            # Construction and candidate copies already validated these objects.
            if value is not getattr(self, name):
                _validate_data(
                    value if name == "columns" else self.columns,
                    value if name == "rows" else self.rows,
                    self.rows if name == "rows" else (),
                )
        elif name == "selected_key" and value is not None and value not in self._by_key:
            raise ValueError(f"Unknown grid row {value!r}")
        elif (
            name == "selected_column"
            and isinstance(value, int)
            and value >= max(1, len(self.columns))
        ):
            raise ValueError("selected_column must identify a column")
        elif (
            name == "sort_key"
            and value is not None
            and value not in {c.key for c in self.columns}
        ):
            raise ValueError(f"Unknown grid column {value!r}")

    def _commit_update(self, properties):
        if not any(
            name in properties and properties[name] != self._values[name]
            for name in ("columns", "rows", "sort_key", "sort_descending")
        ):
            return super()._commit_update(properties)
        # Data and dependent fields were preflighted together. Build only the
        # final order before any notification can reveal the selected cell.
        candidate = super()._validation_copy(properties)
        candidate._rebuild()
        self._pending_order = candidate._by_key, candidate._order, candidate._positions
        self._applying_data = True
        try:
            super()._commit_update(properties)
        finally:
            self._applying_data = False
            self._pending_order = None

    def _rebuild(self):
        self._by_key = {row.key: row for row in self.rows}
        self._order = list(self.rows)
        index = next(
            (i for i, c in enumerate(self.columns) if c.key == self.sort_key), None
        )
        if index is not None:
            self._order.sort(
                key=lambda row: (row.cells[index] is None, row.cells[index]),
                reverse=self.sort_descending,
            )
        self._positions = {row.key: i for i, row in enumerate(self._order)}
        self._clamp()

    def _close_editor(self):
        if self._popup is not None and self._popup.is_open:
            self._popup.dismiss()
        self._popup = None

    def _changed(self, field, old, value):
        if field.name == "rows" and self._pending_cell is not None:
            replacement, order, positions = self._pending_cell
            self._pending_cell = None
            self._by_key[replacement.key] = replacement
            if order is None:
                self._order[self._positions[replacement.key]] = replacement
            else:
                assert positions is not None
                self._order, self._positions = order, positions
            self._last_click = None
            self._resize = self._resize_columns = None
            self._close_editor()
            if self.selected_key == replacement.key:
                self._reveal()
            super()._changed(field, old, value)
            return
        if field.name in (
            "columns",
            "rows",
            "sort_key",
            "sort_descending",
            "selected_key",
            "selected_column",
        ):
            self._last_click = None
        if self._applying_data:
            if self._pending_order is not None:
                self._by_key, self._order, self._positions = self._pending_order
                self._pending_order = None
                self._resize = self._resize_columns = None
                self._close_editor()
                self._reveal()
            super()._changed(field, old, value)
            return
        if field.name in (
            "columns",
            "rows",
            "sort_key",
            "sort_descending",
            "selected_key",
            "selected_column",
        ):
            self._resize = self._resize_columns = None
            self._close_editor()
        if field.name in ("columns", "rows", "sort_key", "sort_descending"):
            self._rebuild()
            if self.sort_key not in {c.key for c in self.columns}:
                self.sort_key = None
            if self.selected_key not in self._by_key:
                self.selected_key = None
            self.selected_column = min(
                self.selected_column, max(0, len(self.columns) - 1)
            )
        if field.name in (
            "selected_key",
            "selected_column",
            "sort_key",
            "sort_descending",
        ):
            self._reveal()
        super()._changed(field, old, value)

    def set_data(
        self, *, columns: tuple[GridColumn, ...], rows: tuple[GridRow, ...]
    ) -> None:
        """Replace a bounded dataset atomically, retaining surviving row/column keys.

        Validation and sorting finish before mutation. Notifications observe the
        complete replacement; an invalid candidate leaves data and editors intact.
        """
        self.update(columns=columns, rows=rows)

    def update_rows(
        self, rows: tuple[GridRow, ...] = (), *, remove: tuple[str, ...] = ()
    ) -> None:
        """Atomically replace/append keyed rows and remove known keys in one batch.

        Existing rows keep their order; new rows append in the supplied order.
        This is synchronous bounded preparation, with the same limits as set_data.
        """
        self._check_live()
        self._schema["rows"].validate(rows)
        if (
            len(rows) > 20_000
            or not isinstance(remove, tuple)
            or any(not isinstance(key, str) for key in remove)
        ):
            raise ValueError("Use at most 20,000 rows and a tuple of removal keys")
        updates = {row.key: row for row in rows}
        removed = set(remove)
        if len(updates) != len(rows) or len(removed) != len(remove):
            raise ValueError("Update and removal keys must be unique")
        if removed - self._by_key.keys() or removed & updates.keys():
            raise ValueError("Removal keys must exist and cannot also be updated")
        candidate = tuple(
            updates.get(row.key, row) for row in self.rows if row.key not in removed
        ) + tuple(row for row in rows if row.key not in self._by_key)
        self.set_data(columns=self.columns, rows=candidate)

    @property
    def selected_row(self) -> GridRow | None:
        self._check_live()
        return (
            self._by_key.get(self.selected_key)
            if self.selected_key is not None
            else None
        )

    def measure(self, host):
        return 560, 300

    def _row_height(self):
        return self.effective_row_height(self.row_height)

    def _header_height(self):
        return self.effective_row_height(self.header_height)

    def _viewport(self):
        # Reserve tracks consistently, so scrollbars never change column sizing.
        return max(0, self.bounds.width - 12), max(
            0, self.bounds.height - self._header_height() - 12
        )

    def _view_columns(self):
        return self._resize_columns or self.columns

    def _page_rows(self):
        return max(1, int(self._viewport()[1] / self._row_height()))

    def _clamp(self):
        self._scroll = clamp_scroll(self._scroll, len(self._order), self._page_rows())
        self._scroll_x = clamp_scroll(
            self._scroll_x,
            sum(c.width for c in self._view_columns()),
            self._viewport()[0],
        )

    def _reveal(self):
        # Data can be assigned during build(), before layout gives us a viewport.
        # Defer scrolling until the selected cell can be measured against it.
        width, height = self._viewport()
        if width <= 0 or height <= 0:
            self._pending_reveal = True
            return
        self._pending_reveal = False
        index = (
            self._positions.get(self.selected_key)
            if self.selected_key is not None
            else None
        )
        if index is not None:
            self._scroll = max(0, min(self._scroll, index))
            if index >= self._scroll + self._page_rows():
                self._scroll = index - self._page_rows() + 1
        if self.columns:
            x = sum(c.width for c in self.columns[: self.selected_column])
            column_width = self.columns[self.selected_column].width
            if column_width >= width:
                # An oversized cell cannot fit; show its label from the start.
                self._scroll_x = x
            else:
                self._scroll_x = min(self._scroll_x, x)
                if x + column_width > self._scroll_x + width:
                    self._scroll_x = x + column_width - width
        self._clamp()

    def _scrollbars(self):
        width, height = self._viewport()
        x, y = self.bounds.x, self.bounds.y + self._header_height()
        bars = (
            scrollbar(Rect(x + width, y, min(12, self.bounds.width), height),
                      len(self._order), self._page_rows(), self._scroll),
            scrollbar(Rect(x, y + height, width, min(12, self.bounds.height)),
                      sum(c.width for c in self._view_columns()), width, self._scroll_x, 0),
        )
        return [bar for bar in bars if bar is not None]

    def _thumbs(self):
        thumbs = {bar.axis: Rect(bar.thumb.x - self.bounds.x, bar.thumb.y - self.bounds.y,
                                bar.thumb.width, bar.thumb.height)
                  for bar in self._scrollbars()}
        return thumbs.get(1), thumbs.get(0)

    def _column_at(self, x):
        offset = -self._scroll_x
        for i, column in enumerate(self._view_columns()):
            if offset <= x < offset + column.width:
                return i
            offset += column.width
        return None

    def scroll_input(self, e, /) -> bool:
        self._clamp()
        offset = self._scroll_x if e.shift else self._scroll
        limit = (
            max(0, sum(column.width for column in self._view_columns()) - self._viewport()[0])
            if e.shift else max(0, len(self._order) - self._page_rows())
        )
        if not can_scroll(offset, limit, e.delta):
            self._wheel = 0.0
            return False
        self.handle_input(e)
        return True

    def handle_input(self, e, /):
        previous_view = self._scroll, self._scroll_x, self._resize_columns, self._resize
        if e.kind == "focus" and self._should_reveal_focus():
            self._reveal()
        self._clamp()
        x, y = e.x - self.bounds.x, e.y - self.bounds.y
        width, height = self._viewport()
        handled, self._drag, values = handle_scrollbars(e, self._scrollbars(), self._drag)
        if 1 in values:
            self._scroll = round(values[1])
        if 0 in values:
            self._scroll_x = values[0]
        if handled:
            self._last_click = None
            self._clamp()
            if previous_view[:2] != (self._scroll, self._scroll_x):
                self._close_editor()
                self.invalidate()
            return
        if e.kind in ("blur", "pointer_cancel") or (
            e.kind == "key_down" and e.key == "Escape" and self._resize is not None
        ):
            self._last_click = None
            if self._resize is not None:
                self._scroll_x = self._resize[3]
            self._resize = self._resize_columns = None
            self._drag = None
            self._clamp()
        elif e.kind == "pointer_up":
            if self._last_click is not None and (
                abs(e.x - self._last_click[4]) > 8 or abs(e.y - self._last_click[5]) > 8
            ):
                self._last_click = None
            columns = self._resize_columns
            self._resize = self._resize_columns = None
            if columns is not None:
                self.columns = columns
            self._drag = None
        elif e.kind == "wheel":
            self._last_click = None
            self._close_editor()
            if e.shift:
                self._scroll_x += e.delta * 40
            else:
                self._wheel += e.delta * 3
                rows_moved = int(round(self._wheel, 12))
                self._wheel -= rows_moved
                self._scroll += rows_moved
            self._clamp()
        elif e.kind == "pointer_move" and self._resize is not None:
            index, start, columns, _ = self._resize
            resized = columns[index].width + e.x - start
            if isfinite(resized):
                candidate = (
                    *columns[:index],
                    replace(columns[index], width=max(48, resized)),
                    *columns[index + 1 :],
                )
                if isfinite(sum(float(column.width) for column in candidate)):
                    self._resize_columns = candidate
                    self._clamp()
        elif e.kind == "pointer_move" and self._last_click is not None:
            if abs(e.x - self._last_click[4]) > 8 or abs(e.y - self._last_click[5]) > 8:
                self._last_click = None
        elif e.kind == "pointer_down":
            previous_click, self._last_click = self._last_click, None
            boundary, resize_index = -self._scroll_x, None
            if 0 <= x < width and 0 <= y < self._header_height():
                for index, column in enumerate(self.columns):
                    boundary += column.width
                    if abs(x - boundary) <= 5:
                        resize_index = index
                        break
            if resize_index is not None:
                self._close_editor()
                self._resize = (resize_index, e.x, self.columns, self._scroll_x)
            elif x < width and y < self._header_height() + height:
                column = self._column_at(x)
                if column is not None:
                    if y < self._header_height():
                        key = self.columns[column].key
                        self.update(
                            sort_descending=self.sort_key == key and not self.sort_descending,
                            sort_key=key,
                        )
                    else:
                        index = self._scroll + int(
                            (y - self._header_height()) / self._row_height()
                        )
                        if index < len(self._order):
                            self.selected_column = column
                            self.selected_key = self._order[index].key
                            now = monotonic()
                            identity = (
                                self.selected_key,
                                self.columns[column].key,
                                e.pointer_kind,
                            )
                            if (
                                previous_click is not None
                                and previous_click[:3] == identity
                                and 0 <= now - previous_click[3] <= 0.4
                                and abs(e.x - previous_click[4]) <= 8
                                and abs(e.y - previous_click[5]) <= 8
                            ):
                                self._activate_cell()
                            else:
                                self._last_click = (*identity, now, e.x, e.y)
        elif e.kind == "key_down" and self.columns:
            index = (
                self._positions.get(self.selected_key, -1)
                if self.selected_key is not None
                else -1
            )
            movement = {
                "ArrowUp": -1,
                "ArrowDown": 1,
                "PageUp": -self._page_rows(),
                "PageDown": self._page_rows(),
            }
            if self._order and e.key in (*movement, "Home", "End"):
                index = (
                    0
                    if e.key == "Home"
                    else len(self._order) - 1
                    if e.key == "End"
                    else max(0, min(len(self._order) - 1, index + movement[e.key]))
                )
                self.selected_key = self._order[index].key
                self._reveal()
            elif e.key in ("ArrowLeft", "ArrowRight"):
                self.selected_column = max(
                    0,
                    min(
                        len(self.columns) - 1,
                        self.selected_column + (-1 if e.key == "ArrowLeft" else 1),
                    ),
                )
                self._reveal()
            elif e.key in ("Enter", "F2") and self.selected_row is not None:
                if e.key == "Enter":
                    self._activate_cell()
                else:
                    self.begin_edit()
        if previous_view != (
            self._scroll,
            self._scroll_x,
            self._resize_columns,
            self._resize,
        ):
            self.invalidate()


    def _activate_cell(self):
        row = self.selected_row
        if row is None or not self.columns:
            return
        column = self.columns[self.selected_column]
        self.activated.emit(
            GridCellEvent(
                source=self,
                row_key=row.key,
                column_key=column.key,
                value=row.cells[self.selected_column],
                origin=self._origin,
            )
        )
        self.begin_edit()

    def begin_edit(self) -> None:
        self._check_live()
        row = self.selected_row
        if row is None or not self.columns or not self.effective_enabled:
            return
        index = self.selected_column
        column = self.columns[index]
        if not column.editable:
            return
        self._close_editor()
        self._reveal()
        boolean = column.kind == "bool"
        choice_height = 3 * self.effective_row_height(30)
        popup = _CellPopup(
            width=max(260, column.width),
            height=choice_height + 50 if boolean else 90,
            layout="stack",
            padding=6,
            spacing=4,
        )
        choices = (True, False, None)
        editor = popup.add(
            _BooleanEditor(
                items=("True", "False", "Empty"),
                selected_index=choices.index(row.cells[index]),
                row_height=30,
                height=choice_height,
            )
            if boolean
            else _CellEditor(text=_display(row.cells[index]), height=36)
        )
        # Single-line editing normalizes CR/LF. Preserve the original scalar
        # when the user accepts that initial presentation without changing it.
        initial_text = editor.text if isinstance(editor, _CellEditor) else None
        message = popup.add(
            _CellMessage(
                text="↑↓ choose · Enter saves · Esc cancels"
                if boolean
                else "Number or blank · Enter saves"
                if column.kind == "number"
                else "Enter saves · Escape cancels",
                height=32,
                font_size=12,
            )
        )

        def commit():
            try:
                if isinstance(editor, _BooleanEditor):
                    value: CellValue = choices[editor.selected_index]
                else:
                    text = editor.text
                    if text == initial_text:
                        popup.dismiss()
                        return
                    value = _parse_number(text, column) if column.kind == "number" else text
                self.set_cell(row.key, column.key, value, origin="user")
            except (ValueError, TypeError) as error:
                message.text = (
                    "Use a finite number, or leave blank"
                    if column.kind == "number"
                    else str(error)
                )
                message.tooltip = str(error)
                message.foreground = self.effective_theme.tokens.danger
                editor.focus()
                if isinstance(editor, _CellEditor):
                    editor.select_all()
                return
            if popup.is_open:
                popup.dismiss()

        editor._commit = commit
        self._popup = popup
        try:
            popup.show(self)
            if isinstance(editor, _CellEditor):
                editor.select_all()
        except BaseException:
            popup.destroy()
            self._popup = None
            raise

    @property
    def selected_cell_text(self) -> str | None:
        """Full formatted cell text, or None when no cell is selected."""
        self._check_live()
        key = self.selected_key
        if key is None or not self.columns:
            return None
        row = self._by_key.get(key)
        if row is None:
            return None
        column = self.columns[self.selected_column]
        return _display(row.cells[self.selected_column], column)

    def set_cell(
        self,
        row_key: str,
        column_key: str,
        value: CellValue,
        *,
        origin: Literal["user", "program", "system"] = "program",
    ) -> None:
        """Validate and publish one edit atomically, retaining unaffected indexes.

        The immutable public rows tuple is published immediately (O(n)). Local
        edits validate only the replacement unless row hooks are customized.
        """
        self._check_live()
        row = self._by_key[row_key]
        index = next(
            (i for i, c in enumerate(self.columns) if c.key == column_key), None
        )
        if index is None:
            raise KeyError(column_key)
        old = row.cells[index]
        if type(old) is type(value) and old == value:
            return
        replacement = replace(
            row, cells=(*row.cells[:index], value, *row.cells[index + 1 :])
        )
        _validate_data(self.columns, (replacement,))
        customized = (
            self._schema["rows"] is not DataGrid._schema["rows"]
            or type(self)._validate_update is not DataGrid._validate_update
            or type(self)._changed is not DataGrid._changed
            or type(self)._apply_validated is not Control._apply_validated
            or any(name in self.__dict__ for name in (
                "_validate_update", "_changed", "_apply_validated"
            ))
        )
        rows = self.rows
        if self.sort_key is None:
            # Source and display order coincide, so reuse the existing index.
            # The transient copy leaves prior immutable snapshots untouched.
            updated = list(rows)
            updated[self._positions[row_key]] = replacement
            candidate = tuple(updated)
        else:
            candidate = tuple(replacement if r is row else r for r in rows)
        if customized:
            # Preserve subclass row constraints from the original property
            # assignment path. Their extra validation uses the slower path.
            self._schema["rows"].validate(candidate, previous=rows)
            self._validate_update("rows", candidate)
        order = positions = None
        if self.sort_key == column_key:
            # Start from publication order to preserve stable ties when a value
            # joins an existing group. All comparisons finish before mutation.
            order = list(candidate)
            order.sort(
                key=lambda item: (item.cells[index] is None, item.cells[index]),
                reverse=self.sort_descending,
            )
            positions = {item.key: i for i, item in enumerate(order)}
        self._pending_cell = replacement, order, positions
        try:
            if customized:
                self._apply_validated({"rows": candidate})
            else:
                self._values["rows"] = candidate
                self._changed(self._schema["rows"], rows, candidate)
        finally:
            self._pending_cell = None
        self.edited.emit(
            GridEditEvent(
                source=self,
                row_key=row_key,
                column_key=column_key,
                old_value=old,
                new_value=value,
                origin=origin,
            )
        )

    def paint(self, p, /):
        row_height, header_height = self._row_height(), self._header_height()
        p.body()
        if self._pending_reveal:
            self._reveal()
        self._clamp()
        width, height = self._viewport()
        visible_columns = []
        x = -self._scroll_x
        for i, column in enumerate(self._view_columns()):
            if x + column.width > max(0, self._clip.x - self.bounds.x) and x < min(
                width, self._clip.right - self.bounds.x
            ):
                visible_columns.append((i, column, x))
            x += column.width
        first = self._scroll + int(
            max(0, self._clip.y - self.bounds.y - header_height) / row_height
        )
        end = min(
            len(self._order),
            self._scroll
            + ceil(
                max(
                    0,
                    min(height, self._clip.bottom - self.bounds.y - header_height),
                )
                / row_height
            ),
        )
        self._painted_cells = 0
        with p._clipped(Rect(0, header_height, width, height)):
            for row_index in range(first, end):
                row = self._order[row_index]
                y = header_height + (row_index - self._scroll) * row_height
                style = p.style("cell", selected=row.key == self.selected_key)
                text_height = self.effective_row_height(style.font_size * 1.2)
                p.surface(Rect(0, y, width, row_height), style)
                for i, column, x in visible_columns:
                    text = p.elide(
                        single_line(_display(row.cells[i], column)),
                        column.width - 16,
                        size=style.font_size,
                        font_family=style.font_family,
                    )
                    p.text(
                        text,
                        _aligned_x(
                            p, text, column, x, style.font_size, style.font_family
                        ),
                        y + (row_height - text_height) / 2,
                        color=style.foreground,
                        size=style.font_size,
                        font_family=style.font_family,
                    )
                    p.line(
                        x + column.width,
                        y,
                        x + column.width,
                        y + row_height,
                        style.border,
                    )
                    if row.key == self.selected_key and i == self.selected_column:
                        p.rect(
                            Rect(x + 1, y + 1, column.width - 2, row_height - 2),
                            "",
                            border=p.theme.tokens.accent,
                        )
                    self._painted_cells += 1
        with p._clipped(Rect(0, 0, width, header_height + height)):
            with p._clipped(Rect(0, 0, width, header_height)):
                header = p.style("header")
                text_height = self.effective_row_height(header.font_size * 1.2)
                p.surface(Rect(0, 0, width, header_height), header)
                for _, column, x in visible_columns:
                    marker = min(header.font_size, 20) if self.sort_key == column.key else 0
                    trailing = marker + 4 if marker else 0
                    title = p.elide(
                        single_line(column.title),
                        column.width - 16 - trailing,
                        size=header.font_size,
                        font_family=header.font_family,
                    )
                    p.text(
                        title,
                        _aligned_x(
                            p, title, column, x, header.font_size, header.font_family,
                            trailing=trailing,
                        ),
                        (header_height - text_height) / 2,
                        color=header.foreground,
                        size=header.font_size,
                        font_family=header.font_family,
                    )
                    if marker:
                        p.icon(
                            "arrow_down" if self.sort_descending else "arrow_up",
                            x + column.width - 8 - marker,
                            (header_height - marker) / 2,
                            size=marker,
                            color=header.foreground,
                        )
                    p.line(
                        x + column.width,
                        0,
                        x + column.width,
                        header_height,
                        header.border,
                    )
            p.line(0, header_height, width, header_height, header.border)
            if self._resize is not None:
                index = self._resize[0]
                boundary = sum(c.width for c in self._view_columns()[: index + 1])
                p.line(
                    boundary - self._scroll_x,
                    0,
                    boundary - self._scroll_x,
                    header_height + height,
                    p.theme.tokens.accent,
                    2,
                )
        vertical, horizontal = self._thumbs()
        if vertical is not None:
            p.rect(Rect(width, 0, 12, p.height), p.theme.tokens.background)
        if horizontal is not None:
            p.rect(
                Rect(0, header_height + height, p.width, 12),
                p.theme.tokens.background,
            )
        paint_scrollbars(p, self._scrollbars())

    def destroy(self):
        self._close_editor()
        super().destroy()
