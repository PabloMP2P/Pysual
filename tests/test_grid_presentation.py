"""Column presentation preserves numeric behavior and bounded viewport work."""

import asyncio
import importlib.util
import os
import unittest
from _ui_testcase import AsyncUIOwnerTestCase
from dataclasses import replace
from unittest.mock import patch

from test_library import RecordingHost, eventually

from pysual import App, DataGrid, GridColumn, GridRow, Rect, Style, Theme
from pysual.data_grid import _display, _parse_number
from pysual.host import Input
from pysual.icons import draw_icon
from pysual.layout import arrange
from pysual.painting import Painter, paint_tree
from pysual.render_cache import RenderCache
from pysual.runtime import Runtime


class ColumnPresentationTests(unittest.TestCase):
    def test_formats_keep_raw_numeric_order_and_empty_cells(self):
        column = GridColumn(
            "amount",
            "Amount",
            kind="number",
            format_spec=",.2f",
            prefix="$",
            align="right",
        )
        grid = DataGrid(
            columns=(column,),
            rows=(GridRow("a", (1000.5,)), GridRow("b", (9,)), GridRow("c", (None,))),
            sort_key="amount",
        )
        self.addCleanup(grid.destroy)
        self.assertEqual([r.key for r in grid._order], ["b", "a", "c"])
        self.assertEqual(_display(grid.rows[0].cells[0], column), "$1,000.50")
        self.assertEqual(_display(None, column), "")
        self.assertEqual(grid.rows[0].cells[0], 1000.5)
        self.assertEqual(_display(-1234, column), "$-1,234.00")
        self.assertEqual(
            _display(0.125, replace(column, prefix="", format_spec=".1%")), "12.5%"
        )
        self.assertEqual(
            _display(12, replace(column, prefix="", suffix=" kg", format_spec=".0f")),
            "12 kg",
        )
        self.assertEqual(_display(1.0, GridColumn("raw", "Raw", kind="number")), "1.0")

    def test_invalid_presentation_is_rejected_before_paint(self):
        for options in (
            {"align": "justify"},
            {"format_spec": ".2f"},
            {"prefix": None},
            {"suffix": "x" * 65},
            {"prefix": "a\nb"},
            {"suffix": "a\rb"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                GridColumn("value", "Value", **options)
        for spec in (
            None,
            "999999999f",
            ".999999f",
            "{value}",
            "d",
            ".17f",
            "n",
            "08.2f",
        ):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                GridColumn("value", "Value", kind="number", format_spec=spec)
        grid = DataGrid(
            columns=(GridColumn("n", "N", kind="number", format_spec=".2f"),)
        )
        self.addCleanup(grid.destroy)
        before = grid.rows
        with self.assertRaisesRegex(ValueError, "cannot use this numeric format"):
            grid.rows = (GridRow("huge", (10**400,)),)
        self.assertIs(grid.rows, before)

    def test_alignment_applies_to_headers_and_elided_cells(self):
        class PositionedHost(RecordingHost):
            def text(self, text, x, y, color, size, mono=False):
                self.positions.append((text, x, y, size))
                super().text(text, x, y, color, size, mono)

        host, app = PositionedHost(), App(width=600, height=300)
        host.positions = []
        self.addCleanup(app.destroy)
        cols = tuple(
            GridColumn(str(i), "Title", width=120, align=align)
            for i, align in enumerate(("left", "center", "right"))
        )
        grid = app.data_grid(
            columns=cols,
            rows=(GridRow("a", ("Hi", "Hi", "Hi")),),
            width=400,
            height=120,
        )
        arrange(app, host)
        paint_tree(app, host)
        for text in ("Hi", "Title"):
            positions = [p for p in host.positions if p[0] == text]
            self.assertEqual(len(positions), 3)
            for i, (_, x, _, size) in enumerate(positions):
                spare = 104 - host.measure(text, size)[0]
                self.assertAlmostEqual(x, grid.bounds.x + i * 120 + 8 + spare * (i / 2))
        grid.rows = (GridRow("a", ("A very long cell value",) * 3),)
        host.positions = []
        paint_tree(app, host)
        for text, x, _, size in host.positions:
            if text == "Title":
                continue
            self.assertTrue(text.endswith("…"))
            self.assertLessEqual(host.measure(text, size)[0], 104)

    def test_only_visible_cells_are_formatted(self):
        app, host = App(width=500, height=300), RecordingHost()
        self.addCleanup(app.destroy)
        grid = app.data_grid(
            columns=(GridColumn("n", "Number", kind="number", format_spec=",.2f"),),
            rows=tuple(GridRow(str(i), (i,)) for i in range(20_000)),
            width=200,
            height=180,
        )
        arrange(app, host)
        with patch("pysual.data_grid._display", wraps=_display) as display:
            paint_tree(app, host)
        self.assertEqual(display.call_count, grid._painted_cells)
        self.assertLess(display.call_count, 10)

    def test_sort_marker_survives_elision_alignment_and_horizontal_scroll(self):
        class PositionedHost(RecordingHost):
            def text(self, text, x, y, color, size, mono=False):
                self.positions.append((text, x, y, size))
                super().text(text, x, y, color, size, mono)

        app, host = App(width=320, height=200), PositionedHost()
        self.addCleanup(app.destroy)
        grid = app.data_grid(
            columns=(GridColumn("account", "Customer account"),),
            sort_key="account", width=120, height=100,
        )
        for size in (14, 22):
            app.theme = Theme().styled(DataGrid, Style(font_size=size), part="header")
            for align, fraction in (("left", 0), ("center", .5), ("right", 1)):
                grid.columns = (replace(grid.columns[0], align=align),)
                for descending in (False, True):
                    grid.sort_descending = descending
                    for scroll in (0, 24):
                        with self.subTest(size=size, align=align, descending=descending,
                                          scroll=scroll):
                            arrange(app, host)
                            grid._scroll_x = scroll
                            host.positions = []
                            with patch("pysual.painting.draw_icon", wraps=draw_icon) as icon:
                                paint_tree(app, host)
                            self.assertEqual(len(host.positions), 1)
                            title, x, _, font_size = host.positions[0]
                            self.assertTrue(title.endswith("…"))
                            self.assertNotIn("↑", title)
                            self.assertNotIn("↓", title)
                            icon.assert_called_once()
                            _, name, marker_x, marker_y, marker_size, _ = icon.call_args.args
                            self.assertEqual(name, "arrow_down" if descending else "arrow_up")
                            available = 140 - 16 - marker_size - 4
                            text_width = host.measure(title, font_size)[0]
                            self.assertLessEqual(text_width, available)
                            self.assertAlmostEqual(
                                x, grid.bounds.x - scroll + 8
                                + (available - text_width) * fraction,
                            )
                            self.assertAlmostEqual(marker_x, 140 - scroll - 8 - marker_size)
                            self.assertAlmostEqual(marker_y, (grid._header_height() - marker_size) / 2)


class GridViewportPixelsTests(unittest.TestCase):
    def test_contained_parts_preserve_body_bounds_and_external_body_effects(self):
        effect = Style(glow="#ffffff", glow_width=8, shadow="#00000090", shadow_blur=12)
        theme = Theme()
        for part in ("header", "cell", "scrollbar"):
            theme = theme.styled(DataGrid, effect, part=part)
        app, host = App(theme=theme), RecordingHost()
        self.addCleanup(app.destroy)
        grid = DataGrid(parent=app, left=30, top=30, width=200, height=140)
        arrange(app, host)
        bounds = Painter(host, grid.bounds, app.theme, grid).effect_bounds()
        self.assertEqual(bounds, grid.bounds)
        app.theme = theme.styled(DataGrid, effect)
        arrange(app, host)
        bounds = Painter(host, grid.bounds, app.theme, grid).effect_bounds()
        self.assertLess(bounds.x, grid.bounds.x)
        self.assertGreater(bounds.right, grid.bounds.right)
        self.assertLess(bounds.y, grid.bounds.y)
        self.assertGreater(bounds.bottom, grid.bounds.bottom)



class FormattedEditingTests(AsyncUIOwnerTestCase):
    async def test_copied_decimal_cell_pastes_back_with_displayed_precision(self):
        app, host = App(), RecordingHost()
        grid = DataGrid(parent=app, width=300, height=160,
            columns=(GridColumn("amount", "Amount", kind="number", editable=True,
                                prefix="$", suffix=" USD", format_spec=",.2f"),),
            rows=(GridRow("a", (1234.567,)),), selected_key="a")
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            grid.focus()
            await runtime.router.clipboard("c")
            self.assertEqual(host.clipboard, "$1,234.57 USD")
            grid.begin_edit()
            runtime.router.process(Input("text", text=host.clipboard, paste=True))
            runtime.router.process(Input("key_down", key="Enter"))
            self.assertIsNone(runtime.popup)
            self.assertEqual(grid.rows[0].cells[0], 1234.57)
            grid.begin_edit()
            runtime.router.process(Input("text", text="$1,23.45 USD", paste=True))
            runtime.router.process(Input("key_down", key="Enter"))
            self.assertIsNotNone(runtime.popup)
            self.assertEqual(grid.rows[0].cells[0], 1234.57)
        finally:
            runtime._stop.set()
            await task
            app.destroy()

    def test_decimal_parser_preserves_raw_values_and_limits_display_unformatting(self):
        column = GridColumn("amount", "Amount", kind="number", prefix="$", suffix=" USD", format_spec=",.2f")
        for text, expected in (("$1,234.57 USD", 1234.57), ("-$1,234.57 USD", None),
                               ("$-1,234.57 USD", -1234.57), ("1234.567", 1234.567),
                               ("1e3", 1000), ("", None)):
            with self.subTest(text=text):
                if text.startswith("-$"):
                    with self.assertRaises(ValueError):
                        _parse_number(text, column)
                else:
                    self.assertEqual(_parse_number(text, column), expected)
        for invalid in ("$1,23.45 USD", "$1,234.57 EUR", "$1,234, USD", "$1.2oops USD"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                _parse_number(invalid, column)
        grouped = GridColumn("amount", "Amount", kind="number", prefix="USD ", format_spec="_.2f")
        self.assertEqual(_parse_number("USD 1_234.57", grouped), 1234.57)
        percent = GridColumn("ratio", "Ratio", kind="number", format_spec=".1%")
        with self.assertRaises(ValueError):
            _parse_number("50.0%", percent)

    async def test_editor_uses_raw_value_and_reports_numeric_changes(self):
        app, host = App(), RecordingHost()
        grid = app.data_grid(
            columns=(
                GridColumn(
                    "amount",
                    "Amount",
                    kind="number",
                    editable=True,
                    format_spec=",.2f",
                    prefix="$",
                ),
            ),
            rows=(GridRow("a", (1234.567,)),),
            selected_key="a",
            width=200,
            height=120,
        )
        events = []
        grid.edited.connect(events.append)
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            grid.begin_edit()
            self.assertEqual(runtime.popup.children[0].text, "1234.567")
            runtime.router.process(Input("key_down", key="Enter"))
            await runtime.dispatcher.drain()
            self.assertEqual(events, [])
            self.assertEqual(grid.rows[0].cells[0], 1234.567)
            grid.begin_edit()
            runtime.popup.children[0].text = "2000.25"
            runtime.router.process(Input("key_down", key="Enter"))
            await runtime.dispatcher.drain()
            self.assertIsNone(runtime.popup)
            self.assertEqual(grid.rows[0].cells[0], 2000.25)
            self.assertEqual(
                (events[0].old_value, events[0].new_value), (1234.567, 2000.25)
            )
            self.assertEqual(events[0].origin, "user")
        finally:
            runtime._stop.set()
            await task
