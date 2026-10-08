"""Navigation consumers, analyzed without execution."""

from typing import assert_type

from pysual import Breadcrumb, BreadcrumbEvent, Container, Pagination, SegmentedControl, blueprint
from pysual.events import ChangeEvent

panel = Container()
mode = panel.create(SegmentedControl, items=("List", "Grid"), selected_index=0)
path = panel.create(Breadcrumb, items=("Home", "Project", "Controls"))
pages = panel.create(Pagination, page=2, page_count=100)
assert_type(mode, SegmentedControl)
assert_type(mode.selected_item, str | None)
assert_type(path, Breadcrumb)
assert_type(pages.page, int)


def mode_changed(event: ChangeEvent[int]) -> None:
    assert_type(event.new_value, int)


def navigate(event: BreadcrumbEvent) -> None:
    assert_type(event.index, int)
    assert_type(event.item, str)


mode.changed.connect(mode_changed)
path.navigated.connect(navigate)
path.activate(0)


class NavigationPanel(Container):
    pages = blueprint(Pagination, page_count=5)


assert_type(NavigationPanel().pages, Pagination)
SegmentedControl(items=["List", "Grid"])  # error: reportArgumentType
SegmentedControl(selected_index="first")  # error: reportArgumentType
panel.create(Breadcrumb, max_item_width="wide")  # error: reportArgumentType
path.activate("home")  # error: reportArgumentType
Pagination(page=1.5)  # error: reportArgumentType
Pagination(visible_pages="all")  # error: reportArgumentType
