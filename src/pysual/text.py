"""Text editing, Unicode navigation, history and viewport painting."""

from __future__ import annotations

import asyncio
import bisect
from collections import Counter, OrderedDict
from dataclasses import replace
from math import ceil, floor, isfinite
from time import monotonic
from typing import ClassVar

from .controls import Control
from .events import ChangeEvent, Event
from .geometry import Rect
from .schema import Dirty, prop
from .painting import resolve_style
from .graphemes import boundaries, floor_boundary, is_boundary, previous_boundary, next_boundary
from ._scrolling import can_scroll, scrollbar, handle_scrollbars, paint_scrollbars
from ._text_display import single_line


class _TabStops:
    """Sparse source/display offsets; only tabs need an index entry."""

    def __init__(self, line, size):
        self.line, self.size = line, size
        self.tabs, self.stops, self.extra = [], [], []
        offset, extra = line.find("\t"), 0
        while offset >= 0:
            extra += size - (offset + extra) % size - 1
            self.tabs.append(offset)
            self.stops.append(offset + 1 + extra)
            self.extra.append(extra)
            offset = line.find("\t", offset + 1)

    def display_offset(self, offset):
        index = bisect.bisect_left(self.tabs, offset)
        return offset + (self.extra[index - 1] if index else 0)

    def source_offset(self, column):
        index = bisect.bisect_right(self.stops, column)
        offset = column - (self.extra[index - 1] if index else 0)
        return min(offset, self.tabs[index] if index < len(self.tabs) else len(self.line))

    def expand(self, start=0, end=None):
        # A fragment's first tab still belongs to the complete row's tab grid.
        prefix = self.display_offset(start) % self.size
        return (" " * prefix + self.line[start:end]).expandtabs(self.size)[prefix:]


class _MaskStops:
    """Map stored string offsets to one password bullet per grapheme."""

    def __init__(self, line):
        self.offsets = boundaries(line)

    def display_offset(self, offset):
        return bisect.bisect_left(self.offsets, offset)

    def source_offset(self, column):
        return self.offsets[min(max(0, column), len(self.offsets) - 1)]

    def expand(self, start=0, end=None):
        end = self.offsets[-1] if end is None else end
        return "•" * (self.display_offset(end) - self.display_offset(start))


def _word_kind(char):
    return "space" if char.isspace() else "word" if char.isalnum() or char == "_" else "punct"


class TextBox(Control):
    cache_paint: bool = prop(default=True)
    text: str = prop(default="", affects=Dirty.PAINT, changed="changed")
    placeholder: str = prop(default="")
    multiline: bool = prop(default=False, affects=Dirty.MEASURE)
    read_only: bool = prop(default=False)
    password: bool = prop(default=False)
    monospace: bool = prop(default=False, affects=Dirty.MEASURE | Dirty.PAINT)
    line_numbers: bool = prop(default=False, affects=Dirty.MEASURE | Dirty.PAINT)
    tab_size: int = prop(default=4, minimum=1)
    auto_indent: bool = prop(default=False)
    max_undo_bytes: int = prop(default=8 * 1024 * 1024, minimum=0)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[str]]] = Event(ChangeEvent)
    history_changed: ClassVar[Event[ChangeEvent[tuple[bool, bool]]]] = Event(ChangeEvent)
    _style_kind: ClassVar[str] = "TextBox"
    _text_input: ClassVar[bool] = True
    style_parts: ClassVar[tuple[str, ...]] = ("body", "track", "thumb")
    _contained_style_parts: ClassVar[frozenset[str]] = frozenset(("track", "thumb"))

    @staticmethod
    def _normalize_text(text, multiline):
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return text if multiline else text.replace("\n", " ")

    def __setattr__(self, name, value):
        if name == "text" and isinstance(value, str) and hasattr(self, "_values"):
            value = self._normalize_text(value, self.multiline)
        super().__setattr__(name, value)

    def _prepare_update(self, values):
        if "text" in values and isinstance(values["text"], str):
            return {**values, "text": self._normalize_text(
                values["text"], values.get("multiline", self.multiline))}
        return values

    def _initialize(self):
        super()._initialize()
        self._values["text"] = self._normalize_text(self.text, self.multiline)
        self._caret = self._anchor = len(self.text)
        self._undo = []
        self._redo = []
        self._edit_group = None
        self._scroll = 0
        self._wheel = 0.0
        self._scroll_x = 0.0
        self._scroll_drag = None
        self._scroll_axes = (False, False)
        self._width_cache = {}
        self._width_cache_key = None
        # Count duplicate rows while retaining one metric per distinct line.
        self._width_line_counts = None
        self._width_pending = set()
        # Equal widths share the maximum until their last measured line leaves.
        self._width_counts = Counter()
        self._reveal_after_paint = True
        self._text_viewport_metrics = None
        self._composition = ""
        self._line_cache = None
        self._pending_line_cache = None
        self._fixed_line_cache = {}
        self._preferred_column = None
        self._extent_cache = None
        self._measure_host = None
        self._history_availability = (False, False)
        self._tab_maps = OrderedDict()
        self._tab_map_chars = 0
        # Only one bounded row of whole-prefix measurements. Keeping offsets,
        # rather than prefix strings or isolated glyph advances, preserves host
        # kerning/shaping without retaining quadratic amounts of text.
        self._hit_widths = OrderedDict()
        self._hit_context = None
        self._hit_host = None
        self._last_click = None
        self._word_drag = None
        self._selection_pointer = None
        self._selection_task = None
        self._selection_token = None

    def _changed(self, field, old, value):
        if field.name == "text" or (field.name in ("enabled", "visible") and not value):
            self._stop_selection_scroll()
        if field.name in ("text", "tab_size", "password"):
            self._tab_maps.clear()
            self._tab_map_chars = 0
            self._hit_widths.clear()
            self._hit_context = self._hit_host = None
            self._last_click = self._word_drag = None
            if field.name in ("tab_size", "password"):
                self._preferred_column = None
                self._ensure_caret()
        if field.name == "multiline" and not value:
            normalized = self._normalize_text(self.text, False)
            if normalized != self.text:
                self._undo.clear()
                self._redo.clear()
                self.text = normalized
        if field.name == "text":
            self._edit_group = None
            pending = self._pending_line_cache
            prepared = pending[1] if pending is not None and pending[0] == value else None
            self._line_cache = prepared[0] if prepared is not None else None
            self._pending_line_cache = None
            self._fixed_line_cache.clear()
            if self._width_line_counts is not None:
                if prepared is None:
                    changes = Counter(self._lines()[0])
                    changes.subtract(self._width_line_counts)
                else:
                    changes = Counter(prepared[2])
                    changes.subtract(prepared[1])
                self._update_line_widths(changes)
            self._preferred_column = None
            self._caret = floor_boundary(value, min(self._caret, len(value)))
            self._anchor = floor_boundary(value, min(self._anchor, len(value)))
            self._ensure_caret()
        if field.name == "max_undo_bytes":
            self._trim_history()
        if field.name == "read_only" and value and self._composition:
            self._composition = ""
            self._reveal_after_paint = False
            self._clamp_scroll()
        if field.name in ("max_undo_bytes", "read_only", "multiline"):
            self._edit_group = None
            self._notify_history()
        super()._changed(field, old, value)

    def measure(self, host):
        self._measure_host = host
        style = resolve_style(self)
        return (
            220,
            max(140 if self.multiline else self._control_height(host), self._line_height() + style.padding * 2),
        )

    @property
    def selection_text(self):
        return (
            ""
            if self.password
            else self.text[
                min(self._caret, self._anchor) : max(self._caret, self._anchor)
            ]
        )

    def select_all(self):
        self.select(0, len(self.text))

    @property
    def selection_range(self) -> tuple[int, int]:
        self._check_live()
        return min(self._anchor, self._caret), max(self._anchor, self._caret)

    @property
    def line_count(self) -> int:
        return len(self._lines()[0])

    def select(self, start: int, end: int) -> None:
        self._check_live()
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start <= len(self.text)
            or not 0 <= end <= len(self.text)
        ):
            raise ValueError("Selection offsets must be within the document")
        self._anchor, self._caret = floor_boundary(self.text, start), floor_boundary(self.text, end)
        self._edit_group = None
        self._last_click = self._word_drag = None
        self._preferred_column = None
        self._ensure_caret()
        self.invalidate()

    def capture_view_state(self) -> tuple[int, int, int, float]:
        """Return anchor, caret, first visible row and horizontal pixel offset."""
        self._check_live()
        return self._anchor, self._caret, self._scroll, float(self._scroll_x)

    def restore_view_state(self, state: tuple[int, int, int, float]) -> None:
        """Restore a saved view without scrolling it back to the caret.

        Selection offsets must fit this document. The row is clamped to the
        current viewport; the horizontal offset must be finite and nonnegative.
        """
        self._check_live()
        if not isinstance(state, tuple) or len(state) != 4:
            raise TypeError("View state must be an (anchor, caret, row, x) tuple")
        anchor, caret, row, x = state
        if any(type(value) is not int for value in (anchor, caret, row)):
            raise TypeError("View selection and row must be integers")
        if not 0 <= anchor <= len(self.text) or not 0 <= caret <= len(self.text):
            raise ValueError("View selection offsets must fit the document")
        if row < 0 or type(x) not in (int, float) or not isfinite(x) or x < 0:
            raise ValueError("View scroll offsets must be finite and nonnegative")
        self._anchor, self._caret = floor_boundary(self.text, anchor), floor_boundary(self.text, caret)
        self._edit_group = None
        self._last_click = self._word_drag = None
        self._preferred_column = None
        self._scroll, self._scroll_x = row, float(x)
        self._clamp_scroll()
        self._wheel = 0.0
        self._composition = ""
        self._reveal_after_paint = False
        self.invalidate()

    def load_text(self, text: str) -> None:
        """Replace a document, resetting selection, scroll and editing history."""
        self._check_live()
        self.text = text
        self._edit_group = None
        self._last_click = self._word_drag = None
        self._caret = self._anchor = 0
        self._scroll = 0
        self._wheel = 0.0
        self._scroll_x = 0
        self._composition = ""
        self._reveal_after_paint = False
        self._undo.clear()
        self._redo.clear()
        self._preferred_column = None
        self._notify_history()
        self.invalidate()

    @property
    def can_undo(self) -> bool:
        """Whether an undo can currently change this editable document."""
        self._check_live()
        return not self.read_only and bool(self._undo)

    @property
    def can_redo(self) -> bool:
        self._check_live()
        return not self.read_only and bool(self._redo)

    def _notify_history(self):
        current = self.can_undo, self.can_redo
        previous = self._history_availability
        self._history_availability = current
        if current != previous and self._dispatcher is not None:
            self.history_changed.emit(ChangeEvent(
                source=self, origin=self._origin, property_name="history",
                old_value=previous, new_value=current,
            ))

    def _remember(self, stack, snapshot):
        stack.append(snapshot)
        self._trim_history()

    def _trim_history(self):
        # Account for worst-case Python Unicode storage, not just UTF-8 bytes.
        for stack in (self._undo, self._redo):
            budget = 0
            keep = 0
            for snapshot in reversed(stack):
                budget += len(snapshot[0]) * 4 + 128
                if budget > self.max_undo_bytes or keep >= 200:
                    break
                keep += 1
            del stack[: len(stack) - keep]
        budget = sum(len(s[0]) * 4 + 128 for s in self._undo + self._redo)
        while budget > self.max_undo_bytes:
            # Prune the farthest step; on a tie prefer retaining redo.
            stack = self._undo if len(self._undo) >= len(self._redo) else self._redo
            budget -= len(stack.pop(0)[0]) * 4 + 128

    def _snapshot(self):
        return self.text, self._caret, self._anchor

    def replace_selection(self, text):
        """Replace the selection, or insert at the caret, as one undoable edit.
        Normalizes line endings and moves the caret after inserted text; changed
        edits clear redo. Read-only controls do nothing; rejected validation
        preserves text, selection and history. Returns None. See
        [text editing](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#text-editing-and-history).
        """
        self._replace_range(text, *self.selection_range)

    def _replace_range(self, text, a, b, *, group=None):
        self._check_live()
        if self.read_only:
            return
        text = self._normalize_text(text, self.multiline)
        a, b = sorted((a, b))
        value = self.text[:a] + text + self.text[b:]
        caret = a + len(text)
        if (value, caret, caret) == self._snapshot():
            self._edit_group = None
            return
        previous = self._snapshot()
        now = monotonic()
        # A selected replacement is always a separate step. A group retains its
        # first snapshot, bounded by the same ordinary history budget.
        group = group if self._caret == self._anchor else None
        coalesce = (
            group is not None and self._edit_group is not None and bool(self._undo)
            and self._edit_group[0] == group and self._edit_group[1] == self._caret
            and 0 <= now - self._edit_group[2] <= 1.0
        )
        prepared = self._edited_lines(a, b, text)
        self._pending_line_cache = value, prepared
        try:
            self.text = value
        finally:
            self._pending_line_cache = None
        self._redo.clear()
        if not coalesce:
            self._remember(self._undo, previous)
        caret = min(caret, len(self.text))
        if not is_boundary(self.text, caret):
            caret = next_boundary(self.text, caret)
        self._caret = self._anchor = caret
        self._edit_group = (group, caret, now) if group is not None else None
        self._ensure_caret()
        self._notify_history()
        self.invalidate()

    def undo(self):
        self._check_live()
        self._edit_group = None
        if self.read_only or not self._undo:
            return
        self._restore_history(self._undo, self._redo)

    def redo(self):
        self._check_live()
        self._edit_group = None
        if self.read_only or not self._redo:
            return
        self._restore_history(self._redo, self._undo)

    def _restore_history(self, source, destination):
        previous = self._snapshot()
        snapshot = source[-1]
        text, caret, anchor = snapshot
        self.text = text
        # A changed hook can reduce the history budget during assignment.
        if source and source[-1] is snapshot:
            source.pop()
        self._remember(destination, previous)
        self._caret = floor_boundary(self.text, min(caret, len(self.text)))
        self._anchor = floor_boundary(self.text, min(anchor, len(self.text)))
        self._ensure_caret()
        self._notify_history()
        self.invalidate()

    def _edited_lines(self, start, end, inserted):
        """Splice an edit's rows, retaining all unaffected lines and metrics."""
        if self._line_cache is None:
            return None
        lines, starts = self._line_cache
        row = bisect.bisect_right(starts, start) - 1
        last = bisect.bisect_right(starts, end) - 1
        offset = starts[row]
        replacement = (lines[row][:start - offset] + inserted
                       + lines[last][end - starts[last]:]).split("\n")
        updated = lines[:row] + replacement + lines[last + 1:]
        delta = len(inserted) - (end - start)
        if delta == 0 and row == last and len(replacement) == 1:
            positions = starts
        else:
            positions = starts[:row]
            for line in replacement:
                positions.append(offset)
                offset += len(line) + 1
            positions.extend(position + delta for position in starts[last + 1:])
        return (updated, positions), lines[row:last + 1], replacement

    def _update_line_widths(self, changes):
        """Keep measured widths only for lines still present in this document."""
        line_counts = self._width_line_counts
        assert line_counts is not None
        for line, delta in changes.items():
            if not delta:
                continue
            count = line_counts.get(line, 0) + delta
            if count:
                line_counts[line] = count
                if line not in self._width_cache:
                    self._width_pending.add(line)
            else:
                del line_counts[line]
                self._width_pending.discard(line)
                width = self._width_cache.pop(line, None)
                if width is not None:
                    self._width_counts[width] -= 1
                    if not self._width_counts[width]:
                        del self._width_counts[width]
                        if width == self._extent_cache:
                            self._extent_cache = None

    def _lines(self):
        if self._line_cache is None:
            lines = self.text.split("\n")
            starts = []
            cursor = 0
            for line in lines:
                starts.append(cursor)
                cursor += len(line) + 1
            self._line_cache = (lines, starts)
        return self._line_cache

    def _ensure_caret(self):
        self._reveal_after_paint = True
        _, starts = self._lines()
        row = bisect.bisect_right(starts, self._caret) - 1
        capacity = self._page_rows()
        self._scroll = max(0, min(self._scroll, row))
        if row >= self._scroll + capacity:
            self._scroll = row - capacity + 1

    def _line_height(self):
        style = resolve_style(self)
        fallback = style.font_size * 1.4
        if not self.multiline:
            return fallback
        return self.effective_row_height(fallback)

    def _page_rows(self):
        style = resolve_style(self)
        return max(
            1, int((self._text_viewport().height - 2 * style.padding) / self._line_height())
        )

    def _clamp_scroll(self, host=None):
        self._scroll = max(0, min(self._scroll, self.line_count - self._page_rows()))
        # Zero is valid for every document extent. Keep ordinary edits bounded
        # to visible-row measurement until horizontal scrolling needs a bound.
        if self._scroll_x <= 0:
            self._scroll_x = 0.0
            return
        self._scroll_x = min(self._scroll_x, self._horizontal_limit(host))

    def _document_width(self, host=None):
        runtime = getattr(self._root(), "_runtime", None)
        host = host or (runtime.host if runtime is not None else self._measure_host)
        if host is None:
            return None
        style = resolve_style(self)
        mono = self.monospace or style.font_family == "mono"
        key = (id(host), getattr(host, "resource_revision", 0), style.font_size,
               mono, self.password, self.tab_size)
        if self._width_line_counts is None:
            self._width_line_counts = Counter(self._lines()[0])
        if self._width_cache_key != key:
            self._width_cache.clear()
            self._width_counts.clear()
            self._width_pending = set(self._width_line_counts)
            self._width_cache_key = key
            self._extent_cache = None
        if self._width_pending:
            advance = host.measure("M", style.font_size, True)[0]
            measure_many = getattr(host, "measure_many", None)
            pending = []
            pending_chars = 0

            def record(line, measured):
                self._width_cache[line] = measured
                self._width_counts[measured] += 1
                self._width_pending.remove(line)
                if self._extent_cache is not None:
                    self._extent_cache = max(self._extent_cache, measured)

            def flush():
                if pending:
                    widths = measure_many([display for _, display in pending],
                                          style.font_size, mono)
                    for (line, _), (width, _) in zip(pending, widths):
                        record(line, width)
                    pending.clear()

            for line in tuple(self._width_pending):
                tabs = self._tab_map(line)
                if self._fixed_line(None, line, mono=mono):
                    measured = (tabs.display_offset(len(line)) if tabs else len(line)) * advance
                    record(line, measured)
                else:
                    display = tabs.expand() if tabs else line
                    if measure_many is None or len(display) > 1024 * 1024:
                        record(line, host.measure(display, style.font_size, mono)[0])
                        continue
                    # Respect the native item limit and leave room for JSON
                    # escaping within its transport frame. Use returned widths
                    # directly: long lines need not fit the host's metric cache.
                    if len(pending) == 4096 or pending_chars + len(display) > 1024 * 1024:
                        flush()
                        pending_chars = 0
                    pending.append((line, display))
                    pending_chars += len(display)
            flush()
        if self._extent_cache is None:
            self._extent_cache = max(self._width_counts, default=0)
        return self._extent_cache

    def _horizontal_limit(self, host=None):
        width = self._document_width(host)
        if width is None:
            return self._scroll_x if self.text else 0.0
        available = max(0, self._text_viewport().right - self._text_left() - 14)
        return max(0, width - available)

    def _sync_scrollbars(self, host=None):
        if not self.multiline:
            self._scroll_axes = (False, False)
            return
        width = self._document_width(host)
        padding = resolve_style(self).padding
        horizontal = vertical = False
        # Either gutter can introduce overflow on the other axis.
        for _ in range(3):
            available = max(0, self._rect.width - (12 if vertical else 0) - self._text_left() - 14)
            horizontal = width is not None and width > available
            page = max(1, int((self._rect.height - (12 if horizontal else 0) - 2 * padding)
                              / self._line_height()))
            vertical = self.line_count > page
        self._scroll_axes = horizontal, vertical

    def _scrollbars(self):
        if not self.multiline:
            return []
        area = self._text_viewport()
        horizontal, vertical = self._scroll_axes
        x, y = self._rect.x, self._rect.y
        bars = []
        if vertical:
            bar = scrollbar(Rect(x + area.right, y, min(12, self._rect.width), area.height),
                            self.line_count, self._page_rows(), self._scroll)
            if bar is not None:
                bars.append(bar)
        if horizontal:
            available = max(0, area.right - self._text_left() - 14)
            bar = scrollbar(Rect(x, y + area.bottom, area.width, min(12, self._rect.height)),
                            available + self._horizontal_limit(), available, self._scroll_x, 0)
            if bar is not None:
                bars.append(bar)
        return bars

    def _text_left(self):
        style = resolve_style(self)
        return (
            style.padding + 8 + len(str(self.line_count)) * style.font_size * 0.65
            if self.line_numbers
            else style.padding + 2
        )

    def _text_viewport(self):
        """Local text area; derived editors can reserve space for adornments."""
        horizontal, vertical = self._scroll_axes if self.multiline else (False, False)
        return Rect(0, 0, max(0, self._rect.width - (12 if vertical else 0)),
                    max(0, self._rect.height - (12 if horizontal else 0)))

    def indent_selection(self, *, unindent=False):
        """Indent selected complete lines as one undo operation."""
        if self.read_only:
            return
        a, b = self.selection_range
        start = self.text.rfind("\n", 0, a) + 1
        end = self.text.find(
            "\n", b - 1 if b > a and self.text[b - 1 : b] == "\n" else b
        )
        if end < 0:
            end = len(self.text)
        lines = self.text[start:end].split("\n")
        if unindent:
            lines = [
                line[1:]
                if line.startswith("\t")
                else line[min(self.tab_size, len(line) - len(line.lstrip(" "))) :]
                for line in lines
            ]
        else:
            lines = [" " * self.tab_size + line for line in lines]
        value = "\n".join(lines)
        self._replace_range(value, start, end)
        self.select(start, start + len(value))

    def _tab_map(self, line):
        if "\t" not in line and not self.password:
            return None
        if line in self._tab_maps:
            self._tab_maps.move_to_end(line)
            return self._tab_maps[line]
        tabs = _MaskStops(line) if self.password else _TabStops(line, self.tab_size)
        # Reuse small current-row display maps; never retain an oversized row.
        if len(line) <= 65536:
            while self._tab_maps and (
                len(self._tab_maps) >= 256 or self._tab_map_chars + len(line) > 65536
            ):
                previous, _ = self._tab_maps.popitem(last=False)
                self._tab_map_chars -= len(previous)
            self._tab_maps[line] = tabs
            self._tab_map_chars += len(line)
        return tabs

    def _fixed_line(self, row, line, *, mono):
        # Fixed-pitch printable ASCII admits exact indexed viewport painting.
        # Other text retains the host's complete shaping/measurement operation.
        if not mono or self.password:
            return False
        if row is None:
            # Composition and document extent scans have no visible row entry.
            return line.isascii() and (line.isprintable() or line.replace("\t", "").isprintable())
        if row not in self._fixed_line_cache:
            if len(self._fixed_line_cache) >= 256:
                self._fixed_line_cache.clear()
            self._fixed_line_cache[row] = line.isascii() and (
                line.isprintable() or line.replace("\t", "").isprintable())
        return self._fixed_line_cache[row]

    def _word_boundary(self, offset, direction):
        """Move across a word/punctuation run and its adjacent whitespace.

        Words contain Unicode letters/numbers or underscore. Each step is a
        whole extended grapheme, including combining marks and emoji sequences.
        """
        text = self.text
        offset = floor_boundary(text, offset)

        def kind(index):
            return _word_kind(text[index])

        if direction > 0:
            if offset < len(text):
                category = kind(offset)
                while offset < len(text) and kind(offset) == category:
                    offset = next_boundary(text, offset)
            while offset < len(text) and kind(offset) == "space":
                offset = next_boundary(text, offset)
        else:
            while offset and kind(previous_boundary(text, offset)) == "space":
                offset = previous_boundary(text, offset)
            if offset:
                category = kind(previous_boundary(text, offset))
                while offset and kind(previous_boundary(text, offset)) == category:
                    offset = previous_boundary(text, offset)
        return offset

    @staticmethod
    def _word_span(line, offset):
        if not line:
            return 0, 0
        start = floor_boundary(line, min(offset, len(line) - 1))
        end = next_boundary(line, start)
        category = _word_kind(line[start])
        while start and _word_kind(line[previous_boundary(line, start)]) == category:
            start = previous_boundary(line, start)
        while end < len(line) and _word_kind(line[end]) == category:
            end = next_boundary(line, end)
        return start, end

    def _pointer_columns(self, row, line, x, host, size, mono):
        """Return the nearest insertion boundary and the grapheme under x."""
        tabs = self._tab_map(line)
        if self._fixed_line(row, line, mono=mono):
            advance = max(host.measure("M", size, True)[0], 0.001)
            column = max(0, int(x / advance))
            low = tabs.source_offset(column) if tabs else min(len(line), column)

            def fixed_width(offset):
                return (tabs.display_offset(offset) if tabs else offset) * advance
            width = fixed_width
        else:
            display = tabs.expand() if tabs else line
            context = (getattr(host, "resource_revision", 0), size, mono,
                       self.password, self.tab_size, line)
            if len(line) <= 65536:
                if self._hit_host is not host or self._hit_context != context:
                    self._hit_widths.clear()
                    self._hit_context, self._hit_host = context, host
                widths = self._hit_widths
            else:
                # Oversized rows only reuse duplicate probes within this call.
                widths = OrderedDict()

            def measured_width(offset):
                if offset in widths:
                    widths.move_to_end(offset)
                    return widths[offset]
                value = host.measure(
                    display[:tabs.display_offset(offset) if tabs else offset], size, mono
                )[0]
                if len(widths) >= 2048:
                    widths.popitem(last=False)
                widths[offset] = value
                return value
            width = measured_width

            low, high = 0, len(line)
            while low < high:
                mid = (low + high + 1) // 2
                if width(mid) <= x:
                    low = mid
                else:
                    high = mid - 1
        low = floor_boundary(line, low)
        high = next_boundary(line, low)
        caret = high if x >= (width(low) + width(high)) / 2 else low
        # Word selection follows the glyph, even when its right half places
        # the caret at the start of the following whitespace or punctuation.
        return caret, low

    def scroll_input(self, e, /) -> bool:
        self._clamp_scroll()
        offset = self._scroll_x if e.shift else self._scroll
        limit = (
            self._horizontal_limit() if e.shift
            else max(0, self.line_count - self._page_rows())
        )
        if not can_scroll(offset, limit, e.delta):
            self._wheel = 0.0
            return False
        self.handle_input(e)
        return True

    def _stop_selection_scroll(self):
        task, self._selection_task = self._selection_task, None
        self._selection_pointer = self._selection_token = None
        if task is not None:
            task.cancel()

    def _selection_can_scroll(self):
        event = self._selection_pointer
        if event is None:
            return False
        area = self._text_viewport()
        x, y = event.x - self._rect.x, event.y - self._rect.y
        return (
            (self.multiline and ((y < 0 and self._scroll > 0)
             or (y >= area.bottom and self._scroll < max(0, self.line_count - self._page_rows()))))
            or (x < 0 and self._scroll_x > 0)
            or (x >= area.right and self._scroll_x < self._horizontal_limit())
        )

    def _sync_selection_scroll(self, event):
        self._selection_pointer = event
        if not self._selection_can_scroll():
            self._stop_selection_scroll()
        elif self._selection_task is None and self._dispatcher is not None:
            token = self._selection_token = object()
            self._selection_task = self.create_task(self._scroll_selection(token))

    async def _scroll_selection(self, token):
        try:
            while self._selection_token is token:
                await asyncio.sleep(0.05)
                runtime = getattr(self._root(), "_runtime", None)
                if runtime is None:
                    break
                runtime.router.reconcile()
                if (self._disposed or not self._pressed or runtime.router.capture is not self
                        or not self._selection_can_scroll()):
                    break
                # Reuse pointer selection (including word/grapheme behavior),
                # limiting each tick even when the pointer is far off screen.
                event = self._selection_pointer
                area = self._text_viewport()
                event = replace(event, kind="pointer_move",
                    x=max(self._rect.x - 16, min(event.x, self._rect.x + area.right + 16)),
                    y=(self._rect.y - self._line_height() if event.y < self._rect.y else
                       min(event.y, self._rect.y + area.bottom + self._line_height())))
                before = self._scroll, self._scroll_x, self._caret
                self.handle_input(event)
                if before == (self._scroll, self._scroll_x, self._caret):
                    break
        finally:
            if self._selection_token is token:
                self._selection_task = self._selection_pointer = self._selection_token = None

    def destroy(self):
        self._stop_selection_scroll()
        super().destroy()

    def handle_input(self, e, /):
        if e.kind in ("blur", "pointer_cancel", "pointer_up", "key_down", "text", "composition", "wheel"):
            self._stop_selection_scroll()
        before = (self._caret, self._anchor, self._scroll, self._scroll_x, self._composition)
        if (e.kind in ("blur", "pointer_down", "pointer_cancel", "composition", "wheel")
                or (e.kind == "pointer_move" and self._pressed)
                or (e.kind == "key_down" and (
                    e.key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
                              "Home", "End", "PageUp", "PageDown", "Enter", "Tab", "Escape")
                    or (e.ctrl and e.key not in ("Backspace", "Delete"))))):
            self._edit_group = None
        if e.kind in ("blur", "pointer_cancel", "key_down", "text", "composition", "wheel"):
            self._last_click = self._word_drag = None
        elif e.kind == "pointer_move" and self._last_click is not None:
            if abs(e.x - self._last_click[1]) > 8 or abs(e.y - self._last_click[2]) > 8:
                self._last_click = None
        if e.kind.startswith("pointer_") or e.kind == "blur":
            self._sync_scrollbars()
            handled, self._scroll_drag, values = handle_scrollbars(
                e, self._scrollbars(), self._scroll_drag)
            if handled:
                self._pressed = False
                self._last_click = self._word_drag = None
                if e.kind in ("blur", "pointer_cancel"):
                    self._composition = ""
                if 1 in values:
                    self._scroll = round(values[1])
                if 0 in values:
                    self._scroll_x = values[0]
                self._clamp_scroll()
                self._reveal_after_paint = False
                if before != (self._caret, self._anchor, self._scroll, self._scroll_x, self._composition):
                    self.invalidate()
                return
        if e.kind in ("blur", "pointer_cancel"):
            self._composition = ""
            self._pressed = False
            self.invalidate()
        elif e.kind == "text":
            group = None if self._composition or e.paste else "typing"
            self._composition = ""
            self._replace_range(e.text, *self.selection_range, group=group)
        elif e.kind == "composition":
            if not self.read_only:
                self._composition = e.text
                self._reveal_after_paint = True
        elif e.kind == "wheel":
            if e.shift:
                self._scroll_x = max(0, self._scroll_x + e.delta * 32)
                self._clamp_scroll()
                self._reveal_after_paint = False
                self.invalidate()
                return
            self._wheel += e.delta * 3
            rows = int(round(self._wheel, 12))
            self._wheel -= rows
            self._scroll += rows
            self._clamp_scroll()
            self._reveal_after_paint = False
        elif e.kind == "pointer_up":
            self._pressed = False
            self._word_drag = None
        elif e.kind == "pointer_down" or (e.kind == "pointer_move" and self._pressed):
            self._pressed = True
            runtime = getattr(self._root(), "_runtime", None)
            if runtime is None:
                return
            lines, starts = self._lines()
            style = resolve_style(self)
            size = style.font_size
            assert size is not None
            lh = self._line_height()
            row = max(
                0,
                min(
                    len(lines) - 1,
                    int((e.y - self._rect.y - style.padding) / lh) + self._scroll,
                ),
            )
            line = lines[row]
            x = (
                min(e.x - self._rect.x, self._text_viewport().right)
                - self._text_left()
                + self._scroll_x
            )
            mono = self.monospace or style.font_family == "mono"
            col, word_col = self._pointer_columns(row, line, x, runtime.host, size, mono)
            self._preferred_column = None
            self._caret = starts[row] + col
            if e.kind == "pointer_down":
                previous, self._last_click = self._last_click, None
                self._word_drag = None
                now = monotonic()
                if not e.shift:
                    self._anchor = self._caret
                    if (e.button == 1 and previous is not None
                            and previous[3:] == (e.button, e.pointer_kind)
                            and 0 <= now - previous[0] <= 0.4
                            and abs(e.x - previous[1]) <= 8 and abs(e.y - previous[2]) <= 8):
                        start, end = self._word_span(line, word_col)
                        self._anchor, self._caret = starts[row] + start, starts[row] + end
                        self._word_drag = self._anchor, self._caret
                    elif e.button == 1:
                        self._last_click = now, e.x, e.y, e.button, e.pointer_kind
            elif self._word_drag is not None:
                start, end = self._word_span(line, word_col)
                first, last = self._word_drag
                start, end = starts[row] + start, starts[row] + end
                self._anchor, self._caret = (
                    (last, start) if start < first else (first, max(last, end)))
            elif self._caret != self._anchor:
                self._last_click = None
            self._ensure_caret()
            self._sync_selection_scroll(e)
        elif e.kind == "key_down":
            key = e.key
            navigation = ("ArrowLeft", "ArrowRight", "Home", "End")
            if e.ctrl and key not in navigation:
                if key.lower() == "a":
                    self.select_all()
                elif key.lower() == "z":
                    self.redo() if e.shift else self.undo()
                elif key.lower() == "y":
                    self.redo()
                elif key in ("Backspace", "Delete") and not self.read_only:
                    anchor = self._anchor
                    if self._caret == self._anchor:
                        anchor = self._word_boundary(
                            self._caret, -1 if key == "Backspace" else 1
                        )
                    self._replace_range("", anchor, self._caret, group="word_" + key)
                return
            if e.ctrl:
                self._preferred_column = None
                if key in ("Home", "End"):
                    self._caret = 0 if key == "Home" else len(self.text)
                else:
                    self._caret = self._word_boundary(self._caret, -1 if key == "ArrowLeft" else 1)
            elif key in ("Backspace", "Delete"):
                if self.read_only:
                    return
                anchor = self._anchor
                if self._caret == self._anchor:
                    anchor = (
                        previous_boundary(self.text, self._caret)
                        if key == "Backspace"
                        else next_boundary(self.text, self._caret)
                    )
                self._replace_range("", anchor, self._caret, group=key)
                return
            elif key in ("ArrowLeft", "ArrowRight"):
                self._preferred_column = None
                if not e.shift and self._caret != self._anchor:
                    self._caret = (
                        min(self._caret, self._anchor)
                        if key == "ArrowLeft"
                        else max(self._caret, self._anchor)
                    )
                else:
                    self._caret = (
                        previous_boundary(self.text, self._caret)
                        if key == "ArrowLeft"
                        else next_boundary(self.text, self._caret)
                    )
            elif key in ("Home", "End", "ArrowUp", "ArrowDown", "PageUp", "PageDown"):
                lines, starts = self._lines()
                row = bisect.bisect_right(starts, self._caret) - 1
                col = self._caret - starts[row]
                if key == "Home":
                    self._preferred_column = None
                    self._caret = starts[row]
                elif key == "End":
                    self._preferred_column = None
                    self._caret = starts[row] + len(lines[row])
                else:
                    style = resolve_style(self)
                    mono = self.monospace or style.font_family == "mono"
                    runtime = getattr(self._root(), "_runtime", None)
                    host = runtime.host if runtime is not None else self._measure_host
                    # Proportional text keeps a pixel position across short
                    # rows. Fixed-pitch text retains its display-column path.
                    context = None
                    if host is not None and not mono:
                        context = (host, style.font_size, getattr(host, "resource_revision", 0),
                                   self.password, self.tab_size)
                    if self._preferred_column is None or self._preferred_column[0] != context:
                        tabs = self._tab_map(lines[row])
                        if context is None:
                            preferred = tabs.display_offset(col) if tabs else col
                        else:
                            prefix = tabs.expand(0, col) if tabs else lines[row][:col]
                            preferred = host.measure(prefix, style.font_size, mono)[0]
                        self._preferred_column = context, preferred
                    preferred = self._preferred_column[1]
                    delta = self._page_rows() if key in ("PageUp", "PageDown") else 1
                    row = max(
                        0, min(len(lines) - 1, row + delta * (-1 if key in ("ArrowUp", "PageUp") else 1))
                    )
                    if context is not None:
                        column, _ = self._pointer_columns(
                            row, lines[row], preferred, host, style.font_size, mono)
                    else:
                        tabs = self._tab_map(lines[row])
                        column = (tabs.source_offset(preferred) if tabs
                                  else min(preferred, len(lines[row])))
                    self._caret = starts[row] + column
                    self._caret = starts[row] + floor_boundary(
                        lines[row], self._caret - starts[row]
                    )
            elif key == "Enter" and self.multiline:
                insertion = min(self._caret, self._anchor)
                prefix = self.text[
                    self.text.rfind("\n", 0, insertion) + 1 : insertion
                ]
                indent = (
                    prefix[: len(prefix) - len(prefix.lstrip(" \t"))]
                    if self.auto_indent
                    else ""
                )
                if self.auto_indent and prefix.rstrip().endswith(":"):
                    indent += " " * self.tab_size
                self.replace_selection("\n" + indent)
                return
            elif key == "Tab" and self.multiline:
                if e.shift or self._caret != self._anchor:
                    self.indent_selection(unindent=e.shift)
                else:
                    self.replace_selection(" " * self.tab_size)
                return
            else:
                return
            if not e.shift:
                self._anchor = self._caret
            self._ensure_caret()
        if before != (self._caret, self._anchor, self._scroll, self._scroll_x, self._composition):
            self.invalidate()

    def paint(self, p, /):
        s = p.body()
        self._measure_host = p.host
        self._sync_scrollbars(p.host)
        viewport = self._text_viewport()
        with p._clipped(viewport):
            self._paint_text(p, s, viewport)
        paint_scrollbars(p, self._scrollbars())

    def _paint_text(self, p, s, viewport):
        self._measure_host = p.host
        mono = self.monospace or s.font_family == "mono"
        metrics = (viewport.width, viewport.height, self._line_height(), s.padding, mono, self.tab_size)
        if self._text_viewport_metrics is not None and metrics != self._text_viewport_metrics:
            self._reveal_after_paint = True
        self._text_viewport_metrics = metrics
        size = s.font_size
        padding = s.padding
        lh = self._line_height()
        t = p.theme.tokens
        lines, starts = self._lines()
        a, b = sorted((self._caret, self._anchor))
        runtime = getattr(self._root(), "_runtime", None)
        focused = runtime is not None and runtime.router.focus is self
        composition = (
            self._normalize_text(self._composition, self.multiline) if focused else ""
        )
        preview_start = preview_stop = preview_delta = 0
        if composition:
            # Preview selection replacement without changing the document, its
            # line index, or history. Only the affected rows lose decoration:
            # subclass spans still refer to the committed document's offsets.
            preview_start = bisect.bisect_right(starts, a) - 1
            last = bisect.bisect_right(starts, b) - 1
            prefix = lines[preview_start][:a - starts[preview_start]]
            suffix = lines[last][b - starts[last]:]
            replacement = (prefix + composition + suffix).split("\n")
            lines = lines[:preview_start] + replacement + lines[last + 1:]
            preview_stop = preview_start + len(replacement)
            preview_delta = last + 1 - preview_stop
            parts = composition.split("\n")
            row = preview_start + len(parts) - 1
            column = len(parts[-1]) + (len(prefix) if len(parts) == 1 else 0)
            capacity = self._page_rows()
            # Keep transient offsets across repaints: the committed document
            # may be smaller than this preview. Wheel input still owns its
            # scroll position until a new composition/navigation requests reveal.
            self._scroll = max(0, min(self._scroll, len(lines) - capacity))
            if self._reveal_after_paint:
                self._scroll = min(self._scroll, row)
                if row >= self._scroll + capacity:
                    self._scroll = row - capacity + 1
        else:
            self._clamp_scroll(p.host)
            row = bisect.bisect_right(starts, self._caret) - 1
            column = self._caret - starts[row]
        if focused and self._reveal_after_paint and not composition:
            self._ensure_caret()
            self._clamp_scroll(p.host)
        text_left = self._text_left()
        if not self.text and self.placeholder and not focused:
            placeholder = self.placeholder if self.multiline else single_line(self.placeholder)
            p.text(
                p.elide(placeholder, max(0, viewport.right - text_left),
                        size=size, mono=self.monospace),
                text_left, padding, color=t.muted, size=size, mono=self.monospace,
            )
        advance = p.measure("M", size=size, mono=True)[0]
        caret_tabs = self._tab_map(lines[row])
        if self._fixed_line(None if composition else row, lines[row], mono=mono):
            caret_x = (caret_tabs.display_offset(column) if caret_tabs else column) * advance
        else:
            prefix = caret_tabs.expand(0, column) if caret_tabs else lines[row][:column]
            caret_x = p.measure(prefix, size=size, mono=self.monospace)[0]
        if focused and self._reveal_after_paint:
            self._scroll_x = max(0, min(self._scroll_x, caret_x))
            available = max(0, viewport.right - text_left - 14)
            if caret_x - self._scroll_x > available:
                self._scroll_x = max(0, caret_x - available)
            self._reveal_after_paint = False
        first = self._scroll + max(
            0, floor((p._clip.y - self._rect.y - padding) / lh)
        )
        end = min(
            len(lines),
            self._scroll
            + max(0, ceil((p._clip.bottom - self._rect.y - padding) / lh)),
        )
        for i in range(first, end):
            line = lines[i]
            tabs = self._tab_map(line)
            display = tabs.expand() if self.password and tabs is not None else line
            y = padding + (i - self._scroll) * lh
            source_row = i
            if composition and i >= preview_start:
                source_row = None if i < preview_stop else i + preview_delta
            left = right = 0
            if not composition:
                left = max(a, starts[i]) - starts[i]
                right = min(b, starts[i] + len(line)) - starts[i]
            fixed = self._fixed_line(source_row, line, mono=mono)
            if right > left:
                display_left = tabs.display_offset(left) if tabs else left
                display_right = tabs.display_offset(right) if tabs else right
                x = (
                    display_left * advance
                    if fixed
                    else p.measure(tabs.expand(0, left) if tabs else display[:left],
                                   size=size, mono=self.monospace)[0]
                )
                w = (
                    (display_right - display_left) * advance
                    if fixed
                    else p.measure(tabs.expand(0, right) if tabs else display[:right],
                                   size=size, mono=self.monospace)[0] - x
                )
                p.rect(Rect(text_left + x - self._scroll_x, y, w, lh), t.selection)
            can_fragment = (
                source_row is None
                or type(self).paint_line is TextBox.paint_line
                or type(self).paint_line_fragment is not TextBox.paint_line_fragment
            )
            if fixed and advance > 0 and can_fragment:
                clip_left = max(0, p._clip.x - self._rect.x - text_left)
                clip_right = max(0, p._clip.right - self._rect.x - text_left)
                first_column = max(0, floor((self._scroll_x + clip_left) / advance) - 1)
                last_column = ceil((self._scroll_x + clip_right) / advance) + 1
                start_col = tabs.source_offset(first_column) if tabs else first_column
                end_col = (min(len(line), tabs.source_offset(last_column) + 1) if tabs
                           else min(len(display), last_column))
                x = text_left + (tabs.display_offset(start_col) if tabs else start_col) * advance - self._scroll_x
                if source_row is None:
                    fragment = tabs.expand(start_col, end_col) if tabs else display[start_col:end_col]
                    TextBox.paint_line(self, p, fragment, x, y, i, s)
                else:
                    self.paint_line_fragment(
                        p, display[start_col:end_col], x, y, source_row, s, start_col,
                    )
            else:
                if source_row is None:
                    TextBox.paint_line(self, p, display, text_left - self._scroll_x, y, i, s)
                else:
                    self.paint_line(p, display, text_left - self._scroll_x, y, source_row, s)
            if focused and i == row:
                x = text_left + caret_x - self._scroll_x
                p.caret(x, y, lh, t.accent)
            if self.line_numbers:
                p.rect(Rect(1, y, text_left - 6, lh), s.fill)
                p.text(str(i + 1), 5, y, size=size, color=t.muted, mono=True)

    def paint_line_fragment(self, painter, text, x, y, row, style, start_column):
        """Paint a visible line fragment; offsets refer to the original line.

        Decorators with indexed spans can override this alongside paint_line.
        """
        if "\t" in text and not self.password:
            tabs = self._tab_map(self._lines()[0][row])
            if tabs is not None:
                text = tabs.expand(start_column, start_column + len(text))
        self.paint_line(painter, text, x, y, row, style)

    def paint_line(self, painter, text, x, y, row, style):
        """Public extension point for text decoration; selection/caret stay shared."""
        painter.text(
            text.expandtabs(self.tab_size) if "\t" in text and not self.password else text,
            x,
            y,
            color=style.foreground,
            size=style.font_size,
            mono=self.monospace,
        )


# Retain the historical import and qualified class identity.
TextBox.__module__ = "pysual.widgets"
