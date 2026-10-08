"""Regression checks for batched control and collection updates."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import (
    Button,
    CheckBox,
    ColorPicker,
    Container,
    Control,
    DataGrid,
    Dropdown,
    GridColumn,
    GridRow,
    MenuBar,
    NumericInput,
    ProgressBar,
    Slider,
    TextBox,
    TreeNode,
    TreeView,
    get_theme,
    light,
)
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, resolve_style
from pysual.schema import Dirty


class ControlUpdateTests(UIOwnerTestCase):
    def keep(self, control):
        self.addCleanup(control.destroy)
        return control

    def test_batch_flush_preserves_origins_without_repeating_local_work(self):
        class CustomParent(Container):
            def paint(self, painter, /):
                painter.body()

        root = self.keep(CustomParent())
        parent = Container(parent=root)
        children = [Button(parent=parent, text="Before") for _ in range(3)]
        retained = RetainedPaintTree(None)
        router = Mock()
        root._runtime = SimpleNamespace(router=router, _retained_paint=retained)
        self.addCleanup(delattr, root, "_runtime")
        root._wake = Mock()
        before = [c._paint_local_revision for c in (root, parent, *children)]
        revisions = [c._paint_revision for c in (root, parent, *children)]
        with patch.object(retained, "invalidate", wraps=retained.invalidate) as invalidated:
            parent.update_children({c: {"text": "After"} for c in children})
        self.assertEqual(router.invalidate.call_count, len(children))
        self.assertEqual(invalidated.call_count, len(children))
        self.assertEqual(retained._dirty, {root, *children})
        self.assertEqual(
            [c._paint_local_revision - old for c, old in zip((root, parent, *children), before)],
            [0, 0, 1, 1, 1],
        )
        self.assertEqual(
            [c._paint_revision - old for c, old in zip((root, parent, *children), revisions)],
            [1, 1, 1, 1, 1],
        )
        root._wake.assert_called_once_with()

    def test_nested_update_keeps_notifications_without_repeating_local_work(self):
        notifications = []

        class NestedButton(Button):
            def _changed(self, field, old, value):
                if field.name == "text":
                    self.update(foreground="#123456")
                notifications.append(field.name)
                super()._changed(field, old, value)

        root = self.keep(Container())
        child = NestedButton(parent=root, text="Before")
        notifications.clear()
        local, subtree = child._paint_local_revision, root._paint_revision
        child.update(text="After")
        self.assertEqual(notifications, ["foreground", "text"])
        self.assertEqual(child._paint_local_revision, local + 2)
        self.assertEqual(root._paint_revision, subtree + 1)
        self.assertEqual((child.text, child.foreground), ("After", "#123456"))

    def test_update_validates_cross_fields_and_invalidates_ancestors_once(self):
        root = self.keep(Container())
        parent = Container(parent=root)
        button = Button(parent=parent, text="Before", min_width=10, max_width=20)
        revisions = tuple(item._paint_revision for item in (root, parent, button))
        button.update(min_width=30, max_width=40, text="After", background="#123456")
        self.assertEqual(
            (button.min_width, button.max_width, button.text), (30, 40, "After")
        )
        self.assertEqual(
            tuple(item._paint_revision for item in (root, parent, button)),
            tuple(value + 1 for value in revisions),
        )
        unchanged = tuple(item._paint_revision for item in (root, parent, button))
        button.update(text="After")
        button.update()
        self.assertEqual(
            tuple(item._paint_revision for item in (root, parent, button)), unchanged
        )

    def test_update_rejection_keeps_values_and_revisions_for_all_fields(self):
        control = self.keep(Button(text="Original", min_width=10, max_width=20))
        before = dict(control._values), control._paint_revision, control.dirty
        for values in (
            {"text": "Changed", "background": "not a color"},
            {"text": "Changed", "min_width": 30},
            {"text": "Changed", "widht": 80},
        ):
            with (
                self.subTest(values=values),
                self.assertRaises((TypeError, ValueError)),
            ):
                control.update(**values)
            self.assertEqual(
                (dict(control._values), control._paint_revision, control.dirty), before
            )

    def test_container_batch_preflights_every_child_and_dirties_parent_once(self):
        root = self.keep(Container())
        controls = [Button(parent=root, text="Before") for _ in range(500)]
        before = root._paint_revision
        root.update_children(
            {
                control: {"text": "After", "background": "#123456"}
                for control in controls
            }
        )
        self.assertEqual(root._paint_revision, before + 1)
        self.assertTrue(all(control.text == "After" for control in controls))
        previous = root._paint_revision
        with self.assertRaises((TypeError, ValueError)):
            root.update_children(
                {
                    controls[0]: {"text": "Must not change"},
                    controls[-1]: {"background": "bad color"},
                }
            )
        self.assertEqual(controls[0].text, "After")
        self.assertEqual(root._paint_revision, previous)
        outside = self.keep(Button())
        with self.assertRaisesRegex(ValueError, "descendants"):
            root.update_children(
                {controls[0]: {"text": "Must not change"}, outside: {"text": "Outside"}}
            )
        self.assertEqual(controls[0].text, "After")

    def test_changed_hooks_see_all_updated_fields(self):
        observed = []

        class SnapshotButton(Button):
            def _changed(self, field, old, value):
                observed.append(
                    (self.text, self.background, self.min_width, self.max_width)
                )
                super()._changed(field, old, value)

        control = self.keep(SnapshotButton())
        observed.clear()
        control.update(text="Changed", background="#123456", min_width=10, max_width=20)
        self.assertTrue(observed)
        self.assertTrue(
            all(snapshot == ("Changed", "#123456", 10, 20) for snapshot in observed)
        )

    def test_update_subclass_hook_observes_complete_candidate_without_live_writes(self):
        observations = []
        live = None

        class WidthLimited(Control):
            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if live is not None:
                    observations.append(
                        (live.width, live.height, self.width, self.height)
                    )
                if (
                    self.width is not None
                    and self.height is not None
                    and self.width > self.height
                ):
                    raise ValueError("width exceeds height")

        live = self.keep(WidthLimited(width=10, height=20))
        live.update(width=30, height=40)
        self.assertTrue(observations)
        self.assertTrue(all(item == (10, 20, 30, 40) for item in observations))
        with self.assertRaisesRegex(ValueError, "width exceeds"):
            live.update(width=100, height=50)
        self.assertEqual((live.width, live.height), (30, 40))

    def test_grid_subclass_rejects_every_mutation_route_without_partial_state(self):
        class NonnegativeGrid(DataGrid):
            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if name == "rows" and any(row.cells[0] < 0 for row in value):
                    raise ValueError("negative row")

        grid = self.keep(
            NonnegativeGrid(
                columns=(GridColumn("number", "Number", kind="number"),),
                rows=(GridRow("one", (1,)), GridRow("two", (2,))),
                selected_key="one",
                sort_key="number",
            )
        )
        invalid = (GridRow("one", (-1,)), GridRow("two", (2,)))
        original = (
            grid.columns,
            grid.rows,
            grid.selected_key,
            grid.selected_column,
            grid.sort_key,
            dict(grid._by_key),
            list(grid._order),
            dict(grid._positions),
        )
        routes = (
            lambda: setattr(grid, "rows", invalid),
            lambda: grid.set_cell("one", "number", -1),
            lambda: grid.set_data(
                columns=(GridColumn("new", "New", kind="number"),), rows=invalid
            ),
            lambda: grid.update_rows((invalid[0],), remove=("two",)),
            lambda: grid.update(rows=invalid),
        )
        for index, mutate in enumerate(routes):
            with (
                self.subTest(route=index),
                self.assertRaisesRegex(ValueError, "negative row"),
            ):
                mutate()
            self.assertEqual(
                (
                    grid.columns,
                    grid.rows,
                    grid.selected_key,
                    grid.selected_column,
                    grid.sort_key,
                    grid._by_key,
                    grid._order,
                    grid._positions,
                ),
                original,
            )

    def test_grid_generic_update_checks_row_shape_and_cell_types(self):
        grid = self.keep(
            DataGrid(
                columns=(GridColumn("number", "Number", kind="number"),),
                rows=(GridRow("one", (1,)),),
                selected_key="one",
            )
        )
        original = grid.rows
        for invalid in ((GridRow("one", ()),), (GridRow("one", ("not numeric",)),)):
            with self.subTest(rows=invalid), self.assertRaises((TypeError, ValueError)):
                grid.update(rows=invalid)
            self.assertIs(grid.rows, original)
        grid.update(
            columns=(GridColumn("word", "Word"),), rows=(GridRow("one", ("valid",)),)
        )
        self.assertEqual(grid.selected_row.cells, ("valid",))

    def test_grid_sort_validation_and_notifications_observe_complete_state(self):
        snapshots = []

        class GuardedGrid(DataGrid):
            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if self.sort_key == "number" and self.sort_descending:
                    raise ValueError("descending number disabled")

            def _changed(self, field, old, value):
                super()._changed(field, old, value)
                snapshots.append((self.sort_key, self.sort_descending,
                                  tuple(row.key for row in self._order)))

        grid = self.keep(GuardedGrid(
            columns=(GridColumn("name", "Name"), GridColumn("number", "Number", kind="number")),
            rows=(GridRow("a", ("A", 2)), GridRow("b", ("B", 1))),
            sort_key="name", sort_descending=True,
        ))
        original = grid.sort_key, grid.sort_descending, grid._order
        with self.assertRaisesRegex(ValueError, "descending number disabled"):
            grid.update(sort_key="number", sort_descending=True)
        self.assertEqual((grid.sort_key, grid.sort_descending, grid._order), original)
        self.assertEqual(snapshots, [])
        grid.update(sort_key="number", sort_descending=False)
        self.assertEqual(snapshots, [("number", False, ("b", "a"))] * 2)

    def test_tree_replacement_retains_surviving_keys_and_accepts_new_selection(self):
        tree = self.keep(
            TreeView(
                nodes=(TreeNode("root", "Root", (TreeNode("one", "One"),)),),
                selected_key="one",
                expanded_keys=("root",),
            )
        )
        replacement = (
            TreeNode(
                "root",
                "Updated",
                (TreeNode("one", "Updated one"), TreeNode("two", "Two")),
            ),
        )
        tree.set_tree(replacement)
        self.assertIs(tree.nodes, replacement)
        self.assertEqual(tree.selected_key, "one")
        self.assertEqual(tree.expanded_keys, ("root",))
        new = (TreeNode("new_root", "New", (TreeNode("new_child", "Child"),)),)
        tree.set_tree(new, selected_key="new_child", expanded_keys=())
        self.assertEqual(tree.selected_node.text, "Child")
        self.assertIn("new_root", tree.expanded_keys)
        self.assertIn("new_child", tree._positions)
        tree.set_tree(())
        self.assertIsNone(tree.selected_key)
        self.assertEqual(tree.expanded_keys, ())

    def test_tree_invalid_replacement_preserves_indexes_and_selection(self):
        tree = self.keep(TreeView(nodes=(TreeNode("one", "One"),), selected_key="one"))
        original = (
            tree.nodes,
            tree.selected_key,
            tree.expanded_keys,
            dict(tree._nodes),
            dict(tree._parents),
            list(tree._rows),
            dict(tree._positions),
        )
        routes = (
            lambda: tree.set_tree((TreeNode("two", "Two"),), selected_key="missing"),
            lambda: tree.set_tree((TreeNode("two", "Two"),), expanded_keys=("two",)),
            lambda: tree.set_tree((TreeNode("same", "A"), TreeNode("same", "B"))),
        )
        for index, mutate in enumerate(routes):
            with self.subTest(route=index), self.assertRaises(ValueError):
                mutate()
            self.assertEqual(
                (
                    tree.nodes,
                    tree.selected_key,
                    tree.expanded_keys,
                    tree._nodes,
                    tree._parents,
                    tree._rows,
                    tree._positions,
                ),
                original,
            )

    def test_automatic_heights_follow_theme_and_fit_large_text(self):
        host = RecordingHost()
        classes = (
            Button,
            CheckBox,
            Slider,
            TextBox,
            Dropdown,
            NumericInput,
            ColorPicker,
            ProgressBar,
            MenuBar,
        )
        themes = (
            light(),
            get_theme("windows_dark"),
            get_theme("terminal"),
            light().derive(font_size=40),
        )
        for theme in themes:
            for cls in classes:
                with self.subTest(theme=theme.tokens.font_size, control=cls.__name__):
                    control = self.keep(cls(theme_override=theme))
                    style = resolve_style(control)
                    measured = control.measure(host)[1]
                    line = host.measure(
                        "M", style.font_size, style.font_family == "mono"
                    )[1]
                    self.assertGreaterEqual(measured, theme.tokens.control_height)
                    self.assertGreaterEqual(measured, line)
                    self.assertGreaterEqual(measured, line + 2 * style.padding)

    def test_explicit_height_wins_over_large_font_measurement(self):
        host = RecordingHost()
        root = self.keep(
            Container(
                width=600, height=600, theme_override=light().derive(font_size=40)
            )
        )
        controls = [
            cls(parent=root, height=23, width=150)
            for cls in (
                Button,
                CheckBox,
                Slider,
                TextBox,
                Dropdown,
                NumericInput,
                ColorPicker,
                ProgressBar,
                MenuBar,
            )
        ]
        arrange(root, host)
        self.assertTrue(all(control.bounds.height == 23 for control in controls))
        self.assertEqual(root.dirty, Dirty.NONE)
