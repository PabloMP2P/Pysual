"""ISO date selection assembled from reusable draft content and buttons."""

from calendar import monthrange
from datetime import date
from typing import ClassVar

from .behaviors import PressBehavior
from .controls import Button, Container, Control, Label
from .editing import EditorContent, EditorPopup
from .events import ChangeEvent, Event, KeyEvent
from .geometry import Rect
from .schema import Dirty, prop
from .text import TextBox


_FIRST = "0001-01-01"
_LAST = "9999-12-31"
_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _iso_date(value: str, name: str = "value") -> date:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be an ISO date string (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must be a valid ISO date (YYYY-MM-DD)") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be a canonical ISO date (YYYY-MM-DD)")
    return parsed


def _date_range(value: str, minimum: str, maximum: str):
    selected = _iso_date(value)
    low, high = _iso_date(minimum, "minimum"), _iso_date(maximum, "maximum")
    if low > high:
        raise ValueError("minimum must not exceed maximum")
    if not low <= selected <= high:
        raise ValueError("DatePicker requires minimum <= value <= maximum")
    return selected, low, high


class _CalendarNavButton(Button):
    """Month navigation uses a secondary face while retaining button behavior."""

    style_fallbacks: ClassVar[tuple[str, ...]] = ("Dropdown",)
    style_excludes: ClassVar[tuple[str, ...]] = ("Button",)


class _DayButton(Button):
    """A regular button with a selected surface and calendar key commands."""

    # Calendar cells are selectable items, not primary command surfaces. Reuse
    # the existing themed row states while keeping Button input/lifetime.
    style_fallbacks: ClassVar[tuple[str, ...]] = ("ListView",)
    style_excludes: ClassVar[tuple[str, ...]] = ("Button",)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "row")
    selected: bool = prop(default=False)

    def measure(self, host):
        return 32, self._control_height(host)

    def paint(self, p, /):
        style = p.style("row", selected=self.selected)
        p.surface(Rect(0, 0, p.width, p.height), style)
        text = p.elide(self.text, max(0, p.width - 4), size=style.font_size)
        width, height = p.measure(text, size=style.font_size)
        p.text(text, max(0, (p.width - width) / 2), max(0, (p.height - height) / 2),
               color=style.foreground, size=style.font_size)

    def activate(self):
        self._check_activation()
        if self.effective_enabled:
            # Update the draft before queued click observers run. The same
            # stable button may represent another month by the next callback.
            self._editor.select(self._date_value)
        super().activate()

    def handle_input(self, event, /):
        if event.kind == "key_down" and event.key in (
            "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
            "Home", "End", "PageUp", "PageDown", "Enter",
        ):
            if self.effective_enabled:
                previous = self._editor._origin
                self._editor._origin = self._origin
                try:
                    self._editor._day_key(self._date_value, event.key)
                finally:
                    self._editor._origin = previous
            return
        super().handle_input(event)


class CalendarEditor(EditorContent):
    """A reusable Monday-first month grid for one canonical ISO date.

    Mount inline with parent=, or pass fresh content to EditorPopup. select()
    changes only the draft; read() returns it. Arrow keys move by day/week,
    PageUp/PageDown by month, Home/End to month edges, and Enter submits.
    A text field also accepts YYYY-MM-DD directly. Month navigation alone
    preserves an untouched draft.
    """

    hint: ClassVar[str] = "YYYY-MM-DD or choose a day; Enter accepts"
    popup_size: ClassVar[tuple[float, float]] = (348, 456)

    def __init__(
        self, value: str, *, minimum: str = _FIRST, maximum: str = _LAST,
        **properties: object,
    ):
        self._selected, self._minimum, self._maximum = _date_range(value, minimum, maximum)
        self._view_year, self._view_month = self._selected.year, self._selected.month
        self._untouched = True
        super().__init__(value, **properties)

    def build(self, value):
        self.entry = TextBox(text=value, placeholder="YYYY-MM-DD", height=38,
                             tooltip="Date (YYYY-MM-DD)")
        self._initial_text = value
        self.entry.select_all()
        self.header = Container(layout="stack", direction="horizontal", height=36, spacing=6)
        self.previous_button = _CalendarNavButton(parent=self.header, icon="chevron_left", width=34,
                                      tooltip="Previous month")
        self.month_label = Label(parent=self.header, flex=1)
        self.next_button = _CalendarNavButton(parent=self.header, icon="chevron_right", width=34,
                                  tooltip="Next month")
        self.weekdays = Container(layout="grid", column_tracks=("1*",) * 7,
                                  row_tracks=(22,), spacing=4, height=22)
        for column, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
            Label(parent=self.weekdays, text=name, grid_row=0, grid_col=column, font_size=12)
        self.days = Container(layout="grid", column_tracks=("1*",) * 7,
                              row_tracks=("1*",) * 6, spacing=4, height=224)
        self._day_buttons = []
        for index in range(42):
            button = _DayButton(parent=self.days, grid_row=index // 7, grid_col=index % 7)
            button._editor = self
            button._date_value = ""
            self._day_buttons.append(button)
        self._refresh()

    @property
    def displayed_month(self) -> tuple[int, int]:
        self._check_live()
        return self._view_year, self._view_month

    def read(self) -> str:
        self._check_live()
        value = _iso_date(self.entry.text)
        if not self._minimum <= value <= self._maximum:
            raise ValueError("Date is outside the calendar bounds")
        return value.isoformat()

    def is_untouched(self) -> bool:
        return self._untouched and self.entry.text == self._initial_text and not self.entry.can_undo

    def entry_on_key_down(self, event: KeyEvent):
        if event.key == "Enter":
            self.submit()

    def select(self, value: str) -> None:
        """Select a valid draft date without committing to an owner."""
        self._check_live()
        selected = _iso_date(value)
        if not self._minimum <= selected <= self._maximum:
            raise ValueError("Date is outside the calendar bounds")
        self._selected = selected
        self._untouched = False
        self.entry.load_text(value)
        self.entry.select_all()
        self._view_year, self._view_month = selected.year, selected.month
        self._refresh()

    def navigate(self, months: int) -> None:
        """Move the displayed month, clamped to months containing allowed dates."""
        self._check_live()
        if type(months) is not int:
            raise TypeError("Month offset must be an integer")
        current = self._view_year * 12 + self._view_month - 1
        low = self._minimum.year * 12 + self._minimum.month - 1
        high = self._maximum.year * 12 + self._maximum.month - 1
        target = max(low, min(high, current + months))
        if target != current:
            year, month = divmod(target, 12)
            self._view_year, self._view_month = year, month + 1
            self._refresh()

    def previous_button_on_click(self, event):
        self.navigate(-1)

    def next_button_on_click(self, event):
        self.navigate(1)

    def _refresh(self):
        year, month = self._view_year, self._view_month
        first, count = monthrange(year, month)
        self.month_label.text = f"{_MONTHS[month - 1]} {year:04d}"
        self.previous_button.enabled = (year, month) > (self._minimum.year, self._minimum.month)
        self.next_button.enabled = (year, month) < (self._maximum.year, self._maximum.month)
        updates = {}
        for index, button in enumerate(self._day_buttons):
            day = index - first + 1
            current = date(year, month, day) if 1 <= day <= count else None
            button._date_value = current.isoformat() if current else ""
            updates[button] = {
                "text": str(day) if current else "",
                "visible": current is not None,
                "enabled": current is not None and self._minimum <= current <= self._maximum,
                "selected": current == self._selected,
                "tooltip": button._date_value,
            }
        self.update_children(updates)

    def _day_key(self, value, key):
        current = _iso_date(value)
        if key == "Enter":
            self.select(value)
            self.submit()
            return
        offsets = {"ArrowLeft": -1, "ArrowRight": 1, "ArrowUp": -7, "ArrowDown": 7}
        if key in offsets:
            ordinal = max(self._minimum.toordinal(), min(
                self._maximum.toordinal(), current.toordinal() + offsets[key],
            ))
            target = date.fromordinal(ordinal)
        elif key in ("PageUp", "PageDown"):
            month_index = current.year * 12 + current.month - 1 + (1 if key == "PageDown" else -1)
            month_index = max(12, min(9999 * 12 + 11, month_index))
            year, month = divmod(month_index, 12)
            target = date(year, month + 1, min(current.day, monthrange(year, month + 1)[1]))
        else:
            day = 1 if key == "Home" else monthrange(current.year, current.month)[1]
            target = date(current.year, current.month, day)
        target = max(self._minimum, min(self._maximum, target))
        self.select(target.isoformat())
        self._focus_selected()

    def _focus_selected(self):
        for button in self._day_buttons:
            if button._date_value == self._selected.isoformat():
                button.focus()
                break


class DatePicker(Control):
    """Single-date picker using canonical Gregorian YYYY-MM-DD strings."""

    cache_paint: bool = prop(default=True)
    value: str = prop(default="2000-01-01", changed="changed")
    minimum: str = prop(default=_FIRST)
    maximum: str = prop(default=_LAST)
    read_only: bool = prop(default=False)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[str]]] = Event(ChangeEvent)
    style_fallbacks: ClassVar[tuple[str, ...]] = ("TextBox",)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "icon")

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior(keys=())
        self._popup = None

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name in ("value", "minimum", "maximum"):
            values = {**self._values, name: value}
            _date_range(values["value"], values["minimum"], values["maximum"])

    def _changed(self, field, old, value):
        if ((field.name == "read_only" and value)
                or (field.name in ("enabled", "visible") and not value)):
            if self._popup is not None and self._popup.is_open:
                self._popup.dismiss()
        super()._changed(field, old, value)

    def open(self) -> None:
        """Open a calendar draft; Apply/Enter commits, dismissal cancels.

        Requires a running App. Disabled/read-only controls do nothing.
        Programmatic value updates remain allowed when read_only is true.
        """
        self._check_live()
        if not self.effective_enabled or self.read_only:
            return
        if self._popup is not None and self._popup.is_open:
            self._popup.dismiss()
            return
        content = CalendarEditor(self.value, minimum=self.minimum, maximum=self.maximum)
        self._popup = EditorPopup(self, content)
        self._popup.show(self)
        content._focus_selected()

    def measure(self, host):
        return 180, self._control_height(host)

    def paint(self, p, /):
        style = p.body()
        text = p.elide(self.value, max(0, p.width - 42), size=style.font_size)
        _, height = p.measure(text, size=style.font_size)
        p.text(text, 10, max(0, (p.height - height) / 2),
               color=style.foreground, size=style.font_size)
        color = p.style("icon").foreground
        x, y = max(0, p.width - 26), (p.height - 16) / 2
        p.rect(Rect(x, y, 16, 16), "", radius=2, border=color)
        p.line(x, y + 5, x + 16, y + 5, color)
        p.line(x + 4, y - 2, x + 4, y + 2, color)
        p.line(x + 12, y - 2, x + 12, y + 2, color)

    def handle_input(self, event, /):
        if self._press.handle_input(self, event):
            self.open()
        elif event.kind == "key_down" and event.key in ("Enter", "Space", "ArrowDown"):
            self.open()

    def destroy(self) -> None:
        self._press.reset(self)
        if self._popup is not None and self._popup.is_open:
            self._popup.destroy()
        super().destroy()
