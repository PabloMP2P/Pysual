"""Tab headers reserve navigation space only when their pages overflow."""

import unittest

from pysual import App, TabControl
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from test_library import RecordingHost


class TabHeaderTests(unittest.TestCase):
    def setUp(self):
        self.app = App(width=400, height=220)
        self.tabs = TabControl(
            parent=self.app, width=280, height=150, tab_width=140
        )
        self.first = self.tabs.tab_page(title="First")
        self.second = self.tabs.tab_page(title="Second")
        self.host = RecordingHost()

    def tearDown(self):
        self.app.destroy()

    def paint(self):
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)

    def test_exactly_fitting_tabs_paint_and_select_without_scroll_buttons(self):
        self.paint()
        self.assertEqual(self.host.texts, ["First", "Second"])
        self.tabs.handle_input(Input("pointer_down", x=260, y=12))
        self.assertEqual(self.tabs.selected_index, 1)
        self.assertTrue(self.second.visible)
        self.assertFalse(self.first.visible)
        self.paint()
        self.assertEqual(self.host.texts, ["First", "Second"])

    def test_resize_to_fit_reveals_all_tabs_and_removes_scroll_buttons(self):
        self.tabs.width = 200
        self.tabs.selected_index = 1
        self.paint()
        self.assertEqual(self.host.texts, ["Second", "‹", "›"])
        self.tabs.width = 280
        self.paint()
        self.assertEqual(self.host.texts, ["First", "Second"])

    def test_one_tab_uses_its_full_width_for_the_title(self):
        self.second.destroy()
        self.tabs.width = 140
        self.first.title = "A long title"
        self.paint()
        self.assertEqual(self.host.texts, ["A long title"])
