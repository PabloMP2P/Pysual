"""Every marked line must produce exactly the specified Pyright error."""

from typing import Literal
from pysual import Button, Container, DonutChart, Image, Label, Separator, SplitPane, prop
from pysual import EditorContent
from pysual import CalendarEditor, DatePicker, Rating, SearchField

panel = Container()
Container(layout="wrong")  # error: reportArgumentType
Container(direction="wrong")  # error: reportArgumentType
Button(dock="wrong")  # error: reportArgumentType
Button(font_family="wrong")  # error: reportArgumentType
Image(fit="wrong")  # error: reportArgumentType
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


Badge(status="wrong")  # error: reportArgumentType
Badge(count="three")  # error: reportArgumentType
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
