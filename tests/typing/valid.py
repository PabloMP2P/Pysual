"""Consumer API checks, analyzed by tools/check_typing.py; never executed."""

from typing import Literal, assert_type

from pysual import (
    Button, ColorEditor, Container, DonutChart, EditorContent, EditorPopup, Image,
    Label, NumericEditor, NumericInput, PressBehavior, Separator, SplitPane,
    blueprint, prop,
)
from pysual.host import Input
from pysual import Blueprint, CalendarEditor, DatePicker, Rating, SearchField

panel = Container(layout="stack", direction="horizontal", font_family=None)
button = Button(parent=panel, dock="left", font_family="ui")
image = Image(parent=panel, fit="contain")
split = SplitPane(parent=panel, orientation="vertical")
separator = Separator(parent=panel, orientation="horizontal")
chart = DonutChart(parent=panel, center_mode="total")
panel.panel(layout="grid", direction="vertical", font_family="mono")
panel.button(dock="bottom", font_family=None)
panel.image(fit="cover")
panel.split_pane(orientation="horizontal")
panel.separator(orientation="vertical")
panel.donut_chart(center_mode="none")
assert_type(button, Button)
assert_type(panel.button(text="Save"), Button)
assert_type(image.fit, Literal["stretch", "contain", "cover"])
assert_type(image.reload(), None)
button.dock = "right"
panel.layout = "flow"
panel.font_family = "mono"


class Badge(Label):
    status: Literal["idle", "busy"] = prop(default="idle")
    count: int = prop(default=0)


badge = Badge(parent=panel, status="busy", count=3)
assert_type(badge.status, Literal["idle", "busy"])
assert_type(badge.count, int)
assert_type(panel.create(Badge, status="busy", count=3), Badge)
assert_type(panel.create(Button, text="Save"), Button)


class Captioned(Label):
    def __init__(self, caption: str, *, emphasized: bool = False):
        super().__init__(text=caption)


assert_type(panel.create(Captioned, "Ready", emphasized=True), Captioned)


class Form(Container):
    accept = blueprint(Button, text="Accept")


assert_type(Form().accept, Button)

press = PressBehavior(keys=())
assert_type(press.handle_input(button, Input("pointer_down"), part="body"), bool)
press.reset(button)

number_content = NumericEditor(5, decimals=1, parent=panel)
color_content = ColorEditor("#123456", parent=panel)
assert_type(number_content.read(), float)
assert_type(color_content.read(), str)
value_owner = NumericInput(value=5)
assert_type(EditorPopup(value_owner, NumericEditor(5)), EditorPopup)


class DraftEditor(EditorContent):
    def build(self, value: object) -> None:
        self._draft = str(value)

    def read(self) -> str:
        return self._draft


draft = DraftEditor("Initial", parent=panel, width=300)
assert_type(draft.read(), str)
assert_type(DraftEditor(value="Initial", height=40), DraftEditor)
assert_type(panel.create(DraftEditor, "Initial", width=300), DraftEditor)


class DetailedDraft(DraftEditor):
    detail: bool = prop(default=False)


assert_type(DetailedDraft("Initial", detail=True), DetailedDraft)

rating = panel.create(Rating, value=3, maximum=5, read_only=False)
search = panel.create(SearchField, text="review", placeholder="Search reviews")
date_picker = panel.create(DatePicker, value="2026-10-15", minimum="2026-01-01")
calendar = panel.create(CalendarEditor, "2026-10-15", maximum="2027-12-31")
assert_type(rating, Rating)
assert_type(rating.value, int)
assert_type(search.text, str)
assert_type(date_picker.value, str)
assert_type(calendar.read(), str)
assert_type(blueprint(SearchField, text="Find"), Blueprint[SearchField])
