"""Consumer API checks, analyzed by tools/check_typing.py; never executed."""

from typing import Literal, assert_type

from pysual import (
    Button, ColorEditor, Container, Control, DonutChart, EditorContent, EditorPopup,
    Image, Label, NumericEditor, NumericInput, Painter, PressBehavior, Rect,
    Separator, SplitPane, blueprint, prop,
)
from pysual.host import Host, ImageFitHost, Input, LineBatchHost, image_fit_host, optional_host
from pysual.schema import Field, expect_number
from pysual.theme import ResolvedStyle
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
    optional_status: Literal["idle", "busy"] | None = prop(default=None)
    statuses: tuple[Literal["idle", "busy"], ...] = prop(default=())


badge = Badge(parent=panel, status="busy", count=3)
assert_type(badge.status, Literal["idle", "busy"])
assert_type(badge.count, int)
assert_type(panel.create(Badge, status="busy", count=3), Badge)
assert_type(panel.create(Button, text="Save"), Button)
assert_type(Badge(optional_status="busy").optional_status, Literal["idle", "busy"] | None)
assert_type(Badge(statuses=("idle", "busy")).statuses, tuple[Literal["idle", "busy"], ...])


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
assert_type(EditorPopup(value_owner, NumericEditor(5)), EditorPopup[float])


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
calendar_popup = EditorPopup(panel, CalendarEditor("2026-10-13"))
assert_type(calendar_popup.content.read(), str)
number_popup = EditorPopup(panel, NumericEditor(1.5))
assert_type(number_popup.content.read(), float)


class Gauge(Control):
    level: float = prop(default=0.5, minimum=0)

    def measure(self, host: Host) -> tuple[float, float]:
        width, height = host.measure("Level", 16)
        assert_type((width, height), tuple[float, float])
        return width + 24, max(32.0, height + 16)

    def paint(self, painter: Painter, /) -> None:
        area = self.bounds
        assert_type(area, Rect)
        assert_type(area.inset(4), Rect)
        assert_type(area.contains(1, 2), bool)
        assert_type(self.effective_enabled, bool)
        face = painter.style()
        assert_type(face, ResolvedStyle)
        assert_type(painter.body(), ResolvedStyle)
        assert_type(painter.measure("Level", size=16), tuple[float, float])
        painter.rect(Rect(0, 0, area.width, area.height), "#127c6a", radius=8)
        painter.text(
            "Level", 12, 8, color=face.foreground, size=face.font_size,
            font_family=face.font_family,
        )
        painter.line(0, area.height - 1, area.width, area.height - 1, "#127c6a")
        painter.image("mark.png", Rect(0, 0, 8, 8), fit="contain")

    def handle_input(self, event: Input, /) -> bool | None:
        assert_type(event.kind, str)
        assert_type(event.x, float)
        return event.kind == "pointer_up"

    def _validate_update(self, name: str, value: object) -> None:
        super()._validate_update(name, value)
        if name == "level" and expect_number(value) > 1:
            raise ValueError("level must be at most 1")

    def _changed(self, field: Field, old: object, value: object) -> None:
        assert_type(field.name, str)
        super()._changed(field, old, value)


class PaintedButton(Button):
    def paint(self, painter, /) -> None:
        assert_type(painter, Painter)

    def measure(self, host) -> tuple[float, float]:
        assert_type(host.measure("ok", 12), tuple[float, float])
        return super().measure(host)

    def handle_input(self, event, /) -> bool | None:
        assert_type(event.button, int)
        return super().handle_input(event)


def _optional_hosts(host: Host) -> None:
    assert_type(optional_host(host, "lines"), LineBatchHost | None)
    assert_type(image_fit_host(host), ImageFitHost | None)
