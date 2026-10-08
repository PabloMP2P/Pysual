"""Regression checks for control editing, focus, menus, and scrolling."""

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase

from pysual import (
    App, Button, ComboBox, Container, DataGrid, Dropdown, GridColumn, GridRow, ListView,
    MenuItem, NumericInput, RadioButton, ScrollArea, SplitPane, TabControl, TabPage, TextBox,
    TreeNode, TreeView,
)
from pysual.editors import _ValuePopup
from pysual.graphemes import is_boundary
from pysual.host import Input
from pysual.layout import arrange
from pysual.menus import _MenuRows
from pysual.painting import paint_tree
from pysual.runtime import Runtime
from pysual.selection import _Choices
from test_library import RecordingHost


class RadioNavigationTests(AsyncUIOwnerTestCase):
    async def test_arrows_skip_unavailable_peers_and_wrap_with_focus_and_selection_together(self):
        app = App(layout="stack", reduce_motion=True)
        first = RadioButton(parent=app, text="First", checked=True)
        middle = RadioButton(parent=app, text="Middle", focusable=False)
        hidden = RadioButton(parent=app, text="Hidden", visible=False)
        disabled = RadioButton(parent=app, text="Disabled", enabled=False)
        last = RadioButton(parent=app, text="Last")
        independent = RadioButton(parent=app, text="Other", group="other", checked=True)
        try:
            app.run(backend=RecordingHost())
            first.focus()
            for key, target in (("ArrowRight", last), ("ArrowRight", first),
                                ("ArrowLeft", last), ("ArrowLeft", first)):
                app._runtime.router.process(Input("key_down", key=key))
                self.assertIs(app._runtime.router.focus, target)
                self.assertEqual([c for c in (first, middle, hidden, disabled, last)
                                  if c.checked], [target])
                self.assertTrue(independent.checked)
            middle.focusable = True
            app._runtime.router.process(Input("key_down", key="ArrowRight"))
            self.assertIs(app._runtime.router.focus, middle)
            self.assertTrue(middle.checked)
        finally:
            await app.destroy_async()


class ControlInteractionTests(UIOwnerTestCase):
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

    def test_subwindow_drag_keeps_a_grabbable_corner_inside_parent_content(self):
        self.host.size = (400, 300)
        for padding, nested in ((0, False), (24, False), (24, True)):
            with self.subTest(padding=padding, nested=nested):
                self.app.padding = padding
                parent = (self.app.sub_window(title="Parent", width=320, height=220,
                                             padding=12) if nested else self.app)
                dialog = parent.sub_window(title="Drag", left=10, top=10,
                                           width=180, height=100)
                self.paint()
                bounds = dialog.bounds
                self.runtime.router.process(Input("pointer_down", bounds.x + 40,
                                                  bounds.y + 10, button=1))
                self.runtime.router.process(Input("pointer_move", 1000, 1000, button=1))
                self.runtime.router.process(Input("pointer_up", 1000, 1000, button=1))
                self.paint()
                content = parent.content_bounds(parent.bounds)
                visible = dialog.bounds.intersect(content)
                self.assertEqual((visible.width, visible.height), (32, 24))
                self.assertEqual(dialog._clip, visible)
                dialog.destroy()
                if nested:
                    parent.destroy()

    def test_numeric_popup_seeds_rounded_selected_text(self):
        owner = NumericInput(parent=self.app, value=0.1 + 0.2, decimals=2)
        popup = _ValuePopup(owner)
        self.addCleanup(popup.destroy)
        self.assertEqual(popup.entry.text, "0.30")
        self.assertEqual(popup.entry.selection_range, (0, 4))

    def test_container_batch_validates_once_before_committing_any_sibling(self):
        first = Button(parent=self.app, text="Before")
        seen = []

        class GuardedButton(Button):
            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if name == "text":
                    seen.append(first.text)
                    if first.text != "Before":
                        raise ValueError("First sibling changed during preflight")

        second = GuardedButton(parent=self.app, text="Initial")
        seen.clear()
        self.app.update_children({first: {"text": "After"}, second: {"text": "Ready"}})
        self.assertEqual(seen, ["Before"])
        self.assertEqual((first.text, second.text), ("After", "Ready"))

    def test_cancelled_choice_gesture_cannot_commit_on_unmatched_release(self):
        for cls in (Dropdown, ComboBox):
            for kind in ("pointer_cancel", "blur"):
                with self.subTest(control=cls.__name__, kind=kind):
                    owner = cls(parent=self.app, items=("First", "Second"))
                    choices = _Choices(parent=self.app, items=owner.items,
                                       width=200, height=100)
                    choices._commit = owner._choose
                    self.paint()
                    x, y = choices.bounds.x + 20, choices.bounds.y + choices._row_height() + 5
                    choices.handle_input(Input("pointer_down", x, y))
                    choices.handle_input(Input(kind))
                    choices.handle_input(Input("pointer_up", x, y))
                    self.assertEqual(owner.selected_index, -1)
                    choices.handle_input(Input("pointer_down", x, y))
                    choices.handle_input(Input("pointer_up", x, y))
                    self.assertEqual(owner.selected_index, 1)
                    choices.destroy()
                    owner.destroy()

    def test_public_selections_snap_both_ends_to_grapheme_boundaries(self):
        for cluster in ("e\u0301", "\U0001f469\U0001f3fd\u200d\U0001f4bb", "\U0001f1ea\U0001f1f8"):
            entry = TextBox(parent=self.app, text=cluster)
            for offset in range(len(cluster) + 1):
                entry.select(offset, offset)
                self.assertTrue(is_boundary(entry.text, entry._caret))
                self.assertTrue(is_boundary(entry.text, entry._anchor))
                entry.restore_view_state((offset, offset, 0, 0.0))
                self.assertTrue(is_boundary(entry.text, entry._caret))
                self.assertTrue(is_boundary(entry.text, entry._anchor))
            entry.destroy()

    def test_word_deletion_uses_navigation_spans_and_one_undo(self):
        document = "cafe\u0301_name  \U0001f469\U0001f3fd\u200d\U0001f4bb!  next"
        for key, position, direction in (("Backspace", len(document), -1), ("Delete", 0, 1)):
            entry = TextBox(parent=self.app, text=document)
            entry.select(position, position)
            boundary = entry._word_boundary(position, direction)
            low, high = sorted((position, boundary))
            entry.handle_input(Input("key_down", key=key, ctrl=True))
            self.assertEqual(entry.text, document[:low] + document[high:])
            self.assertTrue(is_boundary(entry.text, entry._caret))
            self.assertEqual(len(entry._undo), 1)
            entry.undo()
            self.assertEqual(entry.text, document)
            entry.select(0, 2)
            entry.handle_input(Input("key_down", key=key, ctrl=True))
            self.assertEqual(entry.text, document[2:])
            entry.undo()
            entry.read_only = True
            entry.handle_input(Input("key_down", key=key, ctrl=True))
            self.assertEqual(entry.text, document)
            entry.destroy()

    def test_submenu_shortcut_reserves_the_chevron_column(self):
        rows = _MenuRows(parent=self.app, items=("Nested",), width=280, height=40)
        item = MenuItem("nested", "Nested", children=(MenuItem("leaf", "Leaf"),))
        # Built-in MenuItem currently rejects this combination. Exercise the
        # defensive painter layout independently of that constructor policy.
        object.__setattr__(item, "shortcut", "Ctrl+K")
        rows._entries = (item,)
        calls = []
        original = self.host.text

        def text(value, *args, **kwargs):
            calls.append((value, args))
            original(value, *args, **kwargs)

        self.host.text = text
        self.paint()
        _, args = next(call for call in calls if call[0] == "Ctrl+K")
        self.assertLessEqual(args[0] + self.host.measure("Ctrl+K", 12)[0],
                             rows.bounds.right - 30)

    def test_focus_reveals_existing_list_tree_and_grid_selection(self):
        controls = (
            ListView(parent=self.app, items=tuple(str(i) for i in range(100)),
                     selected_index=50, width=260, height=120),
            TreeView(parent=self.app, nodes=tuple(TreeNode(str(i), str(i)) for i in range(100)),
                     selected_key="50", width=260, height=120),
            DataGrid(parent=self.app, columns=(GridColumn("n", "Number"),),
                     rows=tuple(GridRow(str(i), (str(i),)) for i in range(100)),
                     selected_key="50", width=260, height=120),
        )
        self.paint()
        for control in controls:
            control._scroll = 0
            revision = control._paint_revision
            self.runtime.router.set_focus(control)
            self.assertGreater(control._scroll, 0)
            self.assertGreater(control._paint_revision, revision)

    def test_tab_header_wheel_consumes_overflow_and_leaves_body_wheel_alone(self):
        tabs = TabControl(parent=self.app, width=240, height=180)
        for index in range(8):
            TabPage(parent=tabs, title=str(index))
        self.paint()
        self.assertTrue(tabs.scroll_input(Input("wheel", tabs.bounds.x + 10,
                                                tabs.bounds.y + 10, delta=1)))
        self.assertEqual(tabs._scroll, 1)
        self.assertFalse(tabs.scroll_input(Input("wheel", tabs.bounds.x + 10,
                                                 tabs.bounds.y + 100, delta=1)))
        self.assertEqual(tabs._scroll, 1)
        tabs.scroll_input(Input("wheel", tabs.bounds.x + 10, tabs.bounds.y + 10, delta=-1))
        self.assertEqual(tabs._scroll, 0)
        tabs.width = 2000
        self.paint()
        self.assertFalse(tabs.scroll_input(Input("wheel", tabs.bounds.x + 10,
                                                 tabs.bounds.y + 10, delta=1)))

    def test_tab_header_boundaries_pass_wheel_to_parent(self):
        area = ScrollArea(parent=self.app, width=400, height=150)
        tabs = TabControl(parent=area, width=240, height=500)
        for index in range(8):
            TabPage(parent=tabs, title=str(index))
        self.paint()
        for _ in range(7):
            self.runtime.router.process(Input("wheel", 10, 10, delta=1))
        self.assertEqual(tabs._scroll, 7)
        self.assertEqual(area.scroll_y, 0)
        self.runtime.router.process(Input("wheel", 10, 10, delta=1))
        self.assertGreater(area.scroll_y, 0)
        # Keep the strip visible with outer scrolling available in both directions.
        tabs.top = 100
        area.scroll_y = 100
        self.paint()
        for _ in range(7):
            self.runtime.router.process(Input("wheel", 10, 10, delta=-1))
        self.assertEqual(tabs._scroll, 0)
        self.assertEqual(area.scroll_y, 100)
        self.runtime.router.process(Input("wheel", 10, 10, delta=-1))
        self.assertLess(area.scroll_y, 100)

    def test_split_user_input_uses_visible_ratio_with_minimum_sizes(self):
        for orientation in ("horizontal", "vertical"):
            with self.subTest(orientation=orientation):
                pane = SplitPane(parent=self.app, width=400, height=400,
                                 orientation=orientation)
                first = Container(parent=pane, min_width=160, min_height=160)
                second = Container(parent=pane, min_width=160, min_height=160)
                self.paint()
                horizontal = orientation == "horizontal"
                extent = lambda: first.bounds.width if horizontal else first.bounds.height
                arrow = "ArrowRight" if horizontal else "ArrowDown"
                divider = pane._divider
                pane.handle_input(Input("pointer_down", divider.x + 2, divider.y + 2))
                pane.handle_input(Input("pointer_move", 0, 0))
                pane.handle_input(Input("pointer_up", 0, 0))
                self.paint()
                self.assertAlmostEqual(pane.position, 160 / 394)
                self.assertAlmostEqual(extent(), 160)
                pane.handle_input(Input("key_down", key=arrow))
                self.paint()
                self.assertGreater(extent(), 160)
                for key, expected in (("End", 234), ("Home", 160)):
                    pane.handle_input(Input("key_down", key=key))
                    self.paint()
                    self.assertAlmostEqual(extent(), expected)
                    self.assertAlmostEqual(pane.position, expected / 394)
                # Applications may retain a requested ratio across size constraints.
                pane.position = 0
                self.paint()
                self.assertEqual(pane.position, 0)
                self.assertAlmostEqual(extent(), 160)
                pane.handle_input(Input("key_down", key=arrow))
                self.paint()
                self.assertGreater(extent(), 160)
                first.min_width = first.min_height = 0
                second.min_width = second.min_height = 0
                before = extent()
                self.paint()
                self.assertAlmostEqual(extent(), before)
                pane.destroy()

    def test_shared_scrollbars_page_drag_and_cancel_for_every_viewport(self):
        area = ScrollArea(parent=self.app, width=200, height=120)
        Button(parent=area, top=3000, width=100)
        controls = (
            area,
            ListView(parent=self.app, items=tuple(str(i) for i in range(100)), width=200, height=120),
            TreeView(parent=self.app, nodes=tuple(TreeNode(str(i), str(i)) for i in range(100)), width=200, height=120),
            DataGrid(parent=self.app, columns=(GridColumn("n", "N"),),
                     rows=tuple(GridRow(str(i), (str(i),)) for i in range(100)), width=200, height=120),
            TextBox(parent=self.app, text="line\n" * 100, multiline=True, width=200, height=120),
        )
        self.paint()
        for control in controls:
            with self.subTest(control=type(control).__name__):
                bar = next(bar for bar in control._scrollbars() if bar.axis == 1)
                self.assertEqual(bar.track.width, 12)
                control.handle_input(Input("pointer_down", bar.track.x + 3,
                                           bar.track.bottom - 2, pointer_id=7))
                offset = control.scroll_y if control is area else control._scroll
                self.assertEqual(offset, bar.viewport)
                bar = next(bar for bar in control._scrollbars() if bar.axis == 1)
                control.handle_input(Input("pointer_down", bar.thumb.x + 3,
                                           bar.thumb.y + 3, pointer_id=7))
                control.handle_input(Input("pointer_move", bar.thumb.x + 3,
                                           bar.track.bottom + 200, pointer_id=8))
                self.assertEqual(control.scroll_y if control is area else control._scroll, offset)
                control.handle_input(Input("pointer_move", bar.thumb.x + 3,
                                           bar.track.bottom + 200, pointer_id=7))
                self.assertEqual(control.scroll_y if control is area else control._scroll,
                                 bar.total - bar.viewport)
                control.handle_input(Input("pointer_cancel", pointer_id=7))
                control.handle_input(Input("pointer_move", bar.thumb.x + 3,
                                           bar.track.y, pointer_id=7))
                self.assertEqual(control.scroll_y if control is area else control._scroll,
                                 bar.total - bar.viewport)

    def test_multiline_scrollbars_clamp_after_edits_and_resize_without_changing_selection(self):
        entry = TextBox(parent=self.app, text=("x" * 120 + "\n") * 100,
                        multiline=True, monospace=True, width=220, height=120)
        combo = ComboBox(parent=self.app, text="x" * 1000, width=200, height=38)
        short = TextBox(parent=self.app, text="small", multiline=True, width=200, height=120)
        self.paint()
        self.assertEqual({bar.axis for bar in entry._scrollbars()}, {0, 1})
        self.assertEqual(combo._scrollbars(), [])
        self.assertEqual(short._scrollbars(), [])
        selection = entry.selection_range
        entry.handle_input(Input("wheel", delta=20, shift=True))
        entry.handle_input(Input("wheel", delta=20))
        self.assertEqual(entry.selection_range, selection)
        entry.text = "tiny"
        self.paint()
        self.assertEqual((entry._scroll, entry._scroll_x), (0, 0))
        self.assertEqual(entry._scrollbars(), [])
        entry.text = "long\n" * 50
        entry.select(0, 0)
        self.runtime.router.set_focus(entry)
        self.paint()
        self.assertEqual(entry._scroll, 0)
        entry.height = 2000
        self.paint()
        self.assertEqual(entry._scrollbars(), [])
