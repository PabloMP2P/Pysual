"""Navigation consumers, analyzed without execution."""

from typing import assert_type

from pysual import (
    Breadcrumb, BreadcrumbEvent, Container, Pagination, SegmentedControl, Step,
    Stepper, ValidationEvent, blueprint,
)
from pysual.events import ChangeEvent

panel = Container()
mode = panel.create(SegmentedControl, items=("List", "Grid"), selected_index=0)
path = panel.create(Breadcrumb, items=("Home", "Project", "Controls"))
pages = panel.create(Pagination, page=2, page_count=100)
assert_type(mode, SegmentedControl)
assert_type(mode.selected_item, str | None)
assert_type(path, Breadcrumb)
assert_type(pages.page, int)
wizard = panel.create(Stepper)
account = wizard.create(Step, title="Account")
review = wizard.create(Step, title="Review", requires=[account])
review.requires = [account]
assert_type(account.set_validation("Enter a name", show_error=False), bool)
assert_type(account.set_validation(""), bool)
assert_type(wizard.selected_step, Step | None)


def validate_account(event: ValidationEvent) -> bool:
    assert_type(event.source, object)
    return True


async def validate_review(event: ValidationEvent) -> None:
    if not account.completed:
        event.reject("Complete Account first")


async def validate_form() -> None:
    assert_type(await account.validate(show_error=False), bool)
    assert_type(wizard.can_select(1), bool)
    wizard.reset()


account.validating.connect(validate_account)
review.validating.connect(validate_review)


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
account.set_validation(False)  # error: reportArgumentType
account.set_validation("Missing", show_error="no")  # error: reportArgumentType
Step(requires=["account"])  # error: reportArgumentType
review.requires = ["account"]  # error: reportAttributeAccessIssue
