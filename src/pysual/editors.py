"""Small typed value editors; transient editing uses the shared popup path."""

from math import isfinite
from typing import ClassVar

from .behaviors import PressBehavior
from ._ranges import NumericRange
from .controls import Container, Control, Label
from .editing import EditorContent, EditorPopup
from .events import ChangeEvent, Event, KeyEvent
from .geometry import Rect
from .schema import Dirty, prop
from .theme import _color
from .widgets import Slider, TextBox


class NumericInput(Control):
    cache_paint: bool = prop(default=True)
    value: float = prop(default=0.0, changed="changed")
    minimum: float | None = prop(default=None)
    maximum: float | None = prop(default=None)
    step: float = prop(default=1.0, minimum=0.000001)
    decimals: int = prop(default=2, minimum=0)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[float]]] = Event(ChangeEvent)

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior(keys=())

    def destroy(self) -> None:
        self._press.reset(self)
        super().destroy()

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        data = {**self._values, name: value}
        bounds = NumericRange(data["minimum"], data["maximum"])
        if not bounds.contains(data["value"]):
            raise ValueError("NumericInput requires minimum <= value <= maximum")
        if not bounds.is_ordered():
            raise ValueError("minimum must not exceed maximum")
        if data["decimals"] > 8:
            raise ValueError("decimals must be between 0 and 8")

    def measure(self, host):
        return 180, self._control_height(host)

    def set_range(
        self,
        minimum: float | None,
        maximum: float | None,
        *,
        value: float | None = None,
    ) -> None:
        """Commit bounds and value together; omitted value is clamped to the bounds.

        A None bound remains unbounded, matching the individual properties.
        Explicit values outside the candidate range fail without any mutation.
        """
        self._check_live()
        self._schema["minimum"].validate(minimum)
        self._schema["maximum"].validate(maximum)
        bounds = NumericRange(minimum, maximum)
        if not bounds.is_ordered():
            raise ValueError("minimum must not exceed maximum")
        candidate = self.value if value is None else value
        self._schema["value"].validate(candidate)
        if value is None:
            candidate = bounds.clamp(candidate)
        elif not bounds.contains(candidate):
            raise ValueError("NumericInput requires minimum <= value <= maximum")
        self.update(minimum=minimum, maximum=maximum, value=candidate)

    def increment(self, steps=1):
        self._check_live()
        if not self.effective_enabled:
            return
        value = NumericRange(self.minimum, self.maximum).clamp(
            round(self.value + self.step * steps, 10)
        )
        if isfinite(value):
            self.value = value

    def edit(self):
        """Open the numeric editor and return without waiting for a value.
        Requires a running App; disabled controls do nothing. Apply or Enter
        commits a valid value as a user change; dismissal commits no edit.
        See [popup behavior](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#popup-content).
        """
        self._check_live()
        if self.effective_enabled:
            _ValuePopup(self).show(self)

    def _create_editor_content(self):
        return NumericEditor(self.value, decimals=self.decimals)

    def paint(self, p, /):
        style = p.body()
        p.icon("minus", 8, (p.height - 20) / 2, color=style.foreground)
        p.icon(
            "plus", max(8, p.width - 28), (p.height - 20) / 2, color=style.foreground
        )
        text = p.elide(
            f"{self.value:.{self.decimals}f}",
            max(0, p.width - 76),
            size=style.font_size,
        )
        width, height = p.measure(text, size=style.font_size)
        p.text(
            text,
            (p.width - width) / 2,
            (p.height - height) / 2,
            color=style.foreground,
            size=style.font_size,
        )

    def _pointer_part(self, event):
        x = event.x - self.bounds.x
        return -1 if x < 36 else 1 if x >= self.bounds.width - 36 else 0

    def _popup_press_passthrough(self, event):
        return self._pointer_part(event) != 0

    def handle_input(self, event, /):
        part = self._pointer_part(event)
        if self._press.handle_input(self, event, part=part):
            self.increment(part) if part else self.edit()
        elif event.kind == "key_down":
            if event.key in ("ArrowUp", "ArrowRight", "ArrowDown", "ArrowLeft"):
                self.increment(
                    (1 if event.key in ("ArrowUp", "ArrowRight") else -1)
                    * (10 if event.shift else 1)
                )
            elif event.key in ("Enter", "Space"):
                self.edit()
            elif event.key == "Home" and self.minimum is not None:
                self.value = self.minimum
            elif event.key == "End" and self.maximum is not None:
                self.value = self.maximum


class ColorPicker(Control):
    cache_paint: bool = prop(default=True)
    value: str = prop(default="#7289FA", changed="changed")
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[str]]] = Event(ChangeEvent)

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior(keys=())

    def destroy(self) -> None:
        self._press.reset(self)
        super().destroy()

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "value":
            _color(value)

    def measure(self, host):
        return 180, self._control_height(host)

    def edit(self):
        """Open the color editor and return without waiting for a value.
        Requires a running App; disabled controls do nothing. Apply or Enter
        commits a valid color as a user change; dismissal commits no edit.
        See [popup behavior](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#popup-content).
        """
        self._check_live()
        if self.effective_enabled:
            _ValuePopup(self).show(self)

    def _create_editor_content(self):
        return ColorEditor(self.value)

    def paint(self, p, /):
        style = p.body()
        for row in range(2):
            for column in range(4):
                p.rect(
                    Rect(8 + column * 6, (p.height - 24) / 2 + row * 12, 6, 12),
                    "#eeeeee" if (row + column) % 2 else "#777777",
                )
        p.rect(Rect(8, (p.height - 24) / 2, 24, 24), self.value, border=style.border)
        p.text(
            p.elide(self.value.upper(), max(0, p.width - 48)),
            44,
            (p.height - style.font_size * 1.2) / 2,
            color=style.foreground,
            size=style.font_size,
        )

    def handle_input(self, event, /):
        if self._press.handle_input(self, event):
            self.edit()
        elif event.kind == "key_down" and event.key in ("Enter", "Space"):
            self.edit()


class _ColorEntry(TextBox):
    def _changed(self, field, old, value):
        super()._changed(field, old, value)
        editor = getattr(self, "_editor_content", None)
        if field.name == "text" and editor is not None:
            # Keep the next slider input in sync, even within one input batch.
            editor._sync_channels(value)


class _ColorChannel(Slider):
    def _changed(self, field, old, value):
        super()._changed(field, old, value)
        if field.name == "value" and self._origin == "user":
            editor = getattr(self, "_editor_content", None)
            if editor is not None:
                # Internal state follows input order synchronously. Queued
                # notifications cannot overwrite a subsequent text edit, even
                # when multiple events have identical clock timestamps.
                editor._sync_entry()


class _TextEditorContent(EditorContent):
    def is_untouched(self) -> bool:
        # Display precision is not storage precision. An untouched draft must
        # also preserve external value changes made while the editor was open.
        return self.entry.text == self._initial_text and not self.entry.can_undo

    def entry_on_key_down(self, event: KeyEvent):
        if event.key == "Enter":
            self.submit()


class NumericEditor(_TextEditorContent):
    """Reusable numeric draft content, independent of its inline/popup host."""

    hint: ClassVar[str] = "Enter a number; Escape cancels"

    def __init__(self, value: float, *, decimals: int = 2, **properties: object):
        NumericInput._schema["value"].validate(value)
        NumericInput._schema["decimals"].validate(decimals)
        if decimals > 8:
            raise ValueError("decimals must be between 0 and 8")
        self._decimals = decimals
        super().__init__(value, **properties)

    def build(self, value):
        self.entry = TextBox(text=f"{value:.{self._decimals}f}", height=38)
        self._initial_text = self.entry.text
        self.entry.select_all()

    def read(self) -> float:
        value = float(self.entry.text)
        NumericInput._schema["value"].validate(value)
        return value

    def error_message(self, error, owner) -> str:
        try:
            value = float(self.entry.text)
        except ValueError:
            return "Enter a finite number."
        if not isfinite(value):
            return "Enter a finite number."
        low, high = getattr(owner, "minimum", None), getattr(owner, "maximum", None)
        if (low is not None and value < low) or (high is not None and value > high):
            if low is not None and high is not None:
                return f"Enter a number from {low:g} to {high:g}."
            if low is not None:
                return f"Enter a number at least {low:g}."
            return f"Enter a number at most {high:g}."
        return str(error)


class ColorEditor(_TextEditorContent):
    """RGBA draft content with synchronous text/channel synchronization."""

    hint: ClassVar[str] = "RGBA hex or channel sliders"
    popup_size: ClassVar[tuple[float, float]] = (300, 320)

    def __init__(self, value: str, **properties: object):
        super().__init__(value, **properties)

    def build(self, value):
        _color(value)
        self._channels_rgba = []
        self.entry = _ColorEntry(text=value, height=38)
        self._initial_text = self.entry.text
        self.entry.select_all()
        self.entry._editor_content = self
        rgba = value.lstrip("#") + ("ff" if len(value) == 7 else "")
        for index, name in enumerate(("Red", "Green", "Blue", "Alpha")):
            row = Container(
                parent=self, layout="stack", direction="horizontal", height=30, spacing=6,
            )
            Label(parent=row, text=name, width=46, font_size=12)
            slider = _ColorChannel(
                parent=row, value=int(rgba[index * 2 : index * 2 + 2], 16),
                maximum=255, step=1, flex=1,
            )
            self._channels_rgba.append(slider)
            slider._editor_content = self

    def read(self) -> str:
        value = self.entry.text.strip().upper()
        _color(value)
        return value

    def equivalent(self, value, current) -> bool:
        value, current = value.upper(), current.upper()
        return (value + ("FF" if len(value) == 7 else "")) == (
            current + ("FF" if len(current) == 7 else "")
        )

    def error_message(self, error, owner) -> str:
        try:
            _color(self.entry.text.strip())
        except ValueError:
            return "Use #RRGGBB or #RRGGBBAA."
        return str(error)

    def _sync_channels(self, text):
        text = text.strip()
        try:
            _color(text)
        except ValueError:
            return
        rgba = text[1:] + ("ff" if len(text) == 7 else "")
        for index, slider in enumerate(self._channels_rgba):
            previous = slider._origin
            slider._origin = "program"
            try:
                slider.value = int(rgba[index * 2 : index * 2 + 2], 16)
            finally:
                slider._origin = previous

    def _sync_entry(self):
        self.entry.text = (
            "#" + "".join(f"{round(s.value):02X}" for s in self._channels_rgba)
        )


class _ValuePopup(EditorPopup):
    """Compatibility wrapper; each owner supplies its editor content explicitly."""

    def __init__(self, owner):
        super().__init__(owner, owner._create_editor_content())

    @property
    def entry(self):
        return self.content.entry

    @property
    def _channels_rgba(self):
        return getattr(self.content, "_channels_rgba", ())
