"""An interactive material study of nine desktop worlds, in light and dark.

Run: python examples/theme_gallery.py --backend web --pysual-theme modern
"""


from pysual import (
    App, Badge, Button, CheckBox, Container, DataGrid, DatePicker, Disclosure,
    Dropdown, GridColumn, GridRow, Label, MenuBar, MenuGroup, MenuItem, Meter,
    NumericInput, ProgressBar, RadioButton, Rating, Rect, ScrollArea, SearchField,
    SegmentedControl, Slider, Style, SubWindow, TextBox, Toggle, get_theme, prop,
)


# Presentation copy belongs to the gallery; themes themselves remain pure data.
FAMILIES = (
    ("modern", "Modern", "Less noise. More possibility.",
     "Quiet surfaces, precise spacing, and a considered touch of color.", "CONTEMPORARY / EVERYDAY"),
    ("windows", "Windows", "A familiar place to create.",
     "Crisp geometry, layered surfaces, and the confidence of Fluent blue.", "FLUENT / DESKTOP"),
    ("macos", "macOS", "Every detail feels at home.",
     "Softly inset fields, luminous selections, and carefully lifted controls.", "AQUA / DESKTOP"),
    ("terminal", "Terminal", "All signal. No distraction.",
     "DOS utility blue meets the disciplined character of ncurses.", "TEXT MODE / COMMAND CENTER"),
    ("win31", "Win31", "Back to the program manager.",
     "Sculpted bevels, square corners, and unmistakable navy title bars.", "1992 / CLASSIC DESKTOP"),
    ("winxp", "WinXP", "A little more delightful.",
     "Luna blue, warm ivory, and the reassuring glow of green progress.", "2001 / LUNA"),
    ("macintosh", "Macintosh", "Hello, again.",
     "Monochrome clarity, striped title bars, and beautifully simple controls.", "1984 / SYSTEM CLASSIC"),
    ("analog_arcade", "Analog arcade", "One more turn.",
     "Warm cabinet materials, tactile switches, and the glow of a CRT.", "ANALOG / PLAYROOM"),
    ("neon", "Neon", "After hours. Fully alive.",
     "Electric edges, cyan signals, and a luminous magenta accent.", "ELECTRIC / NIGHT SHIFT"),
)


class Caption(Label):
    def paint(self, p, /):
        style = p.style()
        text = p.elide(self.text, p.width, size=style.font_size)
        _, height = p.measure(text, size=style.font_size)
        p.text(text, 0, max(0, (p.height - height) / 2),
               color=p.theme.tokens.muted, size=style.font_size)


class IntroLabel(Label):
    """Wrap the introduction on compact windows using the actual font metrics."""

    muted: bool = prop(default=False)

    def paint(self, p, /):
        style = p.style()
        lines, current = [], ""
        for word in self.text.split():
            candidate = f"{current} {word}".strip()
            if current and p.measure(candidate, size=style.font_size)[0] > p.width:
                lines.append(current)
                current = word
            else:
                current = candidate
        if current:
            lines.append(current)
        line_height = p.measure("Ag", size=style.font_size)[1]
        color = p.theme.tokens.muted if self.muted else style.foreground
        for index, line in enumerate(lines[:max(1, int(p.height / line_height))]):
            p.text(line, 0, index * line_height, color=color, size=style.font_size)


class ThemeLink(Button):
    """An ordinary keyboard-operable button with a quiet navigation treatment."""

    active: bool = prop(default=False)
    ordinal: str = prop(default="01")

    def paint(self, p, /):
        t = p.theme.tokens
        selected = self.active
        hovered = self._hover
        p.surface(Rect(0, 0, p.width, p.height), Style(
            fill=t.selection if selected else t.surface if hovered else "#00000000",
            border=t.accent if selected else "#00000000", border_width=1,
            radius=min(t.radius, 7),
        ))
        p.text(self.ordinal, 12, (p.height - 14) / 2, size=11,
               color=t.foreground if selected else t.muted, mono=True)
        p.text(self.text, 39, (p.height - 18) / 2, size=14, color=t.foreground)
        if selected:
            p.rect(Rect(p.width - 15, (p.height - 5) / 2, 5, 5), t.foreground, radius=2)


class Section(Container):
    """A specimen panel with a title painted in the theme's window material."""

    caption: str = prop(default="Controls")
    number: str = prop(default="01")

    def content_bounds(self, bounds):
        area = super().content_bounds(bounds)
        return Rect(area.x, area.y + 37, area.width, max(0, area.height - 37))

    def paint(self, p, /):
        p.body()
        title = p.theme.resolve("SubWindow", "titlebar")
        p.surface(Rect(1, 1, max(0, p.width - 2), 36), title)
        text = p.elide(f"{self.number}  {self.caption}".strip(), max(0, p.width - 30),
                       size=13, font_family=title.font_family)
        if title.pattern not in (None, "none"):
            width, _ = p.measure(text, size=13, font_family=title.font_family)
            p.rect(Rect(11, 6, min(width + 8, max(0, p.width - 22)), 23), title.fill)
        p.text(text, 15, 10, size=13,
               color=title.foreground, font_family=title.font_family)


class SecondaryButton(Button):
    # Reuse the family-specific neutral action face while retaining Button input.
    _style_kind = "Dropdown"
    style_excludes = ("Button",)


class ThemeGallery(App):
    def build(self):
        self.title = "Pysual / Theme atelier"
        self.width, self.height = 1440, 1040
        self.layout, self.padding, self.spacing = "stack", 24, 18
        current = next((i for i, item in enumerate(FAMILIES)
                        if self.theme in (get_theme(item[0]), get_theme(item[0] + "_dark"))), 0)
        self._family = current
        self._night = self.theme == get_theme(FAMILIES[current][0] + "_dark")
        self._theme_links = []
        self._specimens = []

        self.topbar = Container(parent=self, layout="grid", column_tracks=("1*", 240, 180),
                             spacing=18, height=38)
        Label(parent=self.topbar, text="P Y S U A L   /   T H E M E   A T E L I E R", font_size=13)
        self.family_choice = Dropdown(parent=self.topbar, items=tuple(f[1] for f in FAMILIES),
                                      selected_index=current, tooltip="Theme family")
        self.appearance = SegmentedControl(parent=self.topbar, items=("Light", "Dark"),
                                           selected_index=int(self._night), tooltip="Appearance")

        self.workspace = Container(parent=self, layout="grid", column_tracks=(194, "1*"),
                                   spacing=28, flex=1)
        self.sidebar = ScrollArea(parent=self.workspace, layout="stack", spacing=6)
        Caption(parent=self.sidebar, text="NINE WORLDS TO EXPLORE", height=30, font_size=10)
        for index, (_, label, *_) in enumerate(FAMILIES):
            button = ThemeLink(parent=self.sidebar, text=label, ordinal=f"{index + 1:02}",
                               active=index == current, height=44)
            button.click.connect(lambda event: self._select_family(self._theme_links.index(event.source)))
            self._theme_links.append(button)
        Caption(parent=self.sidebar, text="18 considered appearances", height=36, font_size=11)
        Caption(parent=self.sidebar, text="Tab to explore.\nEnter to activate.", height=46, font_size=11)
        self.reduce = CheckBox(parent=self.sidebar, text="Reduce motion", height=34,
                               checked=self.reduce_motion, font_size=12)

        self.scroll = ScrollArea(parent=self.workspace, layout="stack", spacing=16,
                                 padding=6)
        self.eyebrow = Caption(parent=self.scroll, height=21, font_size=11)
        self.heading = IntroLabel(parent=self.scroll, height=48, font_size=34)
        self.description = IntroLabel(parent=self.scroll, height=24, font_size=14, muted=True)
        self.chrome = Section(parent=self.scroll, caption="Your creative workspace", number="",
                              layout="stack", padding=14, spacing=10, height=148)
        self.command_bar = MenuBar(parent=self.chrome, height=27, groups=(
            MenuGroup("File", (MenuItem("new", "New project", "Ctrl+N"),
                               MenuItem("save", "Save workspace", "Ctrl+S"))),
            MenuGroup("Edit", (MenuItem("reset", "Reset values"),)),
            MenuGroup("View", (MenuItem("light", "Light appearance"),
                               MenuItem("dark", "Dark appearance"))),
            MenuGroup("Help", (MenuItem("help", "Keyboard controls"),)),
        ))
        self.toolbar = Container(parent=self.chrome, layout="grid",
                                 column_tracks=(40, 40, "1*", 110), spacing=9, height=38)
        self.back = SecondaryButton(parent=self.toolbar, icon="undo", tooltip="Reset values")
        self.folder = SecondaryButton(parent=self.toolbar, icon="folder", tooltip="Open project details")
        self.search = SearchField(parent=self.toolbar, placeholder="Search the component library…")
        self.new_project = Button(parent=self.toolbar, text="New project", font_size=12)

        self.grid = Container(parent=self.scroll, layout="grid", column_tracks=("1*",) * 3,
                              row_tracks=("auto", "auto"), spacing=16)
        actions = self._section("Buttons & inputs", "01", 350)
        row = self._row(actions, ("1*", "1*"), 39)
        self.primary = Button(parent=row, text="Save changes", icon="check", font_size=12)
        self.secondary = SecondaryButton(parent=row, text="Preview", font_size=12)
        row = self._row(actions, ("1*", "1*"), 35)
        Button(parent=row, text="Unavailable", enabled=False, font_size=12)
        self.dialog_button = SecondaryButton(parent=row, text="Open dialog", font_size=12)
        Caption(parent=actions, text="PROJECT NAME", height=18, font_size=10)
        self.project_name = TextBox(parent=actions, text="A little possibility", height=37)
        self.destination = Dropdown(parent=actions, items=("Personal workspace", "Design studio", "Archive"),
                                    selected_index=0, height=37)
        self.disabled_field = TextBox(parent=actions, text="Read-only specimen", read_only=True, height=35)
        self.action_hint = Caption(parent=actions, text="Try a field, a menu, or a keyboard shortcut.",
                                   font_size=11, height=20)

        choices = self._section("Selection & switches", "02", 350)
        self.period = SegmentedControl(parent=choices, items=("Day", "Week", "Month"),
                                       selected_index=1, height=37)
        self.live = Toggle(parent=choices, text="Live preview", checked=True, height=35)
        self.notifications = Toggle(parent=choices, text="Notifications", checked=False, height=35)
        self.snap = CheckBox(parent=choices, text="Snap to pixel grid", checked=True, height=30)
        self.hidden_layers = CheckBox(parent=choices, text="Include hidden layers", height=30)
        row = self._row(choices, ("1*", "1*"), 31)
        self.standard = RadioButton(parent=row, text="Standard", checked=True, font_size=12)
        self.compact = RadioButton(parent=row, text="Compact", font_size=12)
        CheckBox(parent=choices, text="Managed by your team", enabled=False, checked=True, height=30)

        values = self._section("Precision & progress", "03", 350)
        self.value_label = Caption(parent=values, text="INTENSITY / 68%", height=18, font_size=10)
        self.intensity = Slider(parent=values, value=68, height=32)
        self.progress = ProgressBar(parent=values, value=68, height=13)
        row = self._row(values, ("1*", 110), 38)
        Caption(parent=row, text="Copies", font_size=13)
        self.copies = NumericInput(parent=row, value=3, minimum=1, maximum=99, decimals=0)
        self.rating = Rating(parent=values, value=4, font_size=24, height=34)
        self.meter = Meter(parent=values, value=68, label="Render capacity", unit="%", height=52)
        Caption(parent=values, text="Arrow keys refine. Home / End go to the limits.",
                font_size=10, height=24)

        files = self._section("Library & collections", "04", 298)
        self.files = DataGrid(parent=files, height=194, columns=(
            GridColumn("name", "Name", width=152), GridColumn("kind", "Kind", width=88),
        ), rows=(
            GridRow("1", ("Brand system", "Folder")),
            GridRow("2", ("Desktop studies", "Folder")),
            GridRow("3", ("Components", "Library")),
            GridRow("4", ("Readme.txt", "Text")),
        ), selected_key="1")
        self.library_status = Caption(parent=files, text="4 items / Select a row to inspect", height=20, font_size=11)

        details = self._section("Dates & details", "05", 298)
        Caption(parent=details, text="NEXT DESIGN REVIEW", height=20, font_size=10)
        self.date = DatePicker(parent=details, value="2026-10-15", height=38)
        self.disclosure = Disclosure(parent=details, title="Workspace details",
                                     summary="Made for the way you work", expanded=False)
        Caption(parent=self.disclosure.content, text="Local workspace · All changes stay in this demo.",
                font_size=11, height=27)
        self.details_note = Caption(parent=details, text="Open the date picker to explore the calendar.",
                                    height=26, font_size=11)

        feedback = self._section("Status & feedback", "06", 298)
        row = self._row(feedback, ("1*", "1*"), 29)
        Badge(parent=row, text="All synced", tone="success")
        Badge(parent=row, text="In review", tone="warning")
        row = self._row(feedback, ("1*", "1*"), 29)
        Badge(parent=row, text="Updated", tone="info")
        Badge(parent=row, text="Offline", tone="neutral")
        Label(parent=feedback, text="Ready when you are.", font_size=19, height=38)
        Caption(parent=feedback, text="A real interface. Every control is yours to try.",
                font_size=11, height=27)
        self.restore = SecondaryButton(parent=feedback, text="Reset the playground", icon="undo", height=36)

        self.empty = Caption(parent=self.scroll, text="No specimens match. Clear the search to see all controls.",
                             height=48, visible=False)
        self.footer = Caption(parent=self, text="LIVE PLAYGROUND  /  Changes are kept while you explore themes.",
                              height=22, font_size=11)
        self.dialog = SubWindow(parent=self, title="A place for your next idea", visible=False,
                                width=390, height=205, layout="stack", padding=20, spacing=12)
        Label(parent=self.dialog, text="Looking good.", font_size=23, height=35)
        Caption(parent=self.dialog, text="Your workspace is ready. Keep making it yours.", font_size=12, height=25)
        self.close_dialog = Button(parent=self.dialog, text="Back to the playground", height=38)
        self._refresh_theme()
        self._adapt(self.width)

    def _row(self, parent, tracks, height):
        return Container(parent=parent, layout="grid", column_tracks=tracks, spacing=8, height=height)

    def _section(self, caption, number, height):
        section = Section(parent=self.grid, caption=caption, number=number,
                          layout="stack", padding=16, spacing=9, height=height)
        self._specimens.append(section)
        return section

    def _select_family(self, index):
        self._family = index
        self.family_choice.selected_index = index
        self._refresh_theme()

    def _refresh_theme(self):
        key, label, heading, description, era = FAMILIES[self._family]
        self.theme = get_theme(key + ("_dark" if self._night else ""))
        self.heading.text, self.description.text = heading, description
        self.eyebrow.text = f"{era}  /  {'DARK' if self._night else 'LIGHT'}"
        self.chrome.caption = f"{label} / Your creative workspace"
        for index, button in enumerate(self._theme_links):
            button.active = index == self._family

    def _adapt(self, width):
        compact = width < 1000
        self.padding = 12 if compact else 24
        self.sidebar.visible = not compact
        self.workspace.column_tracks = ("1*",) if compact else (194, "1*")
        self.workspace.spacing = 0 if compact else 28
        columns = 1 if width < 710 else 2 if width < 1270 else 3
        self.grid.column_tracks = ("1*",) * columns
        self.grid.row_tracks = ("auto",) * ((6 + columns - 1) // columns)
        self.topbar.column_tracks = ("1*", 180) if width < 710 else ("1*", 220, 180)
        self.topbar.children[0].visible = width >= 710
        self.heading.font_size = 25 if width < 710 else 34
        self.heading.height = 66 if width < 710 else 48
        self.description.height = 54 if width < 710 else 24
        self.toolbar.column_tracks = (34, 34, "1*", 0) if width < 540 else (40, 40, "1*", 110)
        self.new_project.visible = width >= 540
        if self.dialog.visible:
            self._position_dialog(width)

    def ThemeGallery_on_viewport_changed(self, event):
        self._adapt(event.new_value.width)

    def family_choice_on_changed(self, event):
        if event.new_value >= 0:
            self._family = event.new_value
            self._refresh_theme()

    def appearance_on_changed(self, event):
        self._night = event.new_value == 1
        self._refresh_theme()

    def reduce_on_changed(self, event):
        self.reduce_motion = self.reduce.checked

    def intensity_on_changed(self, event):
        self.progress.value = self.meter.value = self.intensity.value
        self.value_label.text = f"INTENSITY / {self.intensity.value:g}%"

    def search_on_changed(self, event):
        query = self.search.text.strip().casefold()
        for section in self._specimens:
            section.visible = not query or query in section.caption.casefold()
        self.empty.visible = not any(section.visible for section in self._specimens)

    def primary_on_click(self, event):
        self.footer.text = f"SAVED / {self.project_name.text or 'Untitled project'} · Changes saved in this playground."

    def secondary_on_click(self, event):
        self._show_dialog()

    def new_project_on_click(self, event):
        self.search.text = ""
        # Change events are queued; make the destination visible before focus.
        self.search_on_changed(event)
        self.scroll.scroll_y = 0
        self.project_name.text = "Untitled project"
        self.project_name.focus()
        self.footer.text = "NEW PROJECT / Give your next idea a name."

    def folder_on_click(self, event):
        self.disclosure.expanded = not self.disclosure.expanded
        self.footer.text = "WORKSPACE / Details are in the Dates & details specimen."

    def _show_dialog(self):
        self._position_dialog(self.width or 800)
        self.dialog.show(modal=True)

    def _position_dialog(self, width):
        available = max(0, width - self.padding * 2)
        self.dialog.width = min(390, max(1, available - 16))
        self.dialog.left = max(0, (available - self.dialog.width) / 2)
        self.dialog.top = max(0, min(160, (self.height or 800) - self.padding * 2 - self.dialog.height))

    def dialog_button_on_click(self, event):
        self._show_dialog()

    def close_dialog_on_click(self, event):
        self.dialog.close()

    def _reset(self):
        self.intensity.value, self.copies.value, self.rating.value = 68, 3, 4
        self.project_name.text = "A little possibility"
        self.search.text = ""
        self.live.checked = self.snap.checked = True
        self.notifications.checked = self.hidden_layers.checked = False
        self.standard.checked = True
        self.period.selected_index = 1
        self.destination.selected_index = 0
        self.date.value = "2026-10-15"
        self.files.selected_key = "1"
        self.disclosure.expanded = False
        self.footer.text = "PLAYGROUND RESET / Your theme selection is kept."

    def restore_on_click(self, event):
        self._reset()

    def back_on_click(self, event):
        self._reset()

    def command_bar_on_command(self, event):
        if event.key == "reset":
            self._reset()
        elif event.key == "new":
            self.new_project_on_click(event)
        elif event.key == "save":
            self.primary_on_click(event)
        elif event.key in ("light", "dark"):
            self.appearance.selected_index = int(event.key == "dark")
        else:
            self.footer.text = "KEYBOARD / Tab moves focus · Space selects · Arrow keys adjust · Escape closes."


if __name__ == "__main__":
    from argparse import ArgumentParser
    from pysual import autoconfig

    ArgumentParser(description=__doc__).parse_args()
    ThemeGallery().run_blocking()
