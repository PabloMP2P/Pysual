"""Pysual: typed Python controls with shared desktop and web behavior."""

from .app import App as App, Window as Window
from ._config import configure as configure
from ._engine import shutdown as shutdown
from .runtime import wait as wait, wait_async as wait_async
from .backends.terminal import terminal as terminal

__version__ = "0.1.1"
from .controls import (
    Button as Button,
    Container as Container,
    Control as Control,
    Label as Label,
)
from .controls import register_control as register_control
from .controls import registered_controls as registered_controls
from .events import (
    ClickEvent as ClickEvent,
    ClosingEvent as ClosingEvent,
    Event as Event,
    UiEvent as UiEvent,
)
from .schema import Dirty as Dirty, prop as prop
from .behaviors import PressBehavior as PressBehavior

from .widgets import (
    CheckBox as CheckBox,
    Slider as Slider,
    TextBox as TextBox,
    ScrollArea as ScrollArea,
    ListView as ListView,
    Image as Image,
    SubWindow as SubWindow,
)
from .events import (
    ChangeEvent as ChangeEvent,
    PointerEvent as PointerEvent,
    KeyEvent as KeyEvent,
)
from .theme import (
    Theme as Theme,
    Tokens as Tokens,
    Style as Style,
    Rule as Rule,
    dark as dark,
    get_theme as get_theme,
    theme_names as theme_names,
    light as light,
    load_theme as load_theme,
    save_theme as save_theme,
)
from .geometry import Rect as Rect
from .painting import Painter as Painter

from .host import CapabilityError as CapabilityError
from .host import TextFile as TextFile
from .host import RenderDiagnostics as RenderDiagnostics
from .runtime import FrameTiming as FrameTiming
from .tree import TreeNode as TreeNode, TreeView as TreeView, TreeEvent as TreeEvent
from .popup import Popup as Popup
from .charts import LineChart as LineChart, ChartSeries as ChartSeries
from .data_grid import (
    DataGrid as DataGrid,
    GridColumn as GridColumn,
    GridRow as GridRow,
    CellValue as CellValue,
    GridCellEvent as GridCellEvent,
    GridEditEvent as GridEditEvent,
)
from .commands import (
    MenuItem as MenuItem,
    MenuGroup as MenuGroup,
    CommandEvent as CommandEvent,
)
from .menus import Menu as Menu, MenuBar as MenuBar
from .composition import (
    TabControl as TabControl,
    TabPage as TabPage,
    SplitPane as SplitPane,
)
from .selection import (
    Dropdown as Dropdown,
    ComboBox as ComboBox,
    Toggle as Toggle,
    RadioButton as RadioButton,
    ProgressBar as ProgressBar,
    Rating as Rating,
)

from .icons import icon_names as icon_names

from .render_cache import CacheStats as CacheStats

from .editors import NumericInput as NumericInput, ColorPicker as ColorPicker

from .extras import GroupBox as GroupBox, Separator as Separator, Hyperlink as Hyperlink
from .charts import DonutChart as DonutChart, ChartSlice as ChartSlice
from .events import DragPayload as DragPayload, DragEvent as DragEvent
from .host import Viewport as Viewport
from .inspection import (
    ControlSnapshot as ControlSnapshot,
    HandlerSnapshot as HandlerSnapshot,
    TaskSnapshot as TaskSnapshot,
)

from .controls import blueprint as blueprint
from ._blueprint import Blueprint as Blueprint

from .image_resources import image_source as image_source, package_image as package_image
from .editing import EditorContent as EditorContent, EditorPopup as EditorPopup
from .editors import NumericEditor as NumericEditor, ColorEditor as ColorEditor
from .fields import SearchField as SearchField
from .dates import DatePicker as DatePicker, CalendarEditor as CalendarEditor
from ._controls.indicators import (
    RangeSlider as RangeSlider, CircularProgress as CircularProgress,
)
from .navigation import (
    SegmentedControl as SegmentedControl, Breadcrumb as Breadcrumb,
    BreadcrumbEvent as BreadcrumbEvent, Pagination as Pagination,
)
from .cards import ChoiceCard as ChoiceCard, Disclosure as Disclosure
from .feedback import Badge as Badge, AlertBanner as AlertBanner, Meter as Meter

from ._catalog import install_builtin_controls as _install_builtin_controls

_install_builtin_controls()

# Keep loaded implementation modules out of wildcard imports. Argument
# processing remains an explicit opt-in through pysual.autoconfig.
__all__ = (
    "App", "Window", "configure", "shutdown", "wait", "wait_async", "terminal",
    "Control", "Container", "Button", "Label", "register_control",
    "registered_controls", "blueprint", "Blueprint", "Dirty", "prop", "PressBehavior",
    "CheckBox", "Slider", "TextBox", "ScrollArea", "ListView", "Image",
    "SubWindow", "Popup", "TreeNode", "TreeView", "TreeEvent",
    "MenuItem", "MenuGroup", "Menu", "MenuBar", "TabControl", "TabPage",
    "SplitPane", "Dropdown", "ComboBox", "Toggle", "RadioButton", "ProgressBar", "Rating",
    "NumericInput", "ColorPicker", "GroupBox", "Separator", "Hyperlink",
    "EditorContent", "EditorPopup", "NumericEditor", "ColorEditor",
    "SearchField", "DatePicker", "CalendarEditor",
    "RangeSlider", "CircularProgress", "SegmentedControl", "Breadcrumb",
    "BreadcrumbEvent", "Pagination",
    "ChoiceCard", "Disclosure", "Badge", "AlertBanner", "Meter",
    "LineChart", "ChartSeries", "DonutChart", "ChartSlice", "DataGrid",
    "GridColumn", "GridRow", "CellValue", "GridCellEvent", "GridEditEvent",
    "Event", "UiEvent", "ClickEvent", "ClosingEvent", "ChangeEvent",
    "PointerEvent", "KeyEvent", "CommandEvent", "DragPayload", "DragEvent",
    "Theme", "Tokens", "Style", "Rule", "dark", "light", "get_theme",
    "theme_names", "load_theme", "save_theme", "icon_names",
    "Rect", "Painter", "CapabilityError", "TextFile", "Viewport",
    "FrameTiming", "RenderDiagnostics", "CacheStats", "ControlSnapshot", "HandlerSnapshot",
    "TaskSnapshot", "image_source", "package_image",
)
