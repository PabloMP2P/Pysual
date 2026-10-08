"""Typed table data, bounded paint work and edits through the shared runtime."""

import asyncio
import unittest
from _ui_testcase import AsyncUIOwnerTestCase
from dataclasses import replace
from math import inf, nan
from unittest.mock import patch

from test_library import RecordingHost, eventually

from pysual import App, DataGrid, GridColumn, GridRow
from pysual.data_grid import _validate_data
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.runtime import Runtime


def columns():
    return (
        GridColumn("name", "Name", width=160, editable=True),
        GridColumn("number", "Number", kind="number", editable=True),
        GridColumn("flag", "Enabled", kind="bool", editable=True),
    )


class GridDataTests(unittest.TestCase):
    def test_keyboard_reveals_unchanged_selection_after_wheel_scroll(self):
        app, host = App(width=500, height=300), RecordingHost()
        self.addCleanup(app.destroy)
        grid = app.data_grid(
            columns=columns(),
            rows=tuple(GridRow(str(i), (str(i), i, True)) for i in range(100)),
            selected_key="0",
            width=300,
            height=200,
        )
        arrange(app, host)
        for key, selected, delta in (
            ("Home", "0", 10),
            ("ArrowUp", "0", 10),
            ("End", "99", -10),
            ("ArrowDown", "99", -10),
        ):
            with self.subTest(key=key):
                grid.selected_key = selected
                grid.handle_input(Input("wheel", delta=delta))
                grid.handle_input(Input("key_down", key=key))
                position = int(selected)
                self.assertEqual(grid.selected_key, selected)
                self.assertLessEqual(grid._scroll, position)
                self.assertLess(position, grid._scroll + grid._page_rows())
        grid.selected_column = 0
        grid.handle_input(Input("wheel", delta=3, shift=True))
        self.assertGreater(grid._scroll_x, 0)
        grid.handle_input(Input("key_down", key="ArrowLeft"))
        self.assertEqual(grid.selected_column, 0)
        self.assertEqual(grid._scroll_x, 0)

    def test_build_time_data_does_not_scroll_away_the_first_column(self):
        app, host = App(width=500, height=300), RecordingHost()
        try:
            grid = app.data_grid(columns=columns(), width=300, height=200)
            grid.set_data(columns=columns(), rows=(GridRow("a", ("A", 1, True)),))
            self.assertEqual(grid._scroll_x, 0)
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(grid._scroll_x, 0)
            self.assertFalse(grid._pending_reveal)
        finally:
            app.destroy()

    def test_initial_selection_is_revealed_when_a_real_viewport_arrives(self):
        app, host = App(width=500, height=300), RecordingHost()
        try:
            grid = app.data_grid(
                columns=columns(),
                rows=tuple(GridRow(str(i), (str(i), i, True)) for i in range(100)),
                width=0,
                height=0,
            )
            grid.selected_key = "99"
            grid.selected_column = 2
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual((grid._scroll, grid._scroll_x), (0, 0))
            grid.width, grid.height = 300, 200
            arrange(app, host)
            paint_tree(app, host)
            self.assertLessEqual(grid._scroll, 99)
            self.assertLess(99, grid._scroll + grid._page_rows())
            cell_left = sum(c.width for c in grid.columns[:2])
            self.assertLessEqual(grid._scroll_x, cell_left)
            self.assertLessEqual(
                cell_left + grid.columns[2].width,
                grid._scroll_x + grid._viewport()[0],
            )
        finally:
            app.destroy()

    def test_oversized_selected_column_reveals_its_left_edge(self):
        app, host = App(width=500, height=300), RecordingHost()
        try:
            grid = app.data_grid(
                columns=(GridColumn("a", "First", 252), GridColumn("b", "Second", 300)),
                rows=(GridRow("row", ("First value", "Second value")),),
                width=180,
                height=200,
            )
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(grid._scroll_x, 0)
            grid.selected_column = 1
            paint_tree(app, host)
            self.assertEqual(grid._scroll_x, 252)
        finally:
            app.destroy()

    def test_set_data_commits_schema_rows_and_dependent_state_together(self):
        self.check_data_replacement(lambda grid, values: grid.set_data(**values))

    def test_update_commits_schema_rows_and_dependent_state_together(self):
        self.check_data_replacement(lambda grid, values: grid.update(**values))

    def test_update_children_commits_schema_rows_and_dependent_state_together(self):
        self.check_data_replacement(
            lambda grid, values: grid.parent.update_children({grid: values})
        )

    def check_data_replacement(self, apply):
        snapshots = []

        class ObservedGrid(DataGrid):
            def _changed(self, field, old, value):
                super()._changed(field, old, value)
                snapshots.append(
                    (
                        tuple(c.key for c in self.columns),
                        tuple(r.key for r in self.rows),
                        self.selected_key,
                        self.selected_column,
                        self.sort_key,
                        tuple(r.key for r in self._order),
                    )
                )

        app = App()
        grid = ObservedGrid(
            parent=app,
            columns=columns(),
            rows=(GridRow("a", ("A", 2, True)), GridRow("b", ("B", 1, False))),
            selected_key="a",
            selected_column=1,
            sort_key="number",
        )
        try:
            replacement = (GridColumn("number", "Count", kind="number"),)
            apply(grid, dict(
                columns=replacement, rows=(GridRow("a", (3,)), GridRow("c", (2,)))
            ))
            expected = (("number",), ("a", "c"), "a", 0, "number", ("c", "a"))
            self.assertTrue(snapshots)
            self.assertTrue(all(snapshot == expected for snapshot in snapshots))
            snapshots.clear()
            before = (
                grid.columns,
                grid.rows,
                grid.sort_key,
                grid.selected_key,
                grid._order,
            )
            for invalid_columns, invalid_rows in (
                ((GridColumn("bad", "Bad"),), (GridRow("a", (3,)),)),
                (replacement, (GridRow("a", (3,)), GridRow("a", (4,)))),
                (replacement, (GridRow("a", (3, 4)),)),
                ([], ()),
            ):
                with self.assertRaises((ValueError, TypeError)):
                    apply(grid, dict(columns=invalid_columns, rows=invalid_rows))
                self.assertEqual(
                    (
                        grid.columns,
                        grid.rows,
                        grid.sort_key,
                        grid.selected_key,
                        grid._order,
                    ),
                    before,
                )
                self.assertEqual(snapshots, [])
            apply(grid, dict(columns=(), rows=()))
            self.assertIsNone(grid.selected_key)
            self.assertIsNone(grid.sort_key)
            self.assertEqual(grid.selected_column, 0)
            self.assertEqual(grid._order, [])
        finally:
            app.destroy()

    def test_dataset_update_preserves_explicit_fields_and_rejects_invalid_batches(self):
        for children in (False, True):
            with self.subTest(children=children):
                app = App()
                self.addCleanup(app.destroy)
                label = app.label(text="Before")
                grid = app.data_grid(
                    columns=columns(), rows=(GridRow("old", ("Old", 2, True)),),
                    selected_key="old", selected_column=1, sort_key="number",
                )

                def apply(values):
                    if children:
                        app.update_children({label: {"text": "After"}, grid: values})
                    else:
                        grid.update(**values)

                replacement = dict(
                    columns=(GridColumn("value", "Value", kind="number"),),
                    rows=(GridRow("a", (3,)), GridRow("b", (2,))),
                )
                before = grid.columns, grid.rows, grid.selected_key, grid.selected_column, grid.sort_key
                for invalid in (
                    {"selected_key": "old"}, {"selected_column": 1}, {"sort_key": "number"},
                ):
                    with self.assertRaises(ValueError):
                        apply({**replacement, **invalid})
                    self.assertEqual(
                        (grid.columns, grid.rows, grid.selected_key, grid.selected_column, grid.sort_key),
                        before,
                    )
                    self.assertEqual(label.text, "Before")
                apply({**replacement, "selected_key": "b", "selected_column": 0,
                       "sort_key": "value", "sort_descending": True})
                self.assertEqual((grid.selected_key, grid.selected_column, grid.sort_key), ("b", 0, "value"))
                self.assertEqual([row.key for row in grid._order], ["a", "b"])

    def test_update_rows_is_bounded_atomic_and_preserves_unchanged_records(self):
        grid = DataGrid(
            columns=(GridColumn("value", "Value", kind="number"),),
            rows=(GridRow("a", (1,)), GridRow("b", (2,)), GridRow("c", (3,))),
            selected_key="a",
            sort_key="value",
        )
        try:
            unchanged = grid.rows[2]
            grid.update_rows((GridRow("a", (5,)), GridRow("d", (4,))), remove=("b",))
            self.assertEqual([r.key for r in grid.rows], ["a", "c", "d"])
            self.assertIs(grid.rows[1], unchanged)
            self.assertEqual([r.key for r in grid._order], ["c", "d", "a"])
            self.assertEqual(grid.selected_key, "a")
            before = grid.rows
            for rows, remove in (
                ((GridRow("a", ("invalid",)),), ()),
                ((GridRow("a", (2,)), GridRow("a", (3,))), ()),
                ((GridRow("a", (2,)),), ("a",)),
                ((), ("missing",)),
                ((), ("a", "a")),
                (tuple(GridRow(str(i), (i,)) for i in range(20_001)), ()),
            ):
                with self.assertRaises((ValueError, TypeError)):
                    grid.update_rows(rows, remove=remove)
                self.assertIs(grid.rows, before)
        finally:
            grid.destroy()

    def test_set_data_validates_replacement_cells_once_before_sorting(self):
        grid = DataGrid(
            columns=(GridColumn("value", "Value", kind="number"),),
            rows=(GridRow("old", (0,)),), sort_key="value", selected_key="old",
        )
        self.addCleanup(grid.destroy)
        replacement = tuple(GridRow(str(i), (2000 - i,)) for i in range(2000))
        with patch("pysual.data_grid._validate_data", wraps=_validate_data) as validate:
            grid.set_data(columns=grid.columns, rows=replacement)
        validate.assert_called_once()
        self.assertIs(grid.rows, replacement)
        self.assertEqual([row.cells[0] for row in grid._order], list(range(1, 2001)))
        self.assertIsNone(grid.selected_key)
        # A short row must be rejected as invalid data before the sort indexes it.
        before = grid.rows, grid._order, grid._by_key, grid._positions
        with self.assertRaisesRegex(ValueError, "exactly 1 cells"):
            grid.set_data(columns=grid.columns, rows=(GridRow("bad", ()),))
        self.assertEqual((grid.rows, grid._order, grid._by_key, grid._positions), before)

    def test_validation_atomicity_stable_selection_and_sorting(self):
        grid = DataGrid(
            columns=columns(),
            rows=(GridRow("a", ("A", 10, True)), GridRow("b", ("B", 2, False))),
            selected_key="a",
        )
        try:
            original = grid.rows
            for invalid in (
                (GridRow("a", ("A",)),),
                (GridRow("a", ("A", "bad", True)),),
                (original[0], original[0]),
            ):
                with self.assertRaises((ValueError, TypeError)):
                    grid.rows = invalid
                self.assertEqual(grid.rows, original)
            with self.assertRaises(ValueError):
                grid.set_cell("a", "number", float("inf"))
            with self.assertRaises(TypeError):
                grid.set_cell("a", "number", True)
            grid.sort_key = "number"
            self.assertEqual([r.key for r in grid._order], ["b", "a"])
            self.assertEqual(grid.selected_key, "a")
            grid.set_cell("a", "number", 1)
            self.assertEqual([r.key for r in grid._order], ["a", "b"])
            self.assertEqual(grid.selected_row.cells[1], 1)
            grid.rows = (grid.rows[1],)
            self.assertIsNone(grid.selected_row)
            grid.columns = tuple(replace(c, key=f"new_{c.key}") for c in columns())
            self.assertIsNone(grid.sort_key)
            with self.assertRaises(ValueError):
                GridColumn("bad", "Bad", width=10)
            with self.assertRaises(TypeError):
                GridRow("bad", ["mutable"])
        finally:
            grid.destroy()

    def test_paint_is_limited_by_viewport_and_outer_clip(self):
        app, host = App(width=500, height=300), RecordingHost()
        try:
            outer = app.scroll_area(width=200, height=120)
            grid = outer.data_grid(
                columns=columns(),
                rows=tuple(
                    GridRow(str(i), (f"Row {i}", i, False)) for i in range(20_000)
                ),
                width=480,
                height=260,
            )
            arrange(app, host)
            paint_tree(app, host)
            self.assertLessEqual(grid._painted_cells, 6)
            self.assertLess(grid._painted_cells, len(grid.rows))
            grid.selected_key = "19999"
            arrange(app, host)
            paint_tree(app, host)
            self.assertLessEqual(grid._painted_cells, 6)
            grid.rows = grid.rows[:2]
            paint_tree(app, host)
            self.assertEqual(grid._scroll, 0)
        finally:
            app.destroy()

    def test_reused_rows_are_revalidated_when_columns_change(self):
        grid = DataGrid(
            columns=(GridColumn("value", "Value", kind="number"),),
            rows=(GridRow("a", (1,)), GridRow("b", (2,))),
        )
        try:
            original = grid.rows
            grid.set_cell("a", "value", 1)
            self.assertIs(grid.rows, original)
            grid.set_cell("a", "value", 3)
            self.assertIs(grid.rows[1], original[1])
            with self.assertRaises(TypeError):
                grid.columns = (GridColumn("value", "Value", kind="bool"),)
            self.assertEqual(grid.columns[0].kind, "number")
            with self.assertRaises(ValueError):
                grid.rows = (grid.rows[0], grid.rows[0])
        finally:
            grid.destroy()

    def test_sorted_edit_reveals_selected_row_and_wheel_keeps_fractions(self):
        app, host = App(width=400, height=300), RecordingHost()
        try:
            grid = app.data_grid(
                columns=(GridColumn("value", "Value", kind="number"),),
                rows=tuple(GridRow(str(i), (i,)) for i in range(100)),
                selected_key="0",
                sort_key="value",
                width=300,
                height=200,
            )
            arrange(app, host)
            grid.set_cell("0", "value", 100)
            position = grid._positions["0"]
            self.assertLessEqual(grid._scroll, position)
            self.assertLess(position, grid._scroll + grid._page_rows())
            grid.set_cell("0", "value", -1)
            self.assertEqual(grid._scroll, 0)
            for _ in range(100):
                grid.handle_input(Input("wheel", delta=0.01))
            self.assertEqual(grid._scroll, 3)
            for _ in range(100):
                grid.handle_input(Input("wheel", delta=-0.01))
            self.assertEqual(grid._scroll, 0)
        finally:
            app.destroy()


class GridRuntimeTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        edits = []

        class Demo(App):
            def build(self):
                self.grid = self.data_grid(
                    columns=columns(),
                    rows=tuple(
                        GridRow(str(i), (f"Row {i}", i, i % 2 == 0)) for i in range(100)
                    ),
                    selected_key="0",
                    width=300,
                    height=250,
                )

            def grid_on_edited(self, event):
                edits.append(
                    (
                        event.row_key,
                        event.column_key,
                        event.old_value,
                        event.new_value,
                        event.origin,
                    )
                )

        self.edits = edits
        self.app, self.host = Demo(width=480, height=320), RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task

    async def inputs(self, *events):
        self.host.events.extend(events)
        await eventually(lambda: not self.host.events or self.task.done())
        if self.task.done():
            await self.task
        await self.runtime.dispatcher.drain()
        # Pointer coordinates in following steps must use the completed layout,
        # including popups opened by the dispatched handlers.
        await eventually(lambda: not self.runtime._dirty or self.task.done())
        self.runtime.dispatcher.raise_errors()

    async def key(self, key):
        await self.inputs(Input("key_down", key=key))

    async def test_cell_editor_follows_window_resize_and_preserves_pending_edit(self):
        grid = self.app.grid
        grid.anchor = "right,bottom"
        await self.inputs()
        grid.begin_edit()
        await self.inputs()
        popup = self.runtime.popup
        editor = popup.children[0]
        editor.text = "Resized edit"
        before = grid.bounds
        self.host.size = (680, 420)
        await self.inputs(Input("resize"))
        self.assertTrue(popup.is_open)
        self.assertEqual(grid.bounds.x, before.x + 200)
        self.assertEqual(grid.bounds.y, before.y + 100)
        self.assertEqual(popup.bounds.x, grid.bounds.x)
        self.assertEqual(popup.bounds.y, grid.bounds.y + grid._header_height())
        self.assertEqual(editor.text, "Resized edit")
        await self.key("Enter")
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(grid.rows[0].cells[0], "Resized edit")

    async def test_general_dataset_replacement_keeps_keyboard_navigation_valid(self):
        grid = self.app.grid
        for children in (False, True):
            for sorted_grid in (False, True):
                for reverse in (False, True):
                    with self.subTest(children=children, sorted=sorted_grid, reverse=reverse):
                        grid.set_data(
                            columns=(GridColumn("a", "A"), GridColumn("b", "B")),
                            rows=(GridRow("old", ("First", "Second")),),
                        )
                        grid.update(selected_key="old", selected_column=1,
                                    sort_key="b" if sorted_grid else None)
                        grid.focus()
                        await self.inputs()
                        values = dict(columns=(GridColumn("a", "A"),),
                                      rows=(GridRow("new", ("New",)),))
                        if reverse:
                            values = dict(reversed(tuple(values.items())))
                        if children:
                            self.app.update_children({grid: values})
                        else:
                            grid.update(**values)
                        self.assertEqual((grid.selected_key, grid.selected_column, grid.sort_key),
                                         (None, 0, None))
                        await self.key("ArrowDown")
                        self.assertEqual(grid.selected_key, "new")
                        self.assertEqual(grid.selected_cell_text, "New")

    async def test_double_click_and_touch_tap_open_the_existing_cell_editor(self):
        grid = self.app.grid
        activated = []
        grid.activated.connect(activated.append)
        x, y = grid.bounds.x + 20, grid.bounds.y + grid.header_height + 15

        def tap(at, *, kind="mouse", pointer_id=0, dx=0):
            with patch("pysual.data_grid.monotonic", return_value=at):
                self.runtime.router.process(
                    Input(
                        "pointer_down",
                        x + dx,
                        y,
                        pointer_kind=kind,
                        pointer_id=pointer_id,
                    )
                )
                self.runtime.router.process(
                    Input(
                        "pointer_up",
                        x + dx,
                        y,
                        pointer_kind=kind,
                        pointer_id=pointer_id,
                    )
                )

        tap(1)
        self.assertIsNone(self.runtime.popup)
        tap(1.5)
        self.assertIsNone(self.runtime.popup)
        tap(1.7)
        self.assertIsNotNone(self.runtime.popup)
        self.runtime.popup.children[0].text = "Pointer edited"
        await self.key("Enter")
        self.assertEqual(grid.rows[0].cells[0], "Pointer edited")
        self.assertEqual(
            (activated[-1].row_key, activated[-1].column_key, activated[-1].origin),
            ("0", "name", "user"),
        )
        tap(2, kind="touch", pointer_id=17)
        grid.set_data(
            columns=grid.columns,
            rows=(GridRow("0", ("Fresh", 0, True)), *grid.rows[1:]),
        )
        tap(2.1, kind="touch", pointer_id=18)
        self.assertIsNone(self.runtime.popup)
        self.runtime.router.process(Input("blur"))
        tap(2.2, kind="touch", pointer_id=19)
        self.assertIsNone(self.runtime.popup)
        tap(2.3, kind="touch", pointer_id=20)
        self.assertIsNotNone(self.runtime.popup)
        await self.key("Escape")
        self.assertEqual(grid.rows[0].cells[0], "Fresh")

    async def test_numeric_edit_error_commit_cancel_and_data_replacement(self):
        grid = self.app.grid
        grid.focus()
        await self.key("ArrowRight")
        await self.key("F2")
        popup = self.runtime.popup
        self.assertIsNotNone(popup)
        popup.children[0].text = "not a number"
        await self.key("Enter")
        self.assertIs(self.runtime.popup, popup)
        self.assertEqual(grid.rows[0].cells[1], 0)
        self.assertEqual(
            popup.children[1].foreground, grid.effective_theme.tokens.danger
        )
        self.assertTrue(popup.children[1].tooltip)
        self.assertTrue(popup.children[0].focused)
        popup.children[0].text = "42"
        await self.key("Enter")
        self.assertIsNone(self.runtime.popup)
        self.assertTrue(grid.focused)
        self.assertEqual(grid.rows[0].cells[1], 42)
        self.assertEqual(self.edits, [("0", "number", 0, 42, "user")])
        await self.key("F2")
        self.runtime.popup.children[0].text = "99"
        await self.key("Escape")
        self.assertEqual(grid.rows[0].cells[1], 42)
        await self.key("F2")
        grid.rows = grid.rows[1:]
        self.assertIsNone(self.runtime.popup)
        self.assertIsNone(grid.selected_key)

    async def test_boolean_choices_keyboard_pointer_cancel_and_empty(self):
        grid = self.app.grid
        grid.selected_column = 2
        grid.focus()
        await self.key("F2")
        popup = self.runtime.popup
        choice = popup.children[0]
        self.assertEqual(choice.items, ("True", "False", "Empty"))
        self.assertEqual(choice.selected_index, 0)
        await self.key("ArrowDown")
        await self.key("Escape")
        self.assertIs(grid.rows[0].cells[2], True)
        await self.key("F2")
        await self.key("End")
        await self.key("Enter")
        self.assertIsNone(grid.rows[0].cells[2])
        self.assertEqual(self.edits[-1], ("0", "flag", True, None, "user"))
        await self.key("F2")
        self.assertEqual(self.runtime.popup.children[0].selected_index, 2)
        before = grid.rows
        await self.key("Space")
        self.assertIs(grid.rows, before)
        await self.key("F2")
        choice = self.runtime.popup.children[0]
        x, y = choice.bounds.x + 10, choice.bounds.y + choice.row_height + 10
        await self.inputs(Input("pointer_down", x, y), Input("pointer_up", x, y))
        self.assertIsNone(self.runtime.popup)
        self.assertIs(grid.rows[0].cells[2], False)
        self.assertEqual(self.edits[-1], ("0", "flag", None, False, "user"))

    async def test_atomic_invalid_data_keeps_editor_and_events_intact(self):
        grid = self.app.grid
        grid.focus()
        await self.key("F2")
        popup = self.runtime.popup
        popup.children[0].text = "unsaved"
        changes = []
        grid.changed.connect(lambda event: changes.append((event.new_value, grid.rows)))
        with self.assertRaises(ValueError):
            grid.set_data(columns=(), rows=grid.rows)
        self.assertIs(self.runtime.popup, popup)
        self.assertEqual(popup.children[0].text, "unsaved")
        await asyncio.sleep(0.02)
        self.assertEqual(changes, [])
        grid.set_data(
            columns=(GridColumn("one", "One"),), rows=(GridRow("new", ("N",)),)
        )
        self.assertIsNone(self.runtime.popup)
        await asyncio.sleep(0.02)
        self.assertEqual(changes, [(None, grid.rows)])

    async def test_boolean_pointer_commit_requires_release_on_the_pressed_row(self):
        grid = self.app.grid
        grid.selected_column = 2
        grid.focus()
        await self.key("F2")
        popup = self.runtime.popup
        choice = popup.children[0]
        x = choice.bounds.x + 10
        false_y = choice.bounds.y + choice.row_height + 10
        empty_y = false_y + choice.row_height
        await self.inputs(
            Input("pointer_down", x, false_y),
            Input("pointer_move", x, empty_y),
            Input("pointer_up", x, empty_y),
        )
        self.assertIs(self.runtime.popup, popup)
        self.assertIs(grid.rows[0].cells[2], True)
        self.assertEqual(self.edits, [])
        # A later release without another press must not reuse the canceled click.
        await self.inputs(Input("pointer_up", x, false_y))
        self.assertIs(self.runtime.popup, popup)
        await self.inputs(
            Input("pointer_down", x, false_y), Input("pointer_up", x, false_y)
        )
        self.assertIsNone(self.runtime.popup)
        self.assertIs(grid.rows[0].cells[2], False)
        self.assertEqual(self.edits, [("0", "flag", True, False, "user")])

    async def test_column_resize_capture_commit_and_cancel_restore(self):
        grid = self.app.grid
        grid.focus()
        original = grid.columns
        x = grid.bounds.x + grid.columns[0].width
        y = grid.bounds.y + 10
        await self.inputs(Input("pointer_down", x, y), Input("pointer_move", x + 80, y))
        self.assertIs(self.runtime.router.capture, grid)
        self.assertEqual(grid.columns, original)
        self.assertEqual(grid._view_columns()[0].width, original[0].width + 80)
        self.assertIsNone(grid.sort_key)
        await self.key("Escape")
        self.assertIsNone(grid._resize)
        self.assertEqual(grid.columns, original)
        await self.inputs(Input("pointer_up", x + 80, y))
        for cancellation in ("pointer_cancel", "blur"):
            await self.inputs(
                Input("pointer_down", x, y), Input("pointer_move", x + 60, y)
            )
            await self.inputs(Input(cancellation, x + 60, y))
            self.assertIsNone(grid._resize)
            self.assertEqual(grid.columns, original)
        grid.focus()
        await self.inputs(
            Input("pointer_down", x, y), Input("pointer_move", x + 400, y)
        )
        await self.inputs(Input("pointer_up", x + 400, y))
        self.assertEqual(grid.columns[0].width, original[0].width + 400)
        self.assertEqual(grid.columns[1:], original[1:])
        self.assertIsNone(grid.sort_key)
        self.assertIsNone(self.runtime.router.capture)

    async def test_resize_ignores_nonfinite_pointer_and_enforces_minimum(self):
        grid = self.app.grid
        x, y = grid.bounds.x + grid.columns[0].width, grid.bounds.y + 10
        await self.inputs(Input("pointer_down", x, y))
        for invalid in (inf, -inf, nan):
            grid.handle_input(Input("pointer_move", invalid, y))
            self.assertEqual(grid._view_columns()[0].width, 160)
        await self.inputs(
            Input("pointer_move", x - 1000, y), Input("pointer_up", x - 1000, y)
        )
        self.assertEqual(grid.columns[0].width, 48)

    async def test_keyboard_header_sort_and_scrollbar_drag(self):
        grid = self.app.grid
        grid.focus()
        await self.key("End")
        self.assertEqual(grid.selected_key, "99")
        await self.key("ArrowRight")
        await self.key("ArrowRight")
        self.assertGreater(grid._scroll_x, 0)
        grid.selected_column = 0
        box = grid.bounds
        await self.inputs(
            Input("pointer_down", box.x + 20, box.y + 10),
            Input("pointer_up", box.x + 20, box.y + 10),
        )
        self.assertEqual(grid.sort_key, "name")
        self.assertEqual(grid.selected_key, "99")
        thumb = grid._thumbs()[0]
        await self.inputs(
            Input("pointer_down", box.x + thumb.x + 3, box.y + thumb.y + 3),
            Input("pointer_move", box.x + thumb.x + 3, box.y + grid.header_height + 3),
            Input("pointer_up", box.x + thumb.x + 3, box.y + grid.header_height + 3),
        )
        self.assertEqual(grid._scroll, 0)
        self.assertIsNone(grid._drag)
        old = grid._scroll_x
        await self.inputs(Input("wheel", box.x + 20, box.y + 70, delta=1, shift=True))
        self.assertGreater(grid._scroll_x, old)

    async def test_header_sort_commits_one_final_order(self):
        grid = self.app.grid
        grid.sort_key = "number"
        grid.sort_descending = True
        observations = []
        rebuild = DataGrid._rebuild

        def observed(control):
            observations.append((control.sort_key, control.sort_descending))
            return rebuild(control)

        grid.selected_column = 0
        box = grid.bounds
        with patch.object(DataGrid, "_rebuild", observed):
            await self.inputs(
                Input("pointer_down", box.x + 20, box.y + 10),
                Input("pointer_up", box.x + 20, box.y + 10),
            )
        self.assertEqual(observations, [("name", False)])
        self.assertEqual([row.cells[0] for row in grid._order], sorted(row.cells[0] for row in grid.rows))
        self.assertEqual(grid.selected_key, "0")
        observations.clear()
        with patch.object(DataGrid, "_rebuild", observed):
            await self.inputs(
                Input("pointer_down", box.x + 20, box.y + 10),
                Input("pointer_up", box.x + 20, box.y + 10),
            )
        self.assertEqual(observations, [("name", True)])

    async def test_unchanged_text_editor_preserves_line_breaks_without_an_edit(self):
        grid = self.app.grid
        grid.selected_column = 0
        for original in ("one\ntwo", "one\rtwo", "one\r\ntwo", None, ""):
            with self.subTest(original=original):
                grid.rows = (GridRow("0", (original, 1, False)),)
                self.edits.clear()
                before = grid.rows
                grid.begin_edit()
                await self.key("Enter")
                self.assertIsNone(self.runtime.popup)
                self.assertIs(grid.rows, before)
                self.assertEqual(grid.selected_row.cells[0], original)
                self.assertEqual(self.edits, [])
        grid.rows = (GridRow("0", ("one\ntwo", 1, False)),)
        grid.begin_edit()
        self.runtime.popup.children[0].text = "Changed"
        await self.key("Enter")
        self.assertEqual(grid.selected_row.cells[0], "Changed")
        self.assertEqual(self.edits, [("0", "name", "one\ntwo", "Changed", "user")])

    async def test_numeric_editor_preserves_exact_values_and_rejects_nonfinite(self):
        grid = self.app.grid
        grid.selected_column = 1
        for original, text, expected in (
            (2**53 + 1, str(2**53 + 1), 2**53 + 1),
            (0, "+9007199254740993", 2**53 + 1),
            (0, " -9007199254740993 ", -(2**53 + 1)),
            (0, "1.25", 1.25),
            (0, "2e3", 2000.0),
            (0, "", None),
            (None, "", None),
            (1.0, "1.0", 1.0),
            (1, "1.0", 1.0),
            (1.0, "1", 1),
        ):
            with self.subTest(text=text, original=original):
                grid.rows = (GridRow("0", ("Row", original, False)),)
                self.edits.clear()
                before = grid.rows
                grid.begin_edit()
                self.runtime.popup.children[0].text = text
                await self.key("Enter")
                self.assertIsNone(self.runtime.popup)
                actual = grid.selected_row.cells[1]
                self.assertEqual(actual, expected)
                self.assertIs(type(actual), type(expected))
                if type(original) is type(expected) and original == expected:
                    self.assertIs(grid.rows, before)
                    self.assertEqual(self.edits, [])
                else:
                    self.assertEqual(
                        self.edits, [("0", "number", original, expected, "user")]
                    )

        for text in ("nan", "inf", "-inf", "1e999"):
            with self.subTest(text=text):
                grid.begin_edit()
                popup = self.runtime.popup
                popup.children[0].text = text
                before = grid.rows
                await self.key("Enter")
                self.assertIs(self.runtime.popup, popup)
                self.assertIs(grid.rows, before)
                await self.key("Escape")

        grid.rows = (GridRow("0", (None, 0, False)),)
        grid.selected_column = 0
        grid.begin_edit()
        await self.key("Enter")
        self.assertIsNone(grid.selected_row.cells[0])
