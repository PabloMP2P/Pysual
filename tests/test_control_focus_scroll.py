"""Regression checks for focus and scrolling during pointer input and resize."""

from _ui_testcase import UIOwnerTestCase
from pysual import App, DataGrid, GridColumn, GridRow, ListView, TextBox, TreeNode, TreeView
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.runtime import Runtime
from test_library import RecordingHost


class ControlFocusScrollTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=600, height=400)
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.addCleanup(self.app.destroy)
        self.addCleanup(setattr, self.app, "_runtime", None)

    def paint(self):
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)

    def test_shrinking_multiline_height_keeps_focused_caret_visible(self):
        entry = TextBox(parent=self.app, text="line\n" * 100,
                        multiline=True, width=220, height=240)
        self.paint()
        self.runtime.router.set_focus(entry)
        entry.select(6 * 5, 6 * 5)
        self.paint()
        original_selection = entry.selection_range
        original_width = entry._text_viewport().width
        self.assertLessEqual(entry._scroll, 6)
        self.assertLess(6, entry._scroll + entry._page_rows())
        entry.height = 90
        self.paint()
        self.assertEqual(entry.selection_range, original_selection)
        self.assertEqual(entry._text_viewport().width, original_width)
        self.assertLessEqual(entry._scroll, 6)
        self.assertLess(6, entry._scroll + entry._page_rows(),
                        "The focused caret fell below the resized viewport")

    def test_mouse_focus_does_not_scroll_before_picking_the_clicked_row(self):
        factories = (
            lambda: ListView(parent=self.app, items=tuple(str(i) for i in range(100)),
                             selected_index=50, width=220, height=180),
            lambda: TreeView(parent=self.app, nodes=tuple(TreeNode(str(i), str(i)) for i in range(100)),
                             selected_key="50", width=220, height=180),
            lambda: DataGrid(parent=self.app, columns=(GridColumn("n", "N"),),
                             rows=tuple(GridRow(str(i), (str(i),)) for i in range(100)),
                             selected_key="50", width=220, height=180),
        )
        for factory in factories:
            control = factory()
            with self.subTest(control=type(control).__name__):
                self.paint()
                self.runtime.router.set_focus(None)
                control._scroll = 0
                control.invalidate()
                self.paint()
                x = control.bounds.x + 60
                header = control._header_height() if isinstance(control, DataGrid) else 0
                y = control.bounds.y + header + control._row_height() + 5
                self.runtime.router.process(Input("pointer_down", x, y))
                self.runtime.router.process(Input("pointer_up", x, y))
                chosen = control.selected_index if isinstance(control, ListView) else control.selected_key
                self.assertEqual(str(chosen), "1", "Focus moved the rows before the mouse press selected one")
            control.destroy()
