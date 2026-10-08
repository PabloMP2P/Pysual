"""Explore Pysual controls, layouts, themes, events, and custom painting.

Run: python examples/showcase.py --backend window
"""

import argparse
import asyncio
import math
from inspect import signature
from time import monotonic

from pysual import (
    App,
    Button,
    ChangeEvent,
    ChartSeries,
    ChartSlice,
    CheckBox,
    ClickEvent,
    ColorPicker,
    ComboBox,
    CommandEvent,
    Container,
    Control,
    DataGrid,
    DonutChart,
    Dropdown,
    GridColumn,
    GridEditEvent,
    GridRow,
    GroupBox,
    Hyperlink,
    Image,
    Label,
    LineChart,
    ListView,
    Menu,
    MenuBar,
    MenuGroup,
    MenuItem,
    NumericInput,
    PointerEvent,
    Popup,
    ProgressBar,
    RadioButton,
    Rect,
    ScrollArea,
    Separator,
    Slider,
    SplitPane,
    SubWindow,
    TabControl,
    TabPage,
    TextBox,
    Toggle,
    TreeEvent,
    TreeNode,
    TreeView,
    UiEvent,
    get_theme,
    prop,
    register_control,
    registered_controls,
    theme_names,
)


class Orbit(Control):
    """A typed custom control using only schema and Painter hooks."""

    phase: float = prop(default=0.0)

    def paint(self, painter, /):
        for i in range(7):
            height = 6 + (painter.height - 8) * (1 + math.sin(self.phase + i * 0.7)) / 2
            painter.rect(
                Rect(
                    i * painter.width / 7,
                    (painter.height - height) / 2,
                    max(1, painter.width / 7 - 4),
                    height,
                ),
                painter.theme.tokens.accent,
                radius=3,
            )


class ThemePanel(Container):
    """A visible panel; ordinary layout containers are otherwise transparent."""

    cache_paint: bool = prop(default=True)

    def paint(self, painter, /):
        painter.body()


register_control(Orbit)


def _annotation_name(annotation):
    """Show `float` and `float | None` alike, never `<class 'float'>`."""
    return getattr(annotation, "__name__", None) or str(annotation)


# Embedded PNG keeps this single-file example portable to desktop and web.
SAMPLE_IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGAAAAAwCAYAAADuFn/PAAAAh0lEQVR4nO3RsQ2AMAADwSxDywyZiS1YmQZmSBNL+IrvLd84zvkq10gPaA8AgO4AAOgOAIDuAADobhngup9fBwAAAAAAAAAAAAAAAAAAAAAAAAAAAAB7AHYfkA4AAAAAAAAAAAAAAAAAAADInwKgKAAAAMRHNAcAQHcAAHQHAEB3AAB0ByDcB/oLUr/fAFALAAAAAElFTkSuQmCC"


class Showcase(App):
    def build(self):
        self.title = "Pysual · Feature showcase"
        # App's 960×640 defaults apply; explicit constructor sizes still work.
        self.layout = "stack"
        self.padding = 16
        self.spacing = 12
        display_names = {
            "modern": "Modern", "windows": "Windows", "macos": "macOS",
            "terminal": "Terminal", "win31": "Win31", "winxp": "WinXP",
            "macintosh": "Macintosh", "analog_arcade": "Analog arcade", "neon": "Neon",
        }
        self._themes = tuple(
            (f"{display_names[name.removesuffix('_dark')]} · "
             f"{'Dark' if name.endswith('_dark') else 'Light'}", get_theme(name))
            for name in theme_names()
        )
        self._catalog = {
            "Panel" if cls.__name__ == "Container" else cls.__name__: cls
            for cls in registered_controls(authorable_only=True).values()
        }
        self._pages = (
            "Overview",
            "Theme lab",
            *self._catalog,
            "Layouts",
            "Async tasks",
            "Custom control",
            "Host capabilities",
        )
        self._window_number = 0
        self._dialog_open = False
        self._fps = 0.0
        self._cache_budget = self.render_cache_bytes or 32 * 1024 * 1024
        self.header = Container(
            layout="stack", direction="horizontal", height=38, spacing=16
        )
        self.heading = Label(
            parent=self.header, text="Pysual / feature showcase", font_size=23, flex=1
        )
        self.pulse = Orbit(parent=self.header, width=70, height=32)
        self.meter = Label(
            parent=self.header,
            text="Scene submissions/s —",
            width=300,
            font_size=14,
            visible=(self.width or 960) >= 800,
            tooltip=(
                "Python scene submissions; native animations can present independently. "
                "Zero is normal for an unchanged scene. This meter refreshes once a second."
            ),
        )
        self.body = Container(
            layout="stack", direction="horizontal", flex=1, spacing=12
        )
        self.sidebar = ScrollArea(
            parent=self.body,
            layout="stack",
            background=self.theme.tokens.surface,
            width=178,
            padding=12,
            spacing=8,
            cache_paint=True,
        )
        Label(parent=self.sidebar, text="CONTROL CATALOG", height=22, font_size=12)
        self.catalog = ListView(
            parent=self.sidebar,
            items=self._pages,
            selected_index=0,
            row_height=29,
            flex=1,
            min_height=120,  # Stays usable in a short browser viewport.
        )
        Label(parent=self.sidebar, text="THEME", height=20, font_size=12)
        self.theme_choice = Dropdown(
            parent=self.sidebar,
            items=tuple(name for name, theme in self._themes),
            selected_index=next(
                (i for i, (_, theme) in enumerate(self._themes) if theme == self.theme), -1
            ),
            placeholder="Custom theme",
            height=38,
        )
        self.animate = CheckBox(
            parent=self.sidebar,
            text="Animate",
            checked=True,
            height=32,
            tooltip=(
                "Update animation about 60 times/s; pause to redraw only when the scene changes."
            ),
        )
        self.cache_enabled = CheckBox(
            parent=self.sidebar,
            text="Surface cache",
            height=30,
            font_size=13,
            checked=self.render_cache_bytes > 0,
            tooltip="Reuse stable control paint results. Disable to compare the same scene.",
        )
        self.cache_meter = Label(
            parent=self.sidebar, text="Cache warming…", height=18, font_size=11
        )
        self.cache_memory = Label(parent=self.sidebar, text="", height=18, font_size=11)
        self.workspace = ScrollArea(
            parent=self.body,
            layout="stack",
            background=self.theme.tokens.surface,
            flex=1,
            padding=16,
            spacing=12,
            cache_paint=True,
        )
        self.status = Label(
            text="Explore a control or change the theme. Wheel scrolls the page.",
            height=24,
            font_size=13,
        )
        self.show_page("Overview")

    def apply_theme(self, index):
        self.theme = self._themes[index][1]
        self.sidebar.background = self.theme.tokens.surface
        self.workspace.background = self.theme.tokens.surface

    def theme_choice_on_changed(self, event: ChangeEvent[int]):
        if event.new_value >= 0:
            self.apply_theme(event.new_value)
            self.status.text = f"Theme: {self._themes[event.new_value][0]}"

    def catalog_on_changed(self, event: ChangeEvent[int]):
        if event.new_value >= 0:
            self.show_page(self._pages[event.new_value])

    def cache_enabled_on_changed(self, event: ChangeEvent[bool]):
        if not self.cache_enabled.enabled:
            return
        if event.new_value:
            self.render_cache_bytes = self._cache_budget
        else:
            self._cache_budget = self.render_cache_bytes or self._cache_budget
            self.render_cache_bytes = 0
        self.update_cache_meter()
        self.status.text = (
            "Surface cache enabled; stable bodies warm on redraw."
            if event.new_value
            else "Direct painting; the scene and control values are unchanged."
        )

    def update_cache_meter(self):
        stats = self.cache_stats
        retained = "scene_patches" in self.capabilities
        supported = "render_surfaces" in self.capabilities and not retained
        self.cache_enabled.enabled = supported
        self.cache_enabled.checked = supported and self.render_cache_bytes > 0
        self.cache_enabled.tooltip = (
            "This host reuses retained scenes independently of the surface cache."
            if retained else "Reuse stable control paint results. Disable to compare the same scene."
            if supported else "This host does not provide surface caching."
        )
        self.cache_meter.text = (
            "Retained scenes"
            if retained
            else "Direct painting"
            if not self.render_cache_bytes
            else "Host: direct fallback"
            if not supported
            else f"{stats.hits:,} hits · {stats.entries} stored"
        )
        retained_bytes = stats.retained_bytes if self.render_cache_bytes else 0
        self.cache_memory.text = (
            f"{retained_bytes / 1048576:.2f} MiB / {self._cache_budget / 1048576:g} MiB"
            if supported else ""
        )

    async def Showcase_on_loaded(self, event: UiEvent):
        self.update_cache_meter()
        self.create_task(self.animate_frames())
        self.create_task(self.measure_fps())

    def Showcase_on_viewport_changed(self, event: ChangeEvent):
        self.meter.visible = event.new_value.width >= 800

    async def animate_frames(self):
        start = monotonic()
        while True:
            if self.animate.checked:
                self.pulse.phase = (monotonic() - start) * 4
            await asyncio.sleep(1 / 60)

    async def measure_fps(self):
        previous, start = self.diagnostics().scene_submissions, monotonic()
        while True:
            await asyncio.sleep(1)
            now, count = monotonic(), self.diagnostics().scene_submissions
            self._fps = (count - previous) / (now - start)
            self.meter.text = f"Scene submissions/s {self._fps:.1f}"
            self.update_cache_meter()
            previous, start = count, now

    def page_theme_lab(self):
        Label(
            parent=self.workspace,
            text="Switch themes. Pause Animate to inspect the same scene.",
            height=24,
            font_size=13,
        )
        panel = ThemePanel(
            parent=self.workspace,
            layout="stack",
            padding=16,
            spacing=12,
            height=318,
            cache_paint=True,
        )
        Label(
            parent=panel, text="SIGNAL STUDIO / LIVE SESSION", height=20, font_size=13
        )
        columns = Container(
            parent=panel, layout="stack", direction="horizontal", flex=1, spacing=20
        )
        left = Container(parent=columns, layout="stack", flex=1, spacing=10)
        buttons = Container(
            parent=left, layout="stack", direction="horizontal", height=40, spacing=10
        )
        Button(parent=buttons, text="Launch", icon="play", flex=1)
        Button(parent=buttons, text="Offline", enabled=False, flex=1)
        self.lab_entry = TextBox(
            parent=left,
            text="Signal ready. Try editing this.",
            height=38,
            cache_paint=True,
        )
        self.lab_choice = Dropdown(
            parent=left,
            items=("Orbital relay", "Ground station", "Deep space"),
            selected_index=0,
            height=38,
            cache_paint=True,
        )
        checks = Container(
            parent=left, layout="stack", direction="horizontal", height=32
        )
        CheckBox(parent=checks, text="Sync", checked=True, flex=1)
        Toggle(parent=checks, text="Live", checked=True, flex=1)
        Slider(parent=left, value=68, height=28, cache_paint=True)
        ProgressBar(parent=left, value=68, height=22, show_text=False, cache_paint=True)
        right = Container(parent=columns, layout="stack", flex=1, spacing=10)
        tabs = TabControl(parent=right, height=154, tab_width=112, cache_paint=True)
        session = TabPage(parent=tabs, title="Session", layout="stack")
        ListView(
            parent=session,
            items=(
                "01  Handshake complete",
                "02  Signal acquired",
                "03  Upload queued",
            ),
            selected_index=1,
            row_height=32,
            flex=1,
            cache_paint=True,
        )
        TabPage(parent=tabs, title="History")
        self.lab_amount = NumericInput(
            parent=right,
            value=42.5,
            minimum=0,
            maximum=100,
            height=38,
            cache_paint=True,
        )
        RadioButton(parent=right, text="Local processing", checked=True, height=32)
        Label(
            parent=self.workspace,
            text="Edit a value, open the dropdown, switch tabs, or resize; caching must preserve it all.",
            height=24,
            font_size=12,
        )
        self.lab_window = Button(
            parent=self.workspace, text="Open floating window", height=36
        )

    def lab_window_on_click(self, event: ClickEvent):
        self.modeless_button_on_click(event)

    def show_page(self, name):
        for child in self.workspace.children:
            child.destroy()
        self.workspace.scroll_y = 0
        self.workspace.scroll_x = 0
        Label(parent=self.workspace, text=name, font_size=26, height=36)
        if name == "Overview":
            Label(
                parent=self.workspace,
                text="A shared Python UI engine for desktop and web.",
                height=28,
            )
            Label(
                parent=self.workspace,
                text=f"{len(self._catalog)} registered controls · typed properties · named events",
                height=26,
            )
            row = Container(
                parent=self.workspace, layout="stack", direction="horizontal", height=40
            )
            self.demo_button = Button(
                parent=row,
                text="Click me",
                width=140,
                tooltip="A named click handler updates the status",
            )
            self.demo_check = CheckBox(
                parent=row, text="Try a toggle", checked=True, flex=1
            )
            self.demo_text = TextBox(
                parent=self.workspace,
                placeholder="Type here — property events are live",
                height=38,
            )
            self.demo_slider = Slider(parent=self.workspace, value=40, height=38)
            Label(
                parent=self.workspace,
                text="The meter counts scene submissions. Pausing animation reduces redraws.",
                height=26,
                font_size=13,
            )
            Label(
                parent=self.workspace,
                text="The meter updates once per second, even when animation is paused.",
                height=24,
                font_size=13,
            )
            TextBox(
                parent=self.workspace,
                text="class Demo(App):\n    def build(self):\n        self.hello = Button(text='Hello')\n\n    def hello_on_click(self, event: ClickEvent):\n        self.hello.text = 'Clicked!'\n\nDemo().run_blocking()",
                multiline=True,
                read_only=True,
                monospace=True,
                height=220,
            )
        elif name == "Theme lab":
            self.page_theme_lab()
        elif name == "Label":
            Label(parent=self.workspace, text="A heading", font_size=30, height=44)
            Label(
                parent=self.workspace,
                text="Labels inherit the active theme.",
                height=30,
            )
            Label(
                parent=self.workspace, text="Disabled label", enabled=False, height=30
            )
        elif name == "Button":
            self.demo_button = Button(
                parent=self.workspace, text="Click or press Enter / Space", height=42
            )
            Button(
                parent=self.workspace, text="Disabled button", enabled=False, height=42
            )
        elif name == "Hyperlink":
            Hyperlink(
                parent=self.workspace,
                text="Read the Pysual documentation",
                url="https://github.com/PabloMP2P/Pysual/tree/main/docs",
                height=38,
            )
            Label(
                parent=self.workspace,
                text="Enter or click opens the link in your browser.",
                height=28,
            )
        elif name == "GroupBox":
            group = GroupBox(
                parent=self.workspace,
                title="Workspace preferences",
                layout="stack",
                height=152,
                spacing=10,
            )
            TextBox(parent=group, text="My workspace", height=38)
            CheckBox(parent=group, text="Keep drafts", checked=True, height=32)
        elif name == "Separator":
            Label(parent=self.workspace, text="Horizontal separator", height=28)
            Separator(parent=self.workspace, height=9)
            row = Container(
                parent=self.workspace,
                layout="stack",
                direction="horizontal",
                height=64,
                spacing=16,
            )
            Label(parent=row, text="Before", flex=1)
            Separator(parent=row, orientation="vertical", width=9)
            Label(parent=row, text="After", flex=1)
        elif name == "CheckBox":
            self.demo_check = CheckBox(
                parent=self.workspace,
                text="Enable the next button",
                checked=True,
                height=38,
            )
            self.controlled_button = Button(
                parent=self.workspace, text="Controlled by the checkbox", height=40
            )
            CheckBox(
                parent=self.workspace,
                text="Disabled checkbox",
                checked=True,
                enabled=False,
                height=38,
            )
        elif name == "Slider":
            self.demo_slider = Slider(
                parent=self.workspace,
                minimum=0,
                maximum=100,
                step=5,
                value=40,
                height=40,
            )
            self.slider_value = Label(
                parent=self.workspace, text="Value: 40 · step: 5", height=26
            )
            Label(
                parent=self.workspace,
                text="Drag the thumb or use arrow keys, Home and End.",
                height=26,
            )
        elif name == "TextBox":
            self.demo_text = TextBox(
                parent=self.workspace, placeholder="Single-line input", height=38
            )
            TextBox(parent=self.workspace, text="secret", password=True, height=38)
            TextBox(
                parent=self.workspace, text="Read-only text", read_only=True, height=38
            )
            TextBox(
                parent=self.workspace,
                text="Multiline editing\nSelect, copy, paste and undo.\nTab indents; Ctrl+Tab moves focus.",
                multiline=True,
                monospace=True,
                line_numbers=True,
                auto_indent=True,
                height=200,
            )
        elif name in ("Panel", "Layouts"):
            Label(parent=self.workspace, text="Stack / flex", height=24)
            row = Container(
                parent=self.workspace, layout="stack", direction="horizontal", height=42
            )
            for label, flex in (("Fixed 100", 0), ("Flex 1", 1), ("Flex 2", 2)):
                Button(
                    parent=row, text=label, width=100 if not flex else None, flex=flex
                )
            Label(parent=self.workspace, text="Grid with a two-column span", height=24)
            grid = Container(
                parent=self.workspace, layout="grid", columns=3, height=140, spacing=10
            )
            Button(parent=grid, text="Span 2", grid_row=0, grid_col=0, grid_col_span=2)
            Button(parent=grid, text="Cell", grid_row=0, grid_col=2)
            for i in range(3):
                Label(parent=grid, text=f"Row 2 / {i + 1}", grid_row=1, grid_col=i)
            Label(
                parent=self.workspace,
                text="Absolute placement + anchors · resize the app",
                height=24,
            )
            anchors = Container(parent=self.workspace, height=90)
            Button(parent=anchors, text="Pinned left", width=135, height=38)
            Button(
                parent=anchors,
                text="Stretch horizontally",
                top=48,
                width=300,
                height=38,
                anchor="left,right,top",
            )
        elif name == "ScrollArea":
            Label(
                parent=self.workspace,
                text="Wheel vertically; Shift+wheel horizontally.",
                height=26,
            )
            area = ScrollArea(parent=self.workspace, height=240)
            for i in range(24):
                Label(
                    parent=area,
                    text=f"Scrollable row {i + 1:02d} — content extends to the right →",
                    left=12,
                    top=i * 32,
                    width=960,
                    height=30,
                )
        elif name == "Image":
            Label(
                parent=self.workspace,
                text="An embedded PNG — the same example on both hosts.",
                height=26,
            )
            Image(parent=self.workspace, source=SAMPLE_IMAGE, width=288, height=144)
        elif name == "ListView":
            Label(
                parent=self.workspace,
                text="100,000 rows · painting is limited to the viewport.",
                height=26,
            )
            self.demo_list = ListView(
                parent=self.workspace,
                items=tuple(f"Item {i + 1:06d}" for i in range(100_000)),
                row_height=30,
                height=300,
            )
        elif name == "LineChart":
            Label(
                parent=self.workspace,
                text="20,000 data points · extrema retained · bounded drawing work",
                height=28,
            )
            self.demo_chart = LineChart(
                parent=self.workspace,
                title="Two signals",
                height=300,
                series=(
                    ChartSeries(
                        "first",
                        "Signal A",
                        tuple(
                            (i / 100, math.sin(i / 160) * 25 + 50)
                            for i in range(10_000)
                        ),
                    ),
                    ChartSeries(
                        "second",
                        "Signal B",
                        tuple(
                            (i / 100, math.cos(i / 400) * 18 + 45)
                            for i in range(10_000)
                        ),
                    ),
                ),
            )
        elif name == "DonutChart":
            DonutChart(
                parent=self.workspace,
                title="Workspace activity",
                slices=(
                    ChartSlice("design", "Design", 45),
                    ChartSlice("code", "Code", 35),
                    ChartSlice("review", "Review", 20),
                ),
                height=320,
            )
        elif name == "DataGrid":
            Label(
                parent=self.workspace,
                text="10,000 rows · click a header to sort · F2/Enter edits a cell",
                height=28,
            )
            self.demo_grid = DataGrid(
                parent=self.workspace,
                columns=(
                    GridColumn("name", "Project", width=200, editable=True),
                    GridColumn("hours", "Hours", kind="number", editable=True),
                    GridColumn("ready", "Ready", kind="bool", editable=True),
                    GridColumn("owner", "Owner", width=180),
                ),
                rows=tuple(
                    GridRow(
                        str(i), (f"Project {i:05}", i % 80, i % 3 == 0, f"Team {i % 6}")
                    )
                    for i in range(10_000)
                ),
                selected_key="0",
                height=300,
            )
        elif name == "MenuBar":
            self.demo_menu_bar = MenuBar(
                parent=self.workspace,
                groups=(
                    MenuGroup(
                        "File",
                        (
                            MenuItem("new", "New", "Ctrl+N"),
                            MenuItem("save", "Save", "Ctrl+S"),
                        ),
                    ),
                    MenuGroup(
                        "View",
                        (
                            MenuItem("details", "Show details", checked=True),
                            MenuItem("disabled", "Unavailable", enabled=False),
                        ),
                    ),
                ),
                height=34,
            )
            Label(
                parent=self.workspace,
                text="F10 opens the menu. Arrow keys navigate; Escape closes.",
                height=30,
            )
            TextBox(
                parent=self.workspace,
                placeholder="Ctrl+S also works while editing here",
                height=38,
            )
        elif name == "Menu":
            self.demo_menu = Menu(
                parent=self.workspace,
                items=(
                    MenuItem("copy", "Copy selection", "Ctrl+Shift+C"),
                    MenuItem("separator", "", separator=True),
                    MenuItem("inspect", "Inspect"),
                ),
            )
            self.context_target = Button(
                parent=self.workspace,
                text="Click or right-click for a context menu",
                height=44,
            )
        elif name in ("TabControl", "TabPage"):
            self.demo_tabs = TabControl(parent=self.workspace, height=280)
            for title in ("General", "Appearance", "Advanced"):
                page = TabPage(
                    parent=self.demo_tabs, title=title, layout="stack", padding=14
                )
                Label(parent=page, text=f"{title} settings", height=30)
                TextBox(
                    parent=page,
                    placeholder="Each page keeps its controls and state",
                    height=38,
                )
                CheckBox(parent=page, text="Enabled", checked=True, height=36)
        elif name == "SplitPane":
            Label(
                parent=self.workspace,
                text="Drag the divider or focus it and use arrow keys.",
                height=28,
            )
            split = SplitPane(parent=self.workspace, position=0.35, height=280)
            split.first.layout, split.second.layout = "stack", "stack"
            split.first.padding, split.second.padding = 12, 12
            split.first.min_width, split.second.min_width = 100, 140
            Label(parent=split.first, text="Navigation", height=30)
            TreeView(
                parent=split.first,
                nodes=(TreeNode("root", "Project", (TreeNode("item", "Item"),)),),
                selected_key="item",
                flex=1,
            )
            Label(parent=split.second, text="Document", height=30)
            TextBox(
                parent=split.second,
                text="The panes use normal containers.\nTheir contents retain state while resizing.",
                multiline=True,
                flex=1,
            )
        elif name == "Dropdown":
            Label(
                parent=self.workspace,
                text="Keyboard or pointer selection; Escape cancels.",
                height=28,
            )
            self.demo_dropdown = Dropdown(
                parent=self.workspace,
                items=("Modern", "Windows", "macOS"),
                selected_index=0,
                height=38,
            )
        elif name == "ComboBox":
            Label(
                parent=self.workspace,
                text="Type any value, or choose a suggestion with Down.",
                height=28,
            )
            self.demo_combo = ComboBox(
                parent=self.workspace,
                items=("Python", "Rust", "TypeScript"),
                text="Python",
                height=38,
            )
        elif name == "Toggle":
            self.demo_toggle = Toggle(
                parent=self.workspace, text="Live preview", checked=True, height=38
            )
        elif name == "RadioButton":
            Label(
                parent=self.workspace,
                text="One checked option per sibling group.",
                height=28,
            )
            RadioButton(parent=self.workspace, text="Compact", checked=True, height=38)
            RadioButton(parent=self.workspace, text="Comfortable", height=38)
            RadioButton(parent=self.workspace, text="Spacious", height=38)
        elif name == "ColorPicker":
            Label(
                parent=self.workspace,
                text="Edit RGB/alpha channels or a hex value. Escape cancels.",
                height=28,
            )
            ColorPicker(
                parent=self.workspace,
                value=self.theme.tokens.accent,
                width=260,
                height=40,
            )
        elif name == "NumericInput":
            Label(
                parent=self.workspace,
                text="Arrow keys step; Shift steps 10×. Enter opens exact entry.",
                height=28,
            )
            NumericInput(
                parent=self.workspace,
                value=42,
                minimum=0,
                maximum=100,
                width=260,
                height=40,
            )
        elif name == "ProgressBar":
            self.demo_progress = ProgressBar(parent=self.workspace, value=65, height=32)
            ProgressBar(parent=self.workspace, value=30, show_text=False, height=10)
            Label(
                parent=self.workspace,
                text="Set value as async work progresses; the control owns no timer.",
                height=28,
                font_size=13,
            )
        elif name == "Popup":
            self.popup_button = Button(
                parent=self.workspace, text="Open popup content", height=38
            )
            Label(
                parent=self.workspace,
                text="Escape, Tab, outside click and owner disposal close the popup.",
                height=28,
                font_size=13,
            )
        elif name == "TreeView":
            Label(
                parent=self.workspace,
                text="Arrows navigate · type to find · Enter activates",
                height=26,
                font_size=14,
            )
            self.demo_tree = TreeView(
                parent=self.workspace,
                nodes=(
                    TreeNode(
                        "project",
                        "Project",
                        (
                            TreeNode(
                                "source",
                                "Source",
                                (
                                    TreeNode("app", "app.py"),
                                    TreeNode("theme", "theme.py"),
                                ),
                            ),
                            TreeNode(
                                "assets", "Assets", (TreeNode("logo", "logo.png"),)
                            ),
                            TreeNode("readme", "README.md"),
                        ),
                    ),
                    TreeNode(
                        "data",
                        "10,000 records",
                        tuple(
                            TreeNode(f"record-{i}", f"Record {i + 1:05d}")
                            for i in range(10_000)
                        ),
                    ),
                ),
                expanded_keys=("project", "source"),
                selected_key="app",
                row_height=30,
                height=300,
            )
            Label(
                parent=self.workspace,
                text="Expand branches or drag the scrollbar; only visible rows paint.",
                height=26,
                font_size=13,
            )
        elif name == "SubWindow":
            Label(
                parent=self.workspace,
                text="Draggable, resizable containers inside the current App.",
                height=26,
            )
            self.modeless_button = Button(
                parent=self.workspace, text="Add a modeless subwindow", height=42
            )
            self.modal_button = Button(
                parent=self.workspace, text="Open an awaited modal dialog", height=42
            )
        elif name == "Async tasks":
            Label(
                parent=self.workspace,
                text="Timers yield while the controls and animation remain responsive.",
                height=26,
                font_size=14,
            )
            self.task_button = Button(
                parent=self.workspace, text="Run async task", height=42
            )
            self.task_status = Label(parent=self.workspace, text="Ready", height=28)
            self.task_progress = ProgressBar(parent=self.workspace, height=26)
            self.demo_slider = Slider(parent=self.workspace, value=40, height=40)
            self.demo_text = TextBox(
                parent=self.workspace,
                placeholder="Keep typing while the task runs",
                height=38,
            )
        elif name == "Custom control":
            Label(
                parent=self.workspace,
                text="The animated bars are a registered, typed custom control.",
                height=28,
                font_size=14,
            )
            TextBox(
                parent=self.workspace,
                text="class Orbit(Control):\n    phase: float = prop(default=0.0)\n\n    def paint(self, painter, /):\n        # Draw from phase using Painter primitives.\n        ...\n\nregister_control(Orbit)\n\n# Registration adds catalog discovery.\n# Constructors and automatic names need no App boilerplate:\nself.pulse = Orbit(width=70, height=32)",
                multiline=True,
                read_only=True,
                monospace=True,
                height=300,
            )
            Label(
                parent=self.workspace,
                text="Custom factory autocomplete remains an open tooling gate.",
                height=26,
                font_size=13,
            )
        elif name == "Host capabilities":
            Label(
                parent=self.workspace,
                text="Capabilities describe what this host can provide.",
                height=26,
            )
            ListView(
                parent=self.workspace,
                items=tuple(sorted(self.capabilities)),
                height=180,
            )
            Label(
                parent=self.workspace,
                text="Desktop and web run the same controls, events and layout.",
                height=26,
            )
        elif name in self._catalog:
            cls = self._catalog[name]
            try:
                signature(cls.__init__).bind(None)
            except TypeError as error:
                Label(
                    parent=self.workspace,
                    text=f"{name} needs constructor inputs: {error}. Create a sample in your app with those inputs.",
                    height=54,
                )
            else:
                sample = cls(parent=self.workspace)
                sample.min_height = max(48, sample.min_height)
        cls = self._catalog.get(name)
        if cls is not None:
            Label(
                parent=self.workspace,
                text="Declared properties and events",
                height=30,
                font_size=17,
            )
            fields = [
                f"{field.name}: {_annotation_name(field.annotation)} = {field.definition.default!r}"
                for field in cls.properties().values()
            ]
            events = [
                f"{event_name}(event: {event.event_type.__name__})"
                for event_name, event in cls.events().items()
            ]
            TextBox(
                parent=self.workspace,
                text="\n".join(fields + ["", "Events:"] + events),
                multiline=True,
                read_only=True,
                monospace=True,
                height=200,
                font_size=13,
            )

    def demo_button_on_click(self, event: ClickEvent):
        self.status.text = "demo_button_on_click received a ClickEvent."

    def context_target_on_click(self, event: ClickEvent):
        self.demo_menu.show(self.context_target)

    def context_target_on_context_menu(self, event: PointerEvent):
        self.demo_menu.show(self.context_target, at=(event.x, event.y))

    def demo_menu_on_command(self, event: CommandEvent):
        self.status.text = f"Command: {event.key}"

    def demo_grid_on_edited(self, event: GridEditEvent):
        self.status.text = (
            f"Updated row {event.row_key}: {event.column_key} = {event.new_value}"
        )

    def demo_menu_bar_on_command(self, event: CommandEvent):
        self.status.text = f"Command: {event.key}"

    def demo_check_on_changed(self, event: ChangeEvent[bool]):
        self.status.text = f"Checkbox: {event.new_value} · origin: {event.origin}"
        if self._pages[self.catalog.selected_index] == "CheckBox":
            self.controlled_button.enabled = event.new_value

    def demo_slider_on_changed(self, event: ChangeEvent[float]):
        self.status.text = f"Slider: {event.new_value:g} · origin: {event.origin}"
        if self._pages[self.catalog.selected_index] == "Slider":
            self.slider_value.text = f"Value: {event.new_value:g} · step: 5"

    def demo_text_on_changed(self, event: ChangeEvent[str]):
        self.status.text = (
            f"Text changed · {len(event.new_value)} characters · origin: {event.origin}"
        )

    def demo_list_on_changed(self, event: ChangeEvent[int]):
        self.status.text = f"Selected row {event.new_value + 1:,} of 100,000"

    def demo_tree_on_changed(self, event: ChangeEvent[str | None]):
        self.status.text = f"Selected node: {event.new_value}"

    def demo_tree_on_activated(self, event: TreeEvent):
        self.status.text = f"Activated node: {event.key}"

    def demo_dropdown_on_changed(self, event: ChangeEvent[int]):
        self.status.text = f"Choice index: {event.new_value}"

    def demo_combo_on_changed(self, event: ChangeEvent[str]):
        self.status.text = f"Value: {event.new_value}"

    def popup_button_on_click(self, event: ClickEvent):
        panel = Popup(width=300, height=120, layout="stack", padding=12)
        Label(parent=panel, text="Ordinary controls in a popup", height=26)
        Button(parent=panel, text="Close", height=36).click.connect(
            lambda event: panel.dismiss()
        )
        panel.show(self.popup_button)

    async def task_button_on_click(self, event: ClickEvent):
        self.task_button.enabled = False
        for i in range(10):
            self.task_status.text = f"Working… {(i + 1) * 10}%"
            self.task_progress.value = (i + 1) * 10
            await asyncio.sleep(0.1)
        self.task_status.text = "Done — the UI stayed responsive."
        self.task_button.enabled = True

    def _window_bounds(self, width: float, height: float) -> Rect:
        # Child positions start inside the App's padding.
        viewport_width = max(1, (self.width or 960) - 2 * self.padding)
        viewport_height = max(1, (self.height or 640) - 2 * self.padding)
        width, height = min(width, viewport_width), min(height, viewport_height)
        return Rect(
            (viewport_width - width) / 2, (viewport_height - height) / 2, width, height
        )

    def modeless_button_on_click(self, event: ClickEvent):
        self._window_number += 1
        bounds = self._window_bounds(360, 220)
        window = SubWindow(
            parent=self,
            title=f"Subwindow {self._window_number} · drag me",
            layout="stack",
            left=bounds.x,
            top=bounds.y,
            width=bounds.width,
            height=bounds.height,
            cache_paint=True,
        )
        Label(
            parent=window,
            text="Keep using the app behind this window.",
            height=30,
            font_size=13,
        )
        TextBox(parent=window, placeholder="Independent control state", height=38)
        CheckBox(
            parent=window,
            text="Resizable, draggable, closable",
            checked=True,
            height=34,
            font_size=13,
        )
        presentation = window.show()

        async def dispose_when_closed():
            try:
                await presentation.wait_async()
            finally:
                window.destroy()

        self.create_task(dispose_when_closed())

    async def modal_button_on_click(self, event: ClickEvent):
        if self._dialog_open:
            return
        self._dialog_open = True
        bounds = self._window_bounds(400, 220)
        dialog = SubWindow(
            parent=self,
            title="Awaited modal result",
            layout="stack",
            left=bounds.x,
            top=bounds.y,
            width=bounds.width,
            height=bounds.height,
        )
        Label(
            parent=dialog,
            text="The animation continues while you decide.",
            height=32,
            font_size=13,
        )
        accept = Button(parent=dialog, text="Continue", height=40)

        async def accepted(event: ClickEvent):
            await dialog.close_async("accepted")

        accept.click.connect(accepted)
        try:
            result = await dialog.show_modal_async()
            self.status.text = f"Dialog result: {result or 'closed'}"
        finally:
            dialog.destroy()
            self._dialog_open = False


def main():
    # Activate CLI/environment settings only when launching this script.
    from pysual import autoconfig  # noqa: F401

    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Use --backend NAME to choose a backend (default: window). "
            "Use --pysual-help for backend names and theme/scale settings."
        ),
    )
    parser.parse_args()
    Showcase().run_blocking()


if __name__ == "__main__":
    main()
