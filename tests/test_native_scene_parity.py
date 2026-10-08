"""Incremental command footprints match complete control painting in pixels."""

import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

from _ui_testcase import UIOwnerTestCase
from test_native_window import bmp_pixels, png_data

from pysual import App, Button, CheckBox, Container, Control, Dropdown, Label, Rect, Slider
from pysual import Style, get_theme, theme_names
from pysual import ScrollArea, SplitPane, TabControl, TabPage
from pysual.backends.native import NativeHost
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.runtime import Runtime


ROOT = Path(__file__).resolve().parents[1]
HOST = Path(os.environ.get("PYSUAL_HOST") or ROOT / "src/pysual/bin" /
            ("pysual-host.exe" if sys.platform == "win32" else "pysual-host"))


class Primitives(Control):
    def paint(self, painter, /):
        painter.rect(Rect(0, 0, 130, 65), "#26384faa", radius=7)
        painter.text("café λ", 5, 2, color="#ffffff", size=17)
        painter.icon("sparkles", 5, 28, size=24, color="#00ffcc")
        painter.lines(((40, 50), (65, 24), (88, 54)), "#ff990080", 3)
        painter.caret(94, 20, 20, "#ffffff")
        source = png_data(2, 1, bytes((255, 0, 0, 255, 0, 0, 255, 255)))
        painter.image(source, Rect(108, 10, 18, 36), fit="contain")


@unittest.skipUnless(HOST.is_file(), "Build the C host first")
class NativeSceneParityTests(UIOwnerTestCase):
    def setUp(self):
        self.directory = ROOT / "work" / "scene-parity" / uuid4().hex
        self.directory.mkdir(parents=True)
        self.output = self.directory / "frame.bmp"
        self.host = NativeHost(hidden=True, vsync=False)
        self.host.configure_rendering(redraw_interval=None, fps_limit=None, reduce_motion=True)
        self.host.open("retained pixel parity", 420, 240, False, 1)
        self.app = App(width=420, height=240, layout="absolute", reduce_motion=True)
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.runtime._retained_paint = RetainedPaintTree(self.host)
        self.panel = Container(parent=self.app, left=10, top=10, width=235, height=180,
                               background="#253348", padding=8)
        self.wrapper = Container(parent=self.panel, width=205, height=145)
        self.button = Button(parent=self.wrapper, text="Search", icon="search",
                             left=5, top=4, width=140, height=40)
        self.check = CheckBox(parent=self.wrapper, text="Checked", checked=True,
                              left=8, top=55, width=140, height=30)
        self.slider = Slider(parent=self.wrapper, left=5, top=105, width=150, height=30)
        self.label = Label(parent=self.app, text="clipped λ", left=230, top=20,
                           width=80, height=24)
        Primitives(parent=self.app, left=250, top=95, width=135, height=75)
        arrange(self.app, self.host)

    def tearDown(self):
        self.app._runtime = None
        self.app.destroy()
        self.host.close()
        self.output.unlink(missing_ok=True)
        self.directory.rmdir()

    def capture(self):
        self.host.capture(self.output)
        return bmp_pixels(self.output)

    def assert_pixels_match_complete_scene(self):
        paint_tree(self.app, self.host, self.runtime.router.focus,
                   retained=self.runtime._retained_paint)
        incremental = self.capture()
        paint_tree(self.app, self.host, self.runtime.router.focus)
        self.assertTrue(self.capture() == incremental, "Retained pixels differ from complete frame")
        # The oracle intentionally replaces the native scene. Restore it before
        # returning so the next edit exercises local damage and replay.
        self.runtime._retained_paint.invalidate_all()
        paint_tree(self.app, self.host, self.runtime.router.focus,
                   retained=self.runtime._retained_paint)

    def assert_theme_effects_focus_primitives_and_local_edits_match_complete_scene(self, name):
        self.app.theme = get_theme(name).styled(
            Button, Style(shadow="#ff000060", shadow_blur=7, shadow_x=-2,
                          shadow_y=3), state="hover")
        arrange(self.app, self.host)
        self.assert_pixels_match_complete_scene()
        self.runtime.router._set_hover(self.button)
        self.runtime.router.set_focus(self.button, reveal=False)
        self.assert_pixels_match_complete_scene()
        self.check.checked = not self.check.checked
        self.slider.value = 73 if self.slider.value != 73 else 24
        self.assert_pixels_match_complete_scene()
        self.runtime.router._set_hover(None)
        self.runtime.router.set_focus(None, reveal=False)

    def test_transparent_container_hover_keeps_child_pixels_without_replay(self):
        self.assert_pixels_match_complete_scene()
        paint_tree(self.app, self.host, retained=self.runtime._retained_paint)
        before = self.capture()
        self.host.native_stats(reset=True)
        self.runtime.router._set_hover(self.wrapper)
        paint_tree(self.app, self.host, retained=self.runtime._retained_paint)
        stats = self.host.native_stats()
        self.assertEqual(stats["last_scene_commands_replayed"], 0)
        self.assertEqual(self.capture(), before)

    def test_neon_dropdown_open_close_matches_complete_scene_after_cached_subtree_reuse(self):
        self.app.theme = get_theme("neon")
        dropdown = Dropdown(parent=self.app, left=265, top=12, width=140, height=34,
                            items=("One", "Two", "Three", "Four", "Five"))
        arrange(self.app, self.host)
        self.assert_pixels_match_complete_scene()
        self.app._state = "RUNNING"
        try:
            for cycle in range(3):
                with self.subTest(cycle=cycle):
                    dropdown.open()
                    popup = self.runtime.popup
                    arrange(self.app, self.host)
                    popup.reposition()
                    arrange(self.app, self.host)
                    self.assert_pixels_match_complete_scene()
                    popup.dismiss()
                    arrange(self.app, self.host)
                    self.assert_pixels_match_complete_scene()
        finally:
            if self.runtime.popup is not None:
                self.runtime.popup.dismiss()
            self.app._state = "CREATED"

    def test_child_dependent_headers_scrollbars_and_divider_match_complete_scene(self):
        for child in self.app.children:
            child.destroy()
        tabs = TabControl(parent=self.app, width=400, height=100)
        TabPage(parent=tabs, title="First")
        page = TabPage(parent=tabs, title="Before")
        scroll = ScrollArea(parent=self.app, top=120, width=160, height=90)
        child = Button(parent=scroll, text="Child", width=100, height=35)
        pane = SplitPane(parent=self.app, left=190, top=120, width=200, height=90)
        first, second = Container(parent=pane), Container(parent=pane)
        arrange(self.app, self.host)
        self.assert_pixels_match_complete_scene()
        for control, field, value in (
            (page, "title", "Renamed"), (page, "icon", "search"),
            (page, "enabled", False), (child, "height", 180),
            (child, "height", 240), (child, "height", 35),
            (first, "min_width", 130), (second, "visible", False),
            (second, "visible", True),
        ):
            with self.subTest(control=type(control).__name__, field=field, value=value):
                setattr(control, field, value)
                arrange(self.app, self.host)
                self.assert_pixels_match_complete_scene()

    def test_custom_inherited_enabled_policy_matches_complete_scene(self):
        class Policy(Container):
            def _initialize(self):
                super()._initialize()
                self._allowed = True

            @property
            def effective_enabled(self):
                return self._allowed and super().effective_enabled

        parent = Policy(parent=self.app, left=250, top=185, width=160, height=50)
        Button(parent=parent, text="Policy", width=150, height=35)
        arrange(self.app, self.host)
        self.assert_pixels_match_complete_scene()
        for allowed in (False, True):
            with self.subTest(allowed=allowed):
                parent._allowed = allowed
                parent.invalidate()
                self.assert_pixels_match_complete_scene()


def theme_parity_test(name):
    def test(self):
        self.assert_theme_effects_focus_primitives_and_local_edits_match_complete_scene(name)

    test.__name__ = f"test_theme_{name}_effects_focus_primitives_and_local_edits"
    return test


# Each theme gets its own native fixture and owner dispatch. A single loop can
# exceed the production dispatch deadline even when every pixel oracle passes.
for theme_name in theme_names():
    method = theme_parity_test(theme_name)
    setattr(NativeSceneParityTests, method.__name__, method)


if __name__ == "__main__":
    unittest.main()
