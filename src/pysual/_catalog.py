"""Built-in catalog metadata and installation, independent of implementation files.

New entries default to constructor/create-only registration. The historical
factory aliases are explicit compatibility choices, not an ever-growing set of
reserved Container members. Public Python exports remain explicit for typing.
"""

from dataclasses import dataclass

from .controls import Button, Container, Control, Label
from .widgets import CheckBox, Image, ListView, ScrollArea, Slider, SubWindow, TextBox
from .selection import ComboBox, Dropdown, ProgressBar, RadioButton, Rating, Toggle
from .composition import SplitPane, TabControl, TabPage
from .charts import DonutChart, LineChart
from .data_grid import DataGrid
from .editors import ColorPicker, NumericInput
from .fields import SearchField
from .dates import DatePicker
from ._controls.indicators import RangeSlider, CircularProgress
from .navigation import SegmentedControl, Breadcrumb, Pagination
from .cards import ChoiceCard, Disclosure
from .feedback import Badge, AlertBanner, Meter
from .extras import GroupBox, Hyperlink, Separator
from .menus import Menu, MenuBar
from .popup import Popup
from .tree import TreeView
from ._registry import register_control


@dataclass(frozen=True)
class BuiltinControl:
    name: str
    control_type: type[Control]
    family: str
    legacy_factory: bool = False
    authorable: bool = True


BUILTIN_CONTROLS = (
    BuiltinControl("label", Label, "basic", legacy_factory=True),
    BuiltinControl("button", Button, "basic", legacy_factory=True),
    BuiltinControl("panel", Container, "layout", legacy_factory=True),
    BuiltinControl("check_box", CheckBox, "actions", legacy_factory=True),
    BuiltinControl("slider", Slider, "ranges", legacy_factory=True),
    BuiltinControl("text_box", TextBox, "text", legacy_factory=True),
    BuiltinControl("scroll_area", ScrollArea, "viewports", legacy_factory=True),
    BuiltinControl("list_view", ListView, "collections", legacy_factory=True),
    BuiltinControl("image", Image, "media", legacy_factory=True),
    BuiltinControl("sub_window", SubWindow, "windows", legacy_factory=True),
    BuiltinControl("tree_view", TreeView, "collections", legacy_factory=True),
    BuiltinControl("popup", Popup, "overlays", legacy_factory=True),
    BuiltinControl("line_chart", LineChart, "charts", legacy_factory=True),
    BuiltinControl("donut_chart", DonutChart, "charts", legacy_factory=True),
    BuiltinControl("data_grid", DataGrid, "collections", legacy_factory=True),
    BuiltinControl("menu", Menu, "commands", legacy_factory=True),
    BuiltinControl("menu_bar", MenuBar, "commands", legacy_factory=True),
    BuiltinControl("tab_page", TabPage, "layout", legacy_factory=True),
    BuiltinControl("tab_control", TabControl, "layout", legacy_factory=True),
    BuiltinControl("split_pane", SplitPane, "layout", legacy_factory=True),
    BuiltinControl("toggle", Toggle, "actions", legacy_factory=True),
    BuiltinControl("radio_button", RadioButton, "actions", legacy_factory=True),
    BuiltinControl("progress_bar", ProgressBar, "ranges", legacy_factory=True),
    BuiltinControl("dropdown", Dropdown, "choices", legacy_factory=True),
    BuiltinControl("combo_box", ComboBox, "choices", legacy_factory=True),
    BuiltinControl("numeric_input", NumericInput, "editors", legacy_factory=True),
    BuiltinControl("color_picker", ColorPicker, "editors", legacy_factory=True),
    BuiltinControl("group_box", GroupBox, "layout", legacy_factory=True),
    BuiltinControl("separator", Separator, "layout", legacy_factory=True),
    BuiltinControl("hyperlink", Hyperlink, "actions", legacy_factory=True),
    BuiltinControl("rating", Rating, "ranges"),
    BuiltinControl("search_field", SearchField, "text"),
    BuiltinControl("date_picker", DatePicker, "dates"),
    BuiltinControl("range_slider", RangeSlider, "ranges"),
    BuiltinControl("circular_progress", CircularProgress, "ranges"),
    BuiltinControl("segmented_control", SegmentedControl, "actions"),
    BuiltinControl("breadcrumb", Breadcrumb, "navigation"),
    BuiltinControl("pagination", Pagination, "navigation"),
    BuiltinControl("choice_card", ChoiceCard, "actions"),
    BuiltinControl("disclosure", Disclosure, "layout"),
    BuiltinControl("badge", Badge, "feedback"),
    BuiltinControl("alert_banner", AlertBanner, "feedback"),
    BuiltinControl("meter", Meter, "feedback"),
)


def install_builtin_controls() -> None:
    for entry in BUILTIN_CONTROLS:
        register_control(
            entry.control_type, name=entry.name, authorable=entry.authorable,
            factory=entry.legacy_factory,
        )
