"""A responsive review workspace using thirteen catalog ideas and all 18 themes.

Run: python examples/catalog_controls.py --backend web
"""

from math import ceil

from pysual import (
    AlertBanner, App, Badge, Breadcrumb, Button, CalendarEditor, ChoiceCard,
    CircularProgress, Container, DatePicker, Disclosure, Dropdown, Label, Meter,
    Pagination, RangeSlider, Rating, ScrollArea, SearchField, SegmentedControl,
    get_theme, theme_names,
)


# Title, estimated hours, complete. Applications own the data and filtering.
REVIEWS = (
    ("API design", 8, False), ("Keyboard navigation", 4, True),
    ("Release notes", 2, False), ("Theme contrast", 6, True),
    ("Input validation", 5, True), ("Packaging", 10, False),
    ("Calendar bounds", 3, False), ("Render performance", 12, True),
)
PAGE_SIZE = 3
THEME_FAMILIES = {
    "modern": "Modern", "windows": "Windows", "macos": "macOS",
    "terminal": "Terminal", "win31": "Win31", "winxp": "WinXP",
    "macintosh": "Macintosh", "analog_arcade": "Analog arcade", "neon": "Neon",
}
THEME_LABELS = tuple(
    f"{THEME_FAMILIES[name.removesuffix('_dark')]} · {'Dark' if name.endswith('_dark') else 'Light'}"
    for name in theme_names()
)


class Surface(Container):
    """Use the active theme's panel material, including its border and effects."""

    def paint(self, painter, /):
        painter.body()


class Caption(Label):
    """Secondary text follows live theme changes without storing palette colors."""

    def paint(self, painter, /):
        style = painter.style()
        text = painter.elide(self.text, painter.width, size=style.font_size)
        _, height = painter.measure(text, size=style.font_size)
        painter.text(text, 0, max(0, (painter.height - height) / 2),
                     size=style.font_size, color=painter.theme.tokens.muted)


class ReviewRow(Container):
    def __init__(self, **kwargs):
        super().__init__(layout="grid", column_tracks=("1*", 114),
                         padding=12, spacing=10, height=70, **kwargs)
        self.copy = Container(layout="stack", spacing=2)
        self.subject = Label(parent=self.copy, height=22, font_size=16)
        self.estimate = Caption(parent=self.copy, height=20, font_size=12)
        self.state = Badge(width=114, height=26)

    def paint(self, painter, /):
        # A fine divider keeps the collection calm inside its enclosing panel.
        painter.line(0, painter.height - 1, painter.width, painter.height - 1,
                     painter.theme.tokens.border, width=1)

    def show_review(self, review):
        title, hours, complete = review
        self.subject.text = title
        self.estimate.text = f"{hours} hours estimated"
        self.state.update(text="Complete" if complete else "Pending",
                          tone="success" if complete else "neutral")
        self.visible = True


class Demo(App):
    def build(self):
        self.title = "Pysual · Review workspace"
        self.width, self.height = 1180, 940
        self.layout, self.padding, self.spacing = "stack", 24, 12
        self.header = Container(layout="grid", column_tracks=("1*", 190), height=36, spacing=16)
        Label(parent=self.header, text="P Y S U A L   /   C O N T R O L   L A B", font_size=12)
        self.theme_choice = Dropdown(
            parent=self.header, items=THEME_LABELS, width=190,
            selected_index=next((i for i, name in enumerate(theme_names())
                                 if get_theme(name) == self.theme), -1),
            placeholder="Custom theme", tooltip="Preview a built-in theme",
        )
        self.path = Breadcrumb(items=("Workspace", "Reviews"), height=28)
        self.hero = Container(layout="grid", column_tracks=("1*", 112), height=44, spacing=8)
        self.heading = Label(parent=self.hero, text="Review workspace", font_size=30)
        Badge(parent=self.hero, text="13 controls", tone="info", show_dot=False, height=28)
        Caption(text="A little clarity for your next release.", parent=self, height=24)
        self.notice = AlertBanner(
            text="Four reviews are ready.",
            tone="info", action_text="View ready", height=56,
        )
        self.body = ScrollArea(layout="stack", flex=1, padding=4)
        self.workspace = Container(parent=self.body, layout="grid",
                                   column_tracks=("1.3*", "1*"), row_tracks=("auto",), spacing=20)
        self.details_panel = Surface(parent=self.workspace, layout="stack", padding=20, spacing=12)
        self.plan_panel = Surface(parent=self.workspace, layout="stack", padding=20, spacing=12)

        self.queue_heading = Container(parent=self.details_panel, layout="grid",
                                       column_tracks=("1*", 100), height=28)
        Label(parent=self.queue_heading, text="Your review queue", font_size=20)
        self.queue_count = Badge(parent=self.queue_heading, text="8 items", height=26)
        self.query = SearchField(parent=self.details_panel, placeholder="Search reviews…", height=40)
        self.review_view = SegmentedControl(parent=self.details_panel,
                                            items=("All reviews", "Pending", "Complete"),
                                            selected_index=0, height=36)
        self.filters = Disclosure(parent=self.details_panel, title="Effort filter",
                                  summary="Any estimate", expanded=False)
        self.effort = RangeSlider(parent=self.filters.content, minimum=0, maximum=12,
                                 value=(0, 12), lower_label="Min hours", upper_label="Max hours",
                                 height=60, tooltip="Drag an end; Space switches the keyboard thumb")
        self.result_count = Caption(parent=self.details_panel, height=22, font_size=12)
        self.result_list = Container(parent=self.details_panel, layout="stack", spacing=0, height=210)
        self._rows = [ReviewRow(parent=self.result_list) for _ in range(PAGE_SIZE)]
        self.empty = Label(parent=self.result_list, text="No reviews match these filters.",
                           height=60, visible=False)
        self.pages = Pagination(parent=self.details_panel, height=36)
        self.progress_row = Container(parent=self.details_panel, layout="grid",
                                      column_tracks=(64, "1*"), height=68, spacing=14)
        self.completion = CircularProgress(parent=self.progress_row, width=64, thickness=5)
        self.completion_text = Label(parent=self.progress_row, font_size=13)

        Label(parent=self.plan_panel, text="Make a review plan", font_size=20, height=28)
        Caption(parent=self.plan_panel, text="01  /  CHOOSE YOUR APPROACH", font_size=11, height=20)
        self.approaches = Container(parent=self.plan_panel, layout="grid",
                                    column_tracks=("1*", "1*"), height=148, spacing=10)
        self.focused_review = ChoiceCard(parent=self.approaches, text="Focused",
                                        description="Give one area your full attention.", icon="search",
                                        orientation="vertical",
                                        group="approach", checked=True)
        self.full_review = ChoiceCard(parent=self.approaches, text="Full pass",
                                     description="Bring every detail into view.", icon="check",
                                     orientation="vertical",
                                     group="approach")
        Caption(parent=self.plan_panel, text="02  /  SET YOUR CONFIDENCE", font_size=11, height=20)
        self.confidence_row = Container(parent=self.plan_panel, layout="grid",
                                        column_tracks=("1*", 64), height=36, spacing=8)
        self.score = Rating(parent=self.confidence_row, value=3, font_size=24)
        self.score_text = Caption(parent=self.confidence_row, font_size=12)
        self.readiness = Meter(parent=self.plan_panel, value=60, label="Review readiness",
                               unit="%", segments=20, height=52)
        Caption(parent=self.plan_panel, text="03  /  PICK A DATE", font_size=11, height=20)
        self.due = DatePicker(parent=self.plan_panel, value="2026-10-15",
                              minimum="2026-01-01", maximum="2027-12-31", height=40)
        self.calendar_details = Disclosure(parent=self.plan_panel, title="Browse the calendar",
                                           summary="Preview another date", expanded=False)
        self.calendar = CalendarEditor(
            "2026-10-20", parent=self.calendar_details.content, minimum=self.due.minimum,
            maximum=self.due.maximum, height=388,
        )
        self.use_date = Button(parent=self.calendar_details.content, text="Use this date", height=38)
        self.save_plan = Button(parent=self.plan_panel, text="Save review plan", icon="check", height=42)
        self.status = Caption(height=22, font_size=12)
        self._refresh_results()
        self._refresh_summary()
        self._adapt(self.width)

    def _adapt(self, width):
        narrow = width < 860
        self.padding = 12 if width < 600 else 24
        self.workspace.update(column_tracks=("1*",) if narrow else ("1.3*", "1*"),
                              row_tracks=("auto", "auto") if narrow else ("auto",))
        self.hero.column_tracks = ("1*", 0) if width < 600 else ("1*", 112)
        self.hero.children[1].visible = width >= 600
        self.heading.font_size = 26 if width < 400 else 30
        stack_choices = width < 580 or 860 <= width < 1120
        orientation = "horizontal" if stack_choices else "vertical"
        self.focused_review.orientation = self.full_review.orientation = orientation
        self.approaches.update(column_tracks=("1*",) if stack_choices else ("1*", "1*"),
                               row_tracks=("1*", "1*") if stack_choices else ("1*",),
                               height=174 if stack_choices else 148)

    def Demo_on_viewport_changed(self, event):
        self._adapt(event.new_value.width)

    def _refresh_results(self, *, reset_page=False):
        query = self.query.text.casefold().strip()
        lower, upper = self.effort.value
        view = self.review_view.selected_index
        matches = [review for review in REVIEWS
                   if query in review[0].casefold() and lower <= review[1] <= upper
                   and (view == 0 or review[2] == (view == 2))]
        page_count = max(1, ceil(len(matches) / PAGE_SIZE))
        page = 1 if reset_page else min(self.pages.page, page_count)
        self.pages.update(page_count=page_count, page=page)
        self.pages.enabled = bool(matches)
        start = (page - 1) * PAGE_SIZE
        visible = matches[start:start + PAGE_SIZE]
        for index, row in enumerate(self._rows):
            if index < len(visible):
                row.show_review(visible[index])
            else:
                row.visible = False
        self.empty.visible = not matches
        self.queue_count.text = f"{len(matches)} items"
        self.result_count.text = (f"SHOWING {start + 1}–{start + len(visible)} OF {len(matches)} REVIEWS"
                                  if matches else "NO MATCHING REVIEWS")
        self.filters.summary = "Any estimate" if (lower, upper) == (0, 12) else f"{lower:g}–{upper:g} hours"
        done = sum(item[2] for item in matches)
        self.completion.value = 100 * done / len(matches) if matches else 0
        self.completion_text.text = f"{done} of {len(matches)} complete\nIn your current view"

    def _refresh_summary(self):
        self.score_text.text = f"{self.score.value} / {self.score.maximum}"
        self.readiness.value = self.score.value * 100 / self.score.maximum
        approach = "Focused" if self.focused_review.checked else "Full pass"
        self.status.text = f"{approach} review · {self.due.value} · {self.score.value}/5 confidence"

    def query_on_changed(self, event):
        self._refresh_results(reset_page=True)

    def query_on_submitted(self, event):
        self._refresh_results(reset_page=True)

    def review_view_on_changed(self, event):
        self._refresh_results(reset_page=True)

    def effort_on_changed(self, event):
        self._refresh_results(reset_page=True)

    def pages_on_changed(self, event):
        self._refresh_results()

    def path_on_navigated(self, event):
        self.query.text = ""
        self.review_view.selected_index = 0
        self.effort.value = (0, 12)
        self._refresh_results(reset_page=True)

    def theme_choice_on_changed(self, event):
        if event.new_value >= 0:
            self.theme = get_theme(theme_names()[event.new_value])

    def score_on_changed(self, event):
        self._refresh_summary()

    def due_on_changed(self, event):
        self._refresh_summary()

    def focused_review_on_changed(self, event):
        self._refresh_summary()

    def full_review_on_changed(self, event):
        self._refresh_summary()

    def use_date_on_click(self, event):
        try:
            self.due.value = self.calendar.read()
        except ValueError as error:
            self.status.text = str(error)
            self.calendar.entry.focus()
            return
        self.calendar_details.expanded = False

    def notice_on_action(self, event):
        self.query.text = ""
        self.effort.value = (0, 12)
        self.review_view.selected_index = 2
        self._refresh_results(reset_page=True)

    def save_plan_on_click(self, event):
        approach = "Focused" if self.focused_review.checked else "Full pass"
        self.notice.update(text=f"{approach} plan saved for {self.due.value}.",
                           tone="success", action_text="", visible=True)
        self._refresh_summary()


if __name__ == "__main__":
    from pysual import autoconfig
    from argparse import ArgumentParser

    ArgumentParser(description=__doc__).parse_args()
    Demo().run_blocking()
