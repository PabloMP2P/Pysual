"""A reusable range field with synchronous child coordination and public events."""

from typing import ClassVar

from pysual import App, ChangeEvent, Container, Event, Label, NumericInput, Slider, prop


class _RangeValue:
    """Coordinate child values before queued application handlers run."""

    def _validate_live_update(self, name, value):
        super()._validate_live_update(name, value)
        owner = self._parent
        if name == "value" and isinstance(owner, RangeField) and not owner._syncing:
            # Reject the edit before the child commits or emits a change.
            owner._validate_candidate({"value": value})

    def _changed(self, field, old, value):
        super()._changed(field, old, value)
        owner = self._parent
        if field.name == "value" and isinstance(owner, RangeField) and not owner._syncing:
            owner._accept_value(value, self._origin)


class _RangeSlider(_RangeValue, Slider):
    pass


class _RangeNumber(_RangeValue, NumericInput):
    pass


class RangeField(Container):
    """A labeled 0–100 value; change value to keep both child editors in sync."""

    caption: str = prop(default="Level")
    value: float = prop(default=0.0, minimum=0, changed="changed")
    changed: ClassVar[Event[ChangeEvent[float]]] = Event(ChangeEvent)

    def __init__(self, **properties):
        super().__init__(**{"layout": "stack", "direction": "horizontal", **properties})
        self._syncing = False
        # Each RangeField owns its children, so instances can repeat the same
        # child names without sharing controls or coordination state.
        self.heading = Label(text=self.caption, width=110)
        self.track = _RangeSlider(value=self.value, minimum=0, maximum=100, flex=1)
        self.number = _RangeNumber(value=self.value, minimum=0, maximum=100,
                                   decimals=0, width=150)

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "value" and value > 100:
            raise ValueError("RangeField value must be between 0 and 100")

    def _changed(self, field, old, value):
        if field.name == "caption":
            self.heading.text = value
        elif field.name == "value":
            self._syncing = True
            try:
                self.update_children({self.track: {"value": value},
                                      self.number: {"value": value}})
            finally:
                self._syncing = False
        super()._changed(field, old, value)

    def _accept_value(self, value, origin):
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
    from pysual import autoconfig
    from argparse import ArgumentParser

    ArgumentParser(description=__doc__).parse_args()
    Demo().run_blocking()
