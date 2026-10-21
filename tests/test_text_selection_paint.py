"""A stored selection is painted only while that editor is focused.

The calendar still selects its date so the next keystroke can replace it.
Web, Python terminal, native window, and native terminal output must agree.
"""

import xml.etree.ElementTree as ET

from _terminal import MemoryTerminal
from _ui_testcase import UIOwnerTestCase
from test_web_svg import RecordingRenderer

from pysual import App, CalendarEditor, TextBox, light
from pysual.backends._native_client import native_executable
from pysual.backends.native import NativeHost, NativeTermTextHost
from pysual.backends.term_text import TermTextHost
from pysual.geometry import Rect
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree, resolve_style
from pysual.runtime import Runtime

_SVG = "{http://www.w3.org/2000/svg}"


class SelectionFocusPaintTests(UIOwnerTestCase):
    def backends(self):
        web = RecordingRenderer()
        web.reset_scene("Selection focus", 480, 320)
        self.addCleanup(web.close_scene)
        hosts = [web]
        terminal = TermTextHost(io=MemoryTerminal(60, 20), probe_timeout=0)
        terminal.open("Selection focus", 480, 320, False, 1)
        self.addCleanup(terminal.close)
        hosts.append(terminal)
        if native_executable().is_file():
            window = NativeHost(hidden=True, vsync=False)
            window.open("Selection focus", 480, 320, False, 1)
            self.addCleanup(window.close)
            hosts.append(window)
            native_terminal = NativeTermTextHost(hidden=True)
            native_terminal.open("Selection focus", 480, 320, False, 1)
            self.addCleanup(native_terminal.close)
            hosts.append(native_terminal)
        return hosts

    def app(self, host):
        app = App(width=480, height=320, theme=light(), reduce_motion=True, layout="absolute")
        self.addCleanup(app.destroy)
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        return app, runtime

    def paint(self, app, host, runtime):
        paint_tree(app, host, runtime.router.focus)

    def overlaps(self, rect, bounds):
        return (
            rect.x < bounds.right and rect.right > bounds.x
            and rect.y < bounds.bottom and rect.bottom > bounds.y
        )

    def washes(self, host, entry):
        """Selection-colored output that lands inside the editor."""
        color = entry.effective_theme.tokens.selection
        bounds = entry.bounds
        if isinstance(host, TermTextHost):
            red, green, blue = (int(color[index:index + 2], 16) for index in (1, 3, 5))
            renderer = host._renderer
            hits = []
            for index, cell in enumerate(renderer.cells):
                if cell.background != (red, green, blue):
                    continue
                column, row = index % renderer.columns, index // renderer.columns
                if bounds.contains(
                    (column + 0.5) * renderer.cell_width,
                    (row + 0.5) * renderer.cell_height,
                ):
                    hits.append(index)
            return hits
        if isinstance(host, NativeHost):
            hits = []
            for command in host._commands:
                if command[0] != "rect" or command[2] != color:
                    continue
                rect = Rect(*command[1])
                if self.overlaps(rect, bounds):
                    hits.append(rect)
            return hits
        hits = []
        for element in ET.fromstring(host.export_svg()).iter(f"{_SVG}rect"):
            if element.get("fill") != color:
                continue
            rect = Rect(
                float(element.get("x", 0)), float(element.get("y", 0)),
                float(element.get("width", 0)), float(element.get("height", 0)),
            )
            if self.overlaps(rect, bounds):
                hits.append(rect)
        return hits

    def visible(self, host, entry):
        bounds = entry.bounds
        if isinstance(host, TermTextHost):
            renderer = host._renderer
            chars = []
            for index, cell in enumerate(renderer.cells):
                column, row = index % renderer.columns, index // renderer.columns
                if bounds.contains(
                    (column + 0.5) * renderer.cell_width,
                    (row + 0.5) * renderer.cell_height,
                ):
                    chars.append(cell.text)
            return "".join(chars)
        if isinstance(host, NativeHost):
            return "".join(
                command[1] for command in host._commands
                if command[0] == "text" and bounds.contains(command[2] + 1, command[3] + 1)
            )
        return host.export_svg()

    def assert_wash(self, host, entry, lines):
        hits = self.washes(host, entry)
        label = "terminal-c" if isinstance(host, NativeTermTextHost) else host.backend
        if isinstance(host, TermTextHost):
            if lines == 0:
                self.assertEqual(hits, [], label)
            else:
                self.assertGreaterEqual(len(hits), lines, label)
        else:
            self.assertEqual(len(hits), lines, label)

    def backend_name(self, host):
        if isinstance(host, NativeTermTextHost):
            return "terminal-c"
        if isinstance(host, NativeHost):
            return "window"
        if isinstance(host, TermTextHost):
            return "terminal"
        return "web"

    def test_text_selection_returns_with_focus_and_still_replaces(self):
        cases = (
            ("2026-10-15", {"text": "2026-10-15"}, ("2026-10-15",), 1),
            ("one\ntwo", {"text": "one\ntwo", "multiline": True}, ("one", "two"), 2),
            ("secret", {"text": "secret", "password": True}, ("••••••",), 1),
        )
        for host in self.backends():
            for source, properties, fragments, lines in cases:
                with self.subTest(backend=self.backend_name(host), text=source):
                    app, runtime = self.app(host)
                    entry = TextBox(
                        parent=app, left=16, top=16, width=280, height=96, **properties,
                    )
                    entry.select_all()
                    arrange(app, host)
                    self.assertEqual(entry.selection_range, (0, len(source)))
                    self.paint(app, host, runtime)
                    self.assert_wash(host, entry, 0)
                    shown = self.visible(host, entry)
                    for fragment in fragments:
                        self.assertIn(fragment, shown)
                    if properties.get("password"):
                        self.assertNotIn(source, shown)
                    runtime.router.set_focus(entry)
                    self.paint(app, host, runtime)
                    self.assert_wash(host, entry, lines)
                    self.assertEqual(entry.selection_range, (0, len(source)))
                    runtime.router.set_focus(None)
                    self.paint(app, host, runtime)
                    self.assert_wash(host, entry, 0)
                    self.assertEqual(entry.selection_range, (0, len(source)))
                    runtime.router.set_focus(entry)
                    runtime.router.process(Input("text", text="Q"))
                    self.assertEqual(entry.text, "Q")
                    self.assertEqual(entry.selection_range, (1, 1))
                    self.paint(app, host, runtime)
                    self.assert_wash(host, entry, 0)

    def test_calendar_keeps_select_all_without_an_unfocused_wash(self):
        for host in self.backends():
            with self.subTest(backend=self.backend_name(host)):
                app, runtime = self.app(host)
                editor = CalendarEditor(
                    "2026-10-15", parent=app, left=12, top=12, width=348, height=280,
                )
                arrange(app, host)
                entry = editor.entry
                day = next(
                    button for button in editor.days.children if button.tooltip == "2026-10-15"
                )
                self.assertEqual(entry.selection_range, (0, 10))
                self.assertTrue(day.selected)
                self.assertEqual(
                    resolve_style(day, "row", selected=True).fill,
                    app.theme.tokens.selection,
                )
                self.paint(app, host, runtime)
                self.assert_wash(host, entry, 0)
                self.assertIn("2026-10-15", self.visible(host, entry))
                runtime.router.set_focus(entry)
                self.paint(app, host, runtime)
                self.assert_wash(host, entry, 1)
                runtime.router.set_focus(None)
                self.paint(app, host, runtime)
                self.assert_wash(host, entry, 0)
                self.assertEqual(entry.selection_range, (0, 10))
                self.assertEqual(editor.displayed_month, (2026, 10))
                runtime.router.set_focus(entry)
                runtime.router.process(Input("text", text="9"))
                self.assertEqual(entry.text, "9")
                self.assertEqual(entry.selection_range, (1, 1))
