"""Every marked line must produce exactly the specified Pyright error."""

from typing import Literal
from pysual import (
    Button, Container, Control, DonutChart, EditorPopup, Image, Label, Painter, Rect,
    Separator, SplitPane, prop,
)
from pysual import EditorContent
from pysual import CalendarEditor, DatePicker, Rating, SearchField
from pysual.host import Host, optional_host

panel = Container()
Container(layout="wrong")  # error: reportArgumentType
Container(direction="wrong")  # error: reportArgumentType
Button(dock="wrong")  # error: reportArgumentType
Button(font_family="wrong")  # error: reportArgumentType
Image(fit="wrong")  # error: reportArgumentType
Image().reload("another.png")  # error: reportCallIssue
SplitPane(orientation="wrong")  # error: reportArgumentType
Separator(orientation="wrong")  # error: reportArgumentType
DonutChart(center_mode="wrong")  # error: reportArgumentType
panel.panel(layout="wrong")  # error: reportArgumentType
panel.panel(direction="wrong")  # error: reportArgumentType
panel.button(dock="wrong")  # error: reportArgumentType
panel.button(font_family="wrong")  # error: reportArgumentType
panel.image(fit="wrong")  # error: reportArgumentType
panel.split_pane(orientation="wrong")  # error: reportArgumentType
panel.separator(orientation="wrong")  # error: reportArgumentType
panel.donut_chart(center_mode="wrong")  # error: reportArgumentType
Button(width="wide")  # error: reportArgumentType
panel.button(width="wide")  # error: reportArgumentType


class Badge(Label):
    status: Literal["idle", "busy"] = prop(default="idle")
    count: int = prop(default=0)
    optional_status: Literal["idle", "busy"] | None = prop(default=None)
    statuses: tuple[Literal["idle", "busy"], ...] = prop(default=())


Badge(status="wrong")  # error: reportArgumentType
Badge(count="three")  # error: reportArgumentType
Badge(optional_status="wrong")  # error: reportArgumentType
Badge(statuses=("idle", "wrong"))  # error: reportArgumentType
panel.create(Badge, status="wrong")  # error: reportArgumentType
panel.create(Badge, count="three")  # error: reportArgumentType
panel.create(Button, dock="wrong")  # error: reportArgumentType
panel.create(Button, width="wide")  # error: reportArgumentType


class DraftEditor(EditorContent):
    def build(self, value: object) -> None:
        self._draft = value

    def read(self) -> object:
        return self._draft


DraftEditor()  # error: reportCallIssue
DraftEditor("Initial", "Extra")  # error: reportCallIssue
DraftEditor("Initial", width="wide")  # error: reportArgumentType
panel.create(DraftEditor)  # error: reportCallIssue
Rating(value=2.5)  # error: reportArgumentType
panel.create(Rating, maximum="five")  # error: reportArgumentType
SearchField(text=42)  # error: reportArgumentType
DatePicker(value=20261015)  # error: reportArgumentType
panel.create(DatePicker, minimum=None)  # error: reportArgumentType
CalendarEditor(42)  # error: reportArgumentType
EditorPopup(panel, Label())  # error: reportArgumentType


class BrokenGauge(Control):
    def paint(self, painter: Painter, /) -> None:
        painter.rect(Rect(0, 0, 10, 10), 1)  # error: reportArgumentType
        painter.text("A", 0, 0, font_family="serif")  # error: reportArgumentType
        painter.line(0, 0, 10, 10, 5)  # error: reportArgumentType
        painter.image("mark.png", Rect(0, 0, 8, 8), fit="tile")  # error: reportArgumentType

    def measure(self, host: Host) -> tuple[float, float]:
        host.measure(12, 16)  # error: reportArgumentType
        return (0.0, 0.0)

    def paint_focus(self, painter, /) -> None:
        self.bounds.inset("pad")  # error: reportArgumentType


def _unknown_host_method(host: Host) -> None:
    optional_host(host, "redraw")  # error: reportCallIssue, reportArgumentType
