"""Focus rings follow the theme color and the window's drawing hook."""

import unittest
from typing import ClassVar

from _ui_testcase import UIOwnerTestCase
from test_retained_paint import PatchRecordingHost

from pysual import App, Button, Control, Rect, Theme
from pysual.fields import SearchField
from pysual.layout import arrange
from pysual.painting import Painter, RetainedPaintTree, paint_tree, resolve_style
from pysual.runtime import Runtime
from pysual.schema import Dirty, prop


class RingHost:
    def __init__(self):
        self.rings = []
        self.rects = []
        self.lines = []

    def focus_ring(self, rect, color, radius=0):
        self.rings.append((rect, color, radius))
        return True

    def rect(self, *args, **kwargs):
        self.rects.append(args)

    def line(self, *args, **kwargs):
        self.lines.append(args)

    def clip(self, rect):
        pass


class FallbackHost:
    """A host without a focus stroke, so the painter uses a rectangle."""

    def __init__(self):
        self.rings = []
        self.rects = []

    def rect(self, *args, **kwargs):
        self.rects.append(args)

    def clip(self, rect):
        pass


class Mark(Control):
    style_parts: ClassVar[tuple[str, ...]] = ("mark",)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)

    def paint(self, painter, /) -> None:
        return


class Plain(Button):
    def paint_focus(self, painter, /) -> None:
        return


class Underline(App):
    def paint_focus_ring(self, painter):
        painter.line(
            4, painter.height - 3, max(4, painter.width - 4), painter.height - 3,
            "#ff00ff", 2,
        )


class FocusRingPolicyTests(unittest.TestCase):
    def painter(self, control, theme=None, host=None):
        chosen = control.effective_theme if theme is None else theme
        target = host or RingHost()
        return target, Painter(target, Rect(0, 0, 80, 32), chosen, control)

    def test_focus_color_follows_accent_until_a_token_replaces_it(self):
        button = Button(text="Go", width=80, height=32)
        try:
            theme = button.effective_theme.derive(accent="#abcdef")
            host, painter = self.painter(button, theme)
            button.paint_focus(painter)
            self.assertEqual(
                host.rings,
                [(Rect(0, 0, 80, 32), "#abcdef", resolve_style(button).radius or 0)],
            )
            self.assertEqual(host.rects, [])

            faded = theme.derive(focus="#12345678")
            host, painter = self.painter(button, faded)
            button.paint_focus(painter)
            self.assertEqual(host.rings[0][1], "#12345678")

            recolored = faded.derive(accent="#ffffff")
            host, painter = self.painter(button, recolored)
            button.paint_focus(painter)
            self.assertEqual(host.rings[0][1], "#12345678")

            host, painter = self.painter(button, recolored.derive(focus=None))
            button.paint_focus(painter)
            self.assertEqual(host.rings[0][1], "#ffffff")
        finally:
            button.destroy()

    def test_transparent_focus_skips_the_host_stroke_and_its_fallback(self):
        button = Button(text="Go", width=80, height=32)
        try:
            theme = Theme().derive(focus="#11223300")
            for host in (RingHost(), FallbackHost()):
                with self.subTest(host=type(host).__name__):
                    _, painter = self.painter(button, theme, host)
                    button.paint_focus(painter)
                    self.assertEqual(host.rings, [])
                    self.assertEqual(host.rects, [])
            host, painter = self.painter(button, theme)
            painter.standard_focus_ring("#ffffffff")
            self.assertEqual(host.rings[0][1], "#ffffffff")
            with self.assertRaises(ValueError):
                painter.standard_focus_ring("blue")
        finally:
            button.destroy()

    def test_controls_without_a_body_part_use_a_square_ring(self):
        mark = Mark(width=40, height=24)
        try:
            host, painter = self.painter(mark, Theme().derive(radius=14))
            mark.paint_focus(painter)
            self.assertEqual(host.rings[0][2], 0)
        finally:
            mark.destroy()

    def test_a_control_hook_replaces_the_window_policy(self):
        app = Underline()
        button = Plain(parent=app, text="Go")
        try:
            host, painter = self.painter(button, app.theme)
            button.paint_focus(painter)
            self.assertEqual(host.lines, [])
            self.assertEqual(host.rings, [])
        finally:
            app.destroy()


class FocusRingPaintTests(UIOwnerTestCase):
    def setUp(self):
        self.app, self.host = App(width=420, height=180), PatchRecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.retained = self.runtime._retained_paint = RetainedPaintTree(self.host)

    def tearDown(self):
        self.app._runtime = None
        self.app.destroy()

    def frame(self, *, layout=False):
        if layout:
            arrange(self.app, self.host)
        paint_tree(
            self.app, self.host, self.runtime.router.focus,
            retained=self.retained, layout_changed=layout,
        )

    def outlines(self, color):
        return [
            args[0] for kind, _, args, _ in self.host.snapshot()
            if kind == "rect" and len(args) == 5 and args[1] == ""
            and args[3] == color and args[4] == 2
        ]

    def test_theme_focus_color_repaints_a_retained_ring_without_moving_focus(self):
        button = Button(parent=self.app, text="Go", left=12, top=12, width=120, height=36)
        self.runtime.router.set_focus(button)
        self.frame(layout=True)
        accent = self.app.theme.tokens.accent
        self.assertEqual(self.outlines(accent), [button.bounds.inset(1)])
        painted = [
            args for kind, _, args, _ in self.host.snapshot()
            if kind == "rect" and len(args) == 5 and args[1] == "" and args[3] == accent
            and args[4] == 2
        ]
        self.assertEqual(painted[0][2], max(0, (resolve_style(button).radius or 0) - 1))
        self.app.theme = self.app.theme.derive(focus="#11223366")
        self.frame()
        self.assertIn(self.retained._records[button].identity, self.host.updates)
        self.assertEqual(self.outlines("#11223366"), [button.bounds.inset(1)])
        self.assertEqual(self.outlines(accent), [])
        self.app.theme = self.app.theme.derive(focus="#11223300")
        self.frame()
        self.assertEqual(self.outlines("#11223300"), [])
        self.assertEqual(self.outlines("#11223366"), [])
        retained = self.host.snapshot()
        paint_tree(self.app, self.host, self.runtime.router.focus)
        self.assertEqual(retained, tuple(self.host._drawing))

    def test_search_field_uses_the_theme_ring_once_around_the_field(self):
        field = SearchField(parent=self.app, left=12, top=12, width=320, height=40)
        other = SearchField(parent=self.app, left=12, top=70, width=320, height=40)
        self.frame(layout=True)
        accent = self.app.theme.tokens.accent
        self.assertEqual(self.outlines(accent), [])
        field.focus()
        self.frame()
        self.assertEqual(self.outlines(accent), [field.bounds.inset(1)])
        self.app.theme = self.app.theme.derive(focus="#44556699")
        self.frame()
        self.assertEqual(self.outlines("#44556699"), [field.bounds.inset(1)])
        self.assertEqual(self.outlines(accent), [])
        other.focus()
        self.frame()
        self.assertEqual(self.outlines("#44556699"), [other.bounds.inset(1)])
        self.app.theme = self.app.theme.derive(focus="#44556600")
        self.frame()
        self.assertEqual(self.outlines("#44556699"), [])
        self.assertEqual(self.outlines(accent), [])

    def test_window_hook_replaces_button_and_search_field_rings(self):
        app, host = Underline(width=420, height=180), PatchRecordingHost()
        runtime = Runtime(app, host)
        app._runtime = runtime
        retained = runtime._retained_paint = RetainedPaintTree(host)
        button = Button(parent=app, text="Go", left=12, top=12, width=120, height=36)
        field = SearchField(parent=app, left=12, top=70, width=280, height=40)
        try:
            arrange(app, host)
            for control, focus in ((button, button), (field, field.entry)):
                with self.subTest(control=type(control).__name__):
                    runtime.router.set_focus(focus)
                    paint_tree(app, host, runtime.router.focus, retained=retained)
                    lines = [
                        command for command in host.snapshot()
                        if command[0] == "line" and command[2][4] == "#ff00ff"
                    ]
                    self.assertEqual(len(lines), 1)
                    outlines = [
                        args for kind, _, args, _ in host.snapshot()
                        if kind == "rect" and len(args) == 5 and args[1] == ""
                        and args[4] == 2
                    ]
                    self.assertEqual(outlines, [])
                    retained_drawing = host.snapshot()
                    paint_tree(app, host, runtime.router.focus)
                    self.assertEqual(retained_drawing, tuple(host._drawing))
        finally:
            app._runtime = None
            app.destroy()
