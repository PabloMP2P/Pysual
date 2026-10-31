"""Column presentation preserves numeric behavior and bounded viewport work."""

import asyncio
import importlib.util
import os
import sys
import unittest
from _ui_testcase import AsyncUIOwnerTestCase
from dataclasses import replace
from unittest.mock import patch

from test_library import RecordingHost, eventually

from pysual import App, DataGrid, GridColumn, GridRow, Rect, Style, Theme, get_theme, theme_names
from pysual.data_grid import _display, _measure_places, _parse_number
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

    def test_number_columns_default_to_right_alignment(self):
        number = GridColumn("n", "Number", kind="number")
        self.assertEqual(number.align, "right")
        self.assertEqual(GridColumn("name", "Name").align, "left")
        self.assertEqual(GridColumn("flag", "Flag", kind="bool").align, "left")
        self.assertEqual(replace(number, align="left").align, "left")
        self.assertEqual(replace(number, align="center").align, "center")

        class PositionedHost(RecordingHost):
            def text(self, text, x, y, color, size, mono=False):
                self.positions.append((text, x, y, size))
                super().text(text, x, y, color, size, mono)

        host, app = PositionedHost(), App(width=400, height=200)
        host.positions = []
        self.addCleanup(app.destroy)
        grid = app.data_grid(
            columns=(
                GridColumn("name", "Name", width=120),
                GridColumn("n", "Number", kind="number", width=120),
            ),
            rows=(GridRow("a", ("Hi", 1.5)), GridRow("b", ("Yo", 2))),
            width=280,
            height=140,
        )
        arrange(app, host)
        paint_tree(app, host)
        for text, column, fraction in (
            ("Name", 0, 0),
            ("Hi", 0, 0),
            ("Yo", 0, 0),
            ("Number", 1, 1),
            ("1.5", 1, 1),
            ("2.0", 1, 1),
        ):
            matches = [item for item in host.positions if item[0] == text]
            self.assertEqual(len(matches), 1)
            _, x, _, size = matches[0]
            spare = 104 - host.measure(text, size)[0]
            self.assertAlmostEqual(x, grid.bounds.x + 1 + column * 120 + 8 + spare * fraction)

    def test_plain_number_columns_share_decimal_places(self):
        column = GridColumn("n", "Number", kind="number", prefix="$")
        grid = DataGrid(
            columns=(column,),
            rows=(
                GridRow("a", (1,)),
                GridRow("b", (-1.5,)),
                GridRow("c", (2.25,)),
                GridRow("d", (None,)),
            ),
            selected_key="a",
        )
        self.addCleanup(grid.destroy)
        self.assertEqual(
            [
                _display(row.cells[0], column, places=grid._decimal_places()[0])
                for row in grid.rows
            ],
            ["$1.00", "$-1.50", "$2.25", ""],
        )
        self.assertEqual(grid.selected_cell_text, "$1.00")

        grid.columns = (GridColumn("n", "Number", kind="number"),)
        grid.rows = (GridRow("a", (-2,)), GridRow("b", (10,)))
        self.assertEqual(grid.selected_cell_text, "-2")
        grid.selected_key = "b"
        self.assertEqual(grid.selected_cell_text, "10")

        grid.columns = (replace(column, format_spec=".1f", prefix=""),)
        grid.rows = (GridRow("a", (1,)), GridRow("b", (1.25,)))
        grid.selected_key = "a"
        self.assertEqual(grid.selected_cell_text, "1.0")

        grid.columns = (GridColumn("n", "Number", kind="number"),)
        grid.rows = (GridRow("a", (1.5,)), GridRow("b", (1e-20,)))
        self.assertEqual(grid.selected_cell_text, "1.5")
        grid.selected_key = "b"
        self.assertEqual(grid.selected_cell_text, "1e-20")
        grid.set_cell("b", "n", 2.25)
        grid.selected_key = "a"
        self.assertEqual(grid.selected_cell_text, "1.50")
        grid.selected_key = "b"
        self.assertEqual(grid.selected_cell_text, "2.25")
        huge = 10**20
        grid.rows = (GridRow("a", (huge,)), GridRow("b", (1.5,)))
        grid.selected_key = "a"
        self.assertEqual(grid.selected_cell_text, f"{huge}.0")
        grid.selected_key = "b"
        self.assertEqual(grid.selected_cell_text, "1.5")
        grid.rows = (GridRow("a", (1e-7,)),)
        grid.selected_key = "a"
        self.assertEqual(grid.selected_cell_text, "0.0000001")

    def test_one_number_edit_updates_only_the_shared_decimal_count(self):
        grid = DataGrid(
            columns=(GridColumn("n", "Number", kind="number"),),
            rows=(
                GridRow("a", (1,)),
                GridRow("b", (1.5,)),
                GridRow("c", (2.25,)),
            ),
            selected_key="c",
        )
        self.addCleanup(grid.destroy)
        self.assertEqual(grid.selected_cell_text, "2.25")
        with patch("pysual.data_grid._measure_places", wraps=_measure_places) as measure:
            grid.set_cell("a", "n", 1.234)
            self.assertEqual(measure.call_count, 0)
            self.assertEqual(grid.selected_cell_text, "2.250")
            grid.selected_key = "b"
            self.assertEqual(grid.selected_cell_text, "1.500")
            grid.columns = (replace(grid.columns[0], width=200),)
            self.assertEqual(grid.selected_cell_text, "1.500")
            self.assertEqual(measure.call_count, 0)
            grid.set_cell("a", "n", 1)
            self.assertEqual(measure.call_count, 0)
            grid.selected_key = "c"
            self.assertEqual(grid.selected_cell_text, "2.25")
            self.assertEqual(measure.call_count, 1)
            grid.selected_key = "b"
            self.assertEqual(grid.selected_cell_text, "1.50")

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

    def test_default_number_format_rejects_unprintable_integers_atomically(self):
        previous_limit = sys.get_int_max_str_digits()
        self.addCleanup(sys.set_int_max_str_digits, previous_limit)
        sys.set_int_max_str_digits(640)
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        columns = (GridColumn("n", "Number", kind="number"),)
        grid = app.data_grid(columns=columns, rows=(GridRow("r", (42,)),),
                             selected_key="r", width=240, height=150)
        children, rows, order = tuple(app.children), grid.rows, grid._order
        for value in (10**640, -(10**640)):
            with self.subTest(negative=value < 0):
                with self.assertRaisesRegex(ValueError, "cannot use this numeric format"):
                    app.data_grid(columns=columns, rows=(GridRow("bad", (value,)),))
                self.assertEqual(tuple(app.children), children)
                with self.assertRaisesRegex(ValueError, "cannot use this numeric format"):
                    grid.rows = (GridRow("bad", (value,)),)
                self.assertIs(grid.rows, rows)
                self.assertIs(grid._order, order)
                self.assertEqual(grid.selected_key, "r")
                with self.assertRaisesRegex(ValueError, "cannot use this numeric format"):
                    grid.set_cell("r", "n", value)
                self.assertIs(grid.rows, rows)
                self.assertIs(grid._order, order)
        arrange(app, host)
        paint_tree(app, host)
        self.assertIn("42", host.texts)

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
                self.assertAlmostEqual(x, grid.bounds.x + 1 + i * 120 + 8 + spare * (i / 2))
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
                            shown = grid._scroll_x
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
                                x, grid.bounds.x + 1 - shown + 8
                                + (available - text_width) * fraction,
                            )
                            self.assertAlmostEqual(marker_x, 141 - shown - 8 - marker_size)
                            self.assertAlmostEqual(marker_y, (grid._header_height() + 1 - marker_size) / 2)


class GridPaintHost(RecordingHost):
    def __init__(self):
        super().__init__()
        self.surfaces = []
        self.rects = []
        self.captions = []
        self.current_clip = Rect()

    def clip(self, rect):
        self.current_clip = rect

    def styled_rect(self, rect, style):
        self.surfaces.append((rect, style, self.current_clip))
        return True

    def rect(self, rect, fill, *args, **kwargs):
        self.rects.append((rect, fill, self.current_clip))

    def text(self, text, *args, **kwargs):
        self.captions.append((text, self.current_clip))
        super().text(text, *args, **kwargs)


class GridViewportPixelsTests(unittest.TestCase):
    def test_header_cells_and_scrollbars_stay_inside_each_themes_frame(self):
        app, host = App(width=500, height=300), GridPaintHost()
        self.addCleanup(app.destroy)
        grid = DataGrid(parent=app, left=20, top=20, width=220.5, height=160.5,
                        columns=(GridColumn("name", "Name", 120.5),
                                 GridColumn("value", "Value", 140)),
                        rows=tuple(GridRow(str(i), ("Account", "Value")) for i in range(20)))
        for name in theme_names():
            with self.subTest(theme=name):
                app.theme = get_theme(name)
                arrange(app, host)
                host.surfaces.clear()
                host.rects.clear()
                host.captions.clear()
                paint_tree(app, host)
                # The beveled families have a two-pixel frame; other families
                # have a one-pixel stroke. No painted part may cover either.
                inset = 2 if name.startswith(("win31", "analog_arcade")) else 1
                inner = grid.bounds.inset(inset)
                body_index = next(i for i, (box, _, _) in enumerate(host.surfaces)
                                  if box == grid.bounds)
                for _, _, clip in host.surfaces[body_index + 1:]:
                    self.assertEqual(clip.intersect(inner), clip)
                vertical, horizontal = grid._scrollbars()
                for bar in (vertical, horizontal):
                    self.assertEqual(bar.track.intersect(inner), bar.track)
                self.assertEqual(vertical.track.bottom, horizontal.track.y)
                self.assertEqual(horizontal.track.right, vertical.track.x)
                for text, clip in host.captions:
                    if text == "Name":
                        self.assertEqual(clip.right, inner.right)
                        self.assertEqual(clip.bottom, vertical.track.y)
                    elif text == "Account":
                        self.assertEqual(clip.right, vertical.track.x)
                        self.assertEqual(clip.bottom, horizontal.track.y)
                # The header separator occupies its own final pixel; it does
                # not straddle the first row or the scrollbar track.
                separators = [box for box, _, _ in host.rects
                              if box.width == inner.width and box.height == 1]
                self.assertEqual(separators, [Rect(inner.x, vertical.track.y - 1,
                                                   inner.width, 1)])

    def test_header_above_scrollbar_can_sort_and_resize_but_frame_cannot(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        grid = DataGrid(parent=app, left=20, top=20, width=220, height=160,
                        columns=(GridColumn("name", "Name", 212),),
                        rows=tuple(GridRow(str(i), (str(i),)) for i in range(20)))
        arrange(app, host)
        box = grid.bounds
        for x, y in ((box.x, box.y + 10), (box.x + 10, box.y),
                     (box.right - .5, box.y + 10), (box.x + 10, box.bottom - .5)):
            grid.handle_input(Input("pointer_down", x=x, y=y))
            self.assertIsNone(grid.sort_key)
            self.assertIsNone(grid.selected_key)
            self.assertIsNone(grid._resize)
        grid.handle_input(Input("pointer_down", x=box.x + 207, y=box.y + 10))
        self.assertEqual(grid.sort_key, "name")
        grid.handle_input(Input("pointer_down", x=box.x + 213, y=box.y + 10))
        self.assertIsNotNone(grid._resize)
        grid.handle_input(Input("pointer_move", x=box.x + 233, y=box.y + 10))
        grid.handle_input(Input("pointer_up", x=box.x + 233, y=box.y + 10))
        self.assertEqual(grid.columns[0].width, 232)

    def test_empty_schema_paints_only_its_body_and_can_be_repopulated(self):
        app, host = App(), GridPaintHost()
        self.addCleanup(app.destroy)
        grid = DataGrid(parent=app, left=20, top=20, width=220, height=160)
        for columns, rows in (((), ()), ((GridColumn("name", "Name", 300),),
                                        (GridRow("one", ("Account",)),)),
                              ((), tuple(GridRow(str(i), ()) for i in range(20)))):
            grid.set_data(columns=columns, rows=rows)
            arrange(app, host)
            host.surfaces.clear()
            host.rects.clear()
            host.captions.clear()
            paint_tree(app, host)
            if columns:
                self.assertEqual(grid._painted_cells, 1)
                self.assertTrue(grid._scrollbars())
                self.assertTrue(host.captions)
            else:
                self.assertEqual(grid._painted_cells, 0)
                self.assertEqual(grid._header_height(), 0)
                self.assertEqual(grid._scrollbars(), [])
                self.assertFalse(host.captions)
                self.assertFalse(host.rects)
                self.assertEqual(host.surfaces[-1][0], grid.bounds)
                self.assertEqual(grid._scroll_x, 0)

    def test_scrollbars_resolve_each_other_using_the_space_inside_the_border(self):
        app, host = App(theme=Theme().styled(DataGrid, Style(border_width=3))), RecordingHost()
        self.addCleanup(app.destroy)
        columns = (GridColumn("name", "Name", 210),)
        rows = tuple(GridRow(str(i), (str(i),)) for i in range(4))
        grid = DataGrid(parent=app, width=220, height=157, columns=columns, rows=rows)
        arrange(app, host)
        self.assertEqual(grid._scrollbars(), [])
        grid.rows += (GridRow("extra", ("Extra",)),)
        self.assertEqual(tuple(bar.axis for bar in grid._scrollbars()), (1, 0))
        grid.set_data(columns=(replace(columns[0], width=215),), rows=rows)
        self.assertEqual(tuple(bar.axis for bar in grid._scrollbars()), (1, 0))
        grid.set_data(columns=columns, rows=rows)
        self.assertEqual(grid._scrollbars(), [])
        for width, height in ((0, 0), (10, 10), (220, 34), (220, 40)):
            grid.update(width=width, height=height)
            arrange(app, host)
            paint_tree(app, host)
            for bar in grid._scrollbars():
                self.assertEqual(bar.track.intersect(grid.bounds), bar.track)

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

    def test_percentage_parser_accepts_own_display_and_preserves_raw_entry(self):
        column = GridColumn("ratio", "Ratio", kind="number", prefix="Rate: ",
                            suffix=" annual", format_spec=",.2%")
        for spec in (".2%", "+.2%", ",.2%", "_.2%"):
            formatted = replace(column, format_spec=spec)
            for value in (0, 0.5, -0.125, 1234.5678):
                with self.subTest(spec=spec, value=value):
                    self.assertAlmostEqual(_parse_number(_display(value, formatted), formatted), value)
        for text, expected in (("0.5", 0.5), ("50", 50), ("1e-3", 0.001),
                               ("50.0%", 0.5), ("", None)):
            with self.subTest(text=text):
                self.assertEqual(_parse_number(text, column), expected)
        for text in ("Rate: 5,00.0% annual", "Rate: 50.0% daily", "50.0%%",
                     "Rate: 50.0 annual", "nan%", "inf%", "1e3%"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                _parse_number(text, column)
        with self.assertRaises(ValueError):
            _parse_number("50.0%", replace(column, format_spec=".2f"))

    async def test_copied_percentage_cell_pastes_back_with_displayed_precision(self):
        app, host = App(), RecordingHost()
        grid = DataGrid(parent=app, width=300, height=160,
            columns=(GridColumn("ratio", "Ratio", kind="number", editable=True,
                                prefix="Rate: ", suffix=" annual", format_spec=".1%"),),
            rows=(GridRow("a", (0.12567,)),), selected_key="a")
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            grid.focus()
            await runtime.router.clipboard("c")
            self.assertEqual(host.clipboard, "Rate: 12.6% annual")
            grid.begin_edit()
            self.assertEqual(runtime.popup.children[0].text, "0.12567")
            await runtime.router.clipboard("v")
            runtime.router.process(Input("key_down", key="Enter"))
            self.assertIsNone(runtime.popup)
            self.assertAlmostEqual(grid.rows[0].cells[0], 0.126)
            self.assertEqual(grid.selected_cell_text, host.clipboard)
            grid.begin_edit()
            runtime.router.process(Input("text", text="Rate: inf% annual", paste=True))
            runtime.router.process(Input("key_down", key="Enter"))
            self.assertIsNotNone(runtime.popup)
            self.assertAlmostEqual(grid.rows[0].cells[0], 0.126)
        finally:
            runtime._stop.set()
            await task
            app.destroy()

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
