"""A reusable range field with synchronous child coordination and public events."""

from typing import ClassVar, Literal

from pysual import (
    App, ChangeEvent, Container, Dirty, Event, Label, NumericInput, Slider, prop,
)
from pysual.schema import Field, expect_number, expect_text


def _reject_child_value(control: Slider | NumericInput, name: str, value: object) -> None:
    """Reject a child edit before that child commits or emits a change."""
    owner = control._parent
    if name == "value" and isinstance(owner, RangeField) and not owner._syncing:
        owner._validate_candidate({"value": value})


def _publish_child_value(control: Slider | NumericInput, field: Field, value: object) -> None:
    owner = control._parent
    if field.name == "value" and isinstance(owner, RangeField) and not owner._syncing:
        owner._accept_value(expect_number(value), control._origin)


class _RangeSlider(Slider):
    """Coordinate with the owning range before queued handlers run."""

    def _validate_live_update(self, name: str, value: object) -> None:
        super()._validate_live_update(name, value)
        _reject_child_value(self, name, value)

    def _changed(self, field: Field, old: object, value: object) -> None:
        super()._changed(field, old, value)
        _publish_child_value(self, field, value)


class _RangeNumber(NumericInput):
    """Coordinate with the owning range before queued handlers run."""

    def _validate_live_update(self, name: str, value: object) -> None:
        super()._validate_live_update(name, value)
        _reject_child_value(self, name, value)

    def _changed(self, field: Field, old: object, value: object) -> None:
        super()._changed(field, old, value)
        _publish_child_value(self, field, value)


class RangeField(Container):
    """A labeled 0–100 value; change value to keep both child editors in sync."""

    caption: str = prop(default="Level")
    value: float = prop(default=0.0, minimum=0, changed="changed")
    changed: ClassVar[Event[ChangeEvent[float]]] = Event(ChangeEvent)
    layout: Literal["absolute", "stack", "grid", "flow", "dock"] = prop(
        default="stack", affects=Dirty.MEASURE,
    )
    direction: Literal["vertical", "horizontal"] = prop(
        default="horizontal", affects=Dirty.MEASURE,
    )

    def _initialize(self) -> None:
        super()._initialize()
        self._syncing = False
        # Each RangeField owns its children, so instances can repeat the same
        # child names without sharing controls or coordination state.
        self.heading = Label(text=self.caption, width=110)
        self.track = _RangeSlider(value=self.value, minimum=0, maximum=100, flex=1)
        self.number = _RangeNumber(value=self.value, minimum=0, maximum=100,
                                   decimals=0, width=150)

    def _validate_update(self, name: str, value: object) -> None:
        super()._validate_update(name, value)
        if name == "value" and expect_number(value) > 100:
            raise ValueError("RangeField value must be between 0 and 100")

    def _changed(self, field: Field, old: object, value: object) -> None:
        if field.name == "caption":
            self.heading.text = expect_text(value)
        elif field.name == "value":
            value = expect_number(value)
            self._syncing = True
            try:
                self.update_children({self.track: {"value": value},
                                      self.number: {"value": value}})
            finally:
                self._syncing = False
        super()._changed(field, old, value)

    def _accept_value(
        self, value: int | float, origin: Literal["user", "program", "system"]
    ) -> None:
        previous = self._origin
        self._origin = origin
        try:
            self.value = value
        finally:
            self._origin = previous


class Demo(App):
    def build(self):
        self.title = "Composed controls"
        self.width, self.height = 720, 280
        self.layout, self.padding, self.spacing = "stack", 24, 20
        self.heading = Label(text="One component, two independent instances", font_size=22)
        self.volume = RangeField(caption="Volume", value=35, height=42)
        self.brightness = RangeField(caption="Brightness", value=70, height=42)
        self.summary = Label(text="")
        self._update_summary()

    def _update_summary(self):
        self.summary.text = f"Volume: {self.volume.value:g} / Brightness: {self.brightness.value:g}"

    def volume_on_changed(self, event):
        self._update_summary()

    def brightness_on_changed(self, event):
        self._update_summary()


if __name__ == "__main__":
    from pysual import autoconfig as autoconfig
    from argparse import ArgumentParser

    ArgumentParser(description=__doc__).parse_args()
    Demo().run_blocking()
