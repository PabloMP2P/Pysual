"""Public interval and ring consumers, analyzed and never executed."""

from typing import Literal, assert_type

from pysual import CircularProgress, Container, RangeSlider, blueprint
from pysual.events import ChangeEvent

panel = Container()
interval = panel.create(RangeSlider, value=(20, 80), active_thumb="upper")
ring = panel.create(CircularProgress, value=68, thickness=5, show_text=True)
assert_type(interval, RangeSlider)
assert_type(interval.value, tuple[float, float])
assert_type(interval.active_thumb, Literal["lower", "upper"])
assert_type(ring, CircularProgress)
assert_type(ring.value, float)
interval.set_range(10, 90, value=(20, 70))
ring.set_range(200, value=150)


def interval_changed(event: ChangeEvent[tuple[float, float]]) -> None:
    assert_type(event.new_value, tuple[float, float])


interval.changed.connect(interval_changed)


class Dashboard(Container):
    interval = blueprint(RangeSlider, value=(10, 90))
    progress = blueprint(CircularProgress, value=40)


assert_type(Dashboard().interval, RangeSlider)
RangeSlider(value=(20,))  # error: reportArgumentType
RangeSlider(value=20)  # error: reportArgumentType
RangeSlider(active_thumb="middle")  # error: reportArgumentType
panel.create(RangeSlider, step="one")  # error: reportArgumentType
CircularProgress(thickness="wide")  # error: reportArgumentType
ring.set_range(100, value="done")  # error: reportArgumentType
