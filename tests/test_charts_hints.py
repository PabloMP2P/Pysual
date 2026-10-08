"""Bounded chart rendering and noninteractive hint lifetimes."""

import asyncio
import unittest
from _ui_testcase import AsyncUIOwnerTestCase
from dataclasses import FrozenInstanceError
from math import isfinite
from unittest.mock import patch

from test_library import RecordingHost, eventually

from pysual import App, ChartSeries, LineChart
from pysual.charts import ChartSlice, DonutChart
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.runtime import Runtime


class ChartTests(unittest.TestCase):
    def test_donut_center_text_is_complete_or_absent_in_a_short_chart(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        names = ("North", "South", "East", "West")
        chart = app.donut_chart(
            title="Revenue", width=360,
            slices=tuple(ChartSlice(name, name, 1) for name in names),
        )
        legend_texts = ["Revenue", *(text for name in names for text in (name, "25%"))]
        for center in (None, "5740"):
            chart.center_text = center
            for height in (100, 150, 340):
                with self.subTest(center=center, height=height):
                    chart.height = height
                    arrange(app, host)
                    paint_tree(app, host)
                    expected = legend_texts + ([center or "100%"] if height == 340 else [])
                    self.assertEqual(host.texts, expected)
                    self.assertGreater(chart._painted_segments, 0)

    def test_short_line_chart_keeps_title_and_a_legend_when_it_fits(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        chart = app.line_chart(title="Revenue", width=320, height=70,
                               series=(ChartSeries("sales", "Sales", ((0, 1), (1, 2))),))
        for height, legend in ((70, True), (40, False), (300, True)):
            with self.subTest(height=height):
                chart.height = height
                arrange(app, host)
                paint_tree(app, host)
                self.assertIn("Revenue", host.texts)
                self.assertEqual("Sales" in host.texts, legend)
                self.assertEqual(chart._painted_segments > 0, height == 300)
        chart.series, chart.height = (), 60
        arrange(app, host)
        paint_tree(app, host)
        self.assertIn("Revenue", host.texts)

    def test_wide_donut_legend_packs_short_labels_without_overlap(self):
        app, host = App(width=900, height=400), RecordingHost()
        self.addCleanup(app.destroy)
        chart = app.donut_chart(
            slices=tuple(ChartSlice(str(i), title, value) for i, (title, value)
                         in enumerate((("North", 32), ("South", 31), ("West", 37)))),
            width=868, height=300, center_mode="none",
        )
        arrange(app, host)
        with patch.object(host, "text", wraps=host.text) as draws:
            paint_tree(app, host)
        text = {call.args[0]: call.args for call in draws.call_args_list}
        self.assertLess(text["South"][1] - text["North"][1], 120)
        self.assertEqual(text["North"][1], text["West"][1])
        self.assertGreater(text["West"][2], text["North"][2])
        self.assertGreater(text["South"][1] - 16,
                           text["32%"][1] + host.measure("32%", 11)[0])

    def test_line_chart_ticks_distinguish_close_values_after_series_updates(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        chart = LineChart(parent=app, width=560, height=300, show_legend=False)
        arrange(app, host)
        for low, high in ((10000, 10001), (-10001, -10000)):
            chart.series = (ChartSeries("s", "Values", ((1791028800, low), (1791028860, high))),)
            reduced = chart._points
            paint_tree(app, host)
            self.assertEqual([float(text) for text in host.texts[:5]],
                             [low, low + .25, low + .5, low + .75, high])
            self.assertEqual([float(text) for text in host.texts[5:8]],
                             [1791028800, 1791028830, 1791028860])
            self.assertEqual(chart._extent, (1791028800, 1791028860, low, high))
            self.assertIs(chart._points, reduced)

    def test_line_chart_compact_offsets_and_exponents_are_explicit(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        chart = LineChart(parent=app, width=240, height=260, show_legend=False)
        arrange(app, host)
        chart.series = (ChartSeries("s", "Time", ((1791028800, 0), (1791028860, 1))),)
        with patch.object(host, "text", wraps=host.text) as draws:
            paint_tree(app, host)
        self.assertEqual(host.texts[5:8], ["0", "30", "60"])
        self.assertIn("+1791028800", host.texts[8:])
        for call in draws.call_args_list:
            text, x = call.args[:2]
            self.assertGreaterEqual(x, chart.bounds.x)
            self.assertLessEqual(x + host.measure(text, 11)[0], chart.bounds.right)
        chart.series = (ChartSeries("s", "Huge", ((-1.7e308, 0), (1.7e308, 1))),)
        paint_tree(app, host)
        self.assertEqual(host.texts[5:8], ["-1.7", "0", "1.7"])
        self.assertIn("×1e308", host.texts[8:])
        chart.series = (ChartSeries("s", "Constant", ((2, 10000),)),)
        paint_tree(app, host)
        self.assertEqual(len(set(host.texts[:5])), 1)

    def test_donut_slices_validate_and_extreme_values_stay_bounded(self):
        slices = (ChartSlice("a", "North", 1e308), ChartSlice("b", "South", 1e308))
        with self.assertRaises(FrozenInstanceError):
            slices[0].value = 4
        for value in (-1, float("nan"), float("inf"), True, "one"):
            with self.assertRaises((ValueError, TypeError)):
                ChartSlice("bad", "Bad", value)
        for color in ("red", "#GGGGGG"):
            with self.assertRaises(ValueError):
                ChartSlice("bad", "Bad", 1, color)
        chart = DonutChart(slices=slices)
        try:
            self.assertEqual(chart._fractions, (0.5, 0.5))
            self.assertLessEqual(sum(len(arc) for arc in chart._arcs), 520)
            previous = chart.slices
            for invalid in (
                (slices[0], slices[0]),
                tuple(ChartSlice(str(i), str(i), i) for i in range(9)),
            ):
                with self.assertRaises(ValueError):
                    chart.slices = invalid
                self.assertIs(chart.slices, previous)
            chart.slices = (ChartSlice("a", "Tiny", 5e-324),)
            self.assertEqual(chart._fractions, (1.0,))
        finally:
            chart.destroy()

    def test_donut_uses_shared_primitives_handles_empty_and_elides_legend(self):
        app, host = App(width=500, height=400), RecordingHost()
        try:
            chart = DonutChart(
                parent=app,
                slices=tuple(
                    ChartSlice(str(i), "Long region " * 10, i) for i in range(8)
                ),
                width=360,
                height=340,
                title="Revenue",
            )
            arrange(app, host)
            with patch.object(host, "line", wraps=host.line) as lines:
                paint_tree(app, host)
            self.assertLessEqual(chart._painted_segments, 520)
            self.assertGreater(chart._painted_segments, 0)
            for call in lines.call_args_list:
                self.assertTrue(all(isfinite(value) for value in call.args[:4]))
                self.assertGreaterEqual(call.args[5], 0)
            self.assertTrue(any("…" in text for text in host.texts))
            arcs = chart._arcs
            paint_tree(app, host)
            self.assertIs(chart._arcs, arcs)
            for slices in ((), (ChartSlice("a", "Empty", 0),)):
                chart.slices = slices
                host.texts.clear()
                paint_tree(app, host)
                self.assertEqual(chart._painted_segments, 0)
                self.assertIn("No data", host.texts)
            chart.width, chart.height = 40, 30
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(chart._painted_segments, 0)
        finally:
            app.destroy()

    def test_donut_keeps_percentages_visible_with_long_legend_titles(self):
        app, host = App(width=400, height=380), RecordingHost()
        try:
            names = ("Research and Development", "Sales and Marketing",
                     "Customer Support", "Administration")
            chart = app.donut_chart(
                slices=tuple(ChartSlice(str(i), name, value)
                             for i, (name, value) in enumerate(zip(names, (40, 30, 20, 10)))),
                left=20, top=20, width=360, height=340, center_mode="none",
            )
            arrange(app, host)
            with patch.object(host, "text", wraps=host.text) as draws:
                paint_tree(app, host)
            calls = draws.call_args_list
            self.assertEqual([call.args[0] for call in calls[1::2]],
                             ["40%", "30%", "20%", "10%"])
            self.assertIn("…", calls[0].args[0])
            for index in range(4):
                title, value = calls[index * 2:index * 2 + 2]
                title_text, title_x, title_y = title.args[:3]
                value_text, value_x, value_y = value.args[:3]
                title_width = host.measure(title_text, 11)[0]
                value_width = host.measure(value_text, 11)[0]
                self.assertEqual(title_y, value_y)
                self.assertLessEqual(title_x + title_width + 6, value_x)
                slot_right = chart.bounds.x + 12 + (index % 2 + 1) * 168
                self.assertLessEqual(value_x + value_width, slot_right - 6)
        finally:
            app.destroy()

    def test_limits_reduction_preserves_spikes_and_extreme_ranges(self):
        points = tuple(
            (i, 100 if i == 5123 else -80 if i == 6789 else 0) for i in range(10_000)
        )
        chart = LineChart(series=(ChartSeries("a", "Signal", points),))
        try:
            self.assertIn(points[5123], chart._points[0])
            self.assertIn(points[6789], chart._points[0])
            self.assertEqual(chart._points[0][0], points[0])
            self.assertEqual(chart._points[0][-1], points[-1])
            self.assertLessEqual(len(chart._points[0]), 2048)
            for invalid in (((1, 2), (0, 3)), ((0, float("nan")),)):
                with self.assertRaises((TypeError, ValueError)):
                    ChartSeries("bad", "Bad", invalid)
            with self.assertRaises(ValueError):
                chart.series = (chart.series[0], chart.series[0])
            self.assertEqual(len(chart.series), 1)
            with self.assertRaises(ValueError):
                ChartSeries("bad", "Bad", (), "red")
        finally:
            chart.destroy()
        app, host = App(width=500, height=320), RecordingHost()
        try:
            chart = app.line_chart(
                series=(ChartSeries("a", "Signal", points),), width=480, height=300
            )
            arrange(app, host)
            paint_tree(app, host)
            self.assertLessEqual(chart._painted_segments, 2048)
            reduced = chart._points
            paint_tree(app, host)
            self.assertIs(chart._points, reduced)
            for values in (
                ((0.0, 0.0),),
                ((-1e308, -1e308), (1e308, 1e308)),
                ((0.0, 0.0), (5e-324, 5e-324)),
                (),
            ):
                chart.series = (ChartSeries("extreme", "Extreme", values),)
                paint_tree(app, host)
        finally:
            app.destroy()


class HintTests(AsyncUIOwnerTestCase):
    async def test_keyboard_hints_follow_navigation_and_dismiss_without_rearming(self):
        app, host = App(width=400, height=280), RecordingHost()
        first = app.button(icon="undo", tooltip="Reset values", left=10, top=220, width=40)
        second = app.button(icon="folder", tooltip="Open details", left=70, top=220, width=40)
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            with patch("pysual.hints.monotonic", return_value=100) as clock:
                async def navigate(owner, *, shift=False):
                    host.events.extend([
                        Input("key_down", key="Tab", shift=shift),
                        Input("key_up", key="Tab", shift=shift),
                    ])
                    await eventually(lambda: runtime.hints.owner is owner)
                    self.assertIs(runtime.router.focus, owner)
                    self.assertIsNone(runtime.hints.control)
                    clock.return_value += 0.6
                    await eventually(lambda: runtime.hints.control is not None)
                    self.assertEqual(runtime.hints.control.text, owner.tooltip)
                    self.assertLessEqual(runtime.hints.control.bounds.bottom, owner.bounds.y)
                    self.assertIsNot(
                        runtime.router.hit(app, runtime.hints.control.bounds.x + 1,
                                           runtime.hints.control.bounds.y + 1),
                        runtime.hints.control,
                    )

                await navigate(first)
                await navigate(second)
                await navigate(first, shift=True)
                for event in (
                    Input("key_down", key="Escape"),
                    Input("key_down", key="Enter"),
                    Input("text", text="a"),
                    Input("blur"),
                ):
                    host.events.append(event)
                    await eventually(lambda: runtime.hints.control is None)
                    clock.return_value += 1
                    await asyncio.sleep(0.025)
                    self.assertIsNone(runtime.hints.owner)
                    self.assertIsNone(runtime.hints.control)
                    await navigate(second if runtime.router.focus is first else first)
                owner = runtime.router.focus
                owner.visible = False
                await eventually(lambda: runtime.hints.control is None)
                owner.visible = True
                box = second.bounds
                host.events.append(Input("pointer_move", box.x + 10, box.y + 10))
                await eventually(lambda: runtime.hints.owner is second)
                clock.return_value += 0.6
                await eventually(lambda: runtime.hints.control is not None)
                self.assertEqual(runtime.hints.control.text, "Open details")
        finally:
            runtime._stop.set()
            await task

    async def test_multiline_tab_does_not_arm_hint_but_control_tab_does(self):
        app, host = App(), RecordingHost()
        entry = app.text_box(multiline=True, tooltip="Edit notes", width=240, height=120)
        button = app.button(tooltip="Save notes", left=260)
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            entry.focus()
            with patch("pysual.hints.monotonic", return_value=100) as clock:
                host.events.append(Input("key_down", key="Tab"))
                await eventually(lambda: not host.events)
                clock.return_value = 100.6
                await asyncio.sleep(0.025)
                self.assertIs(runtime.router.focus, entry)
                self.assertIsNone(runtime.hints.owner)
                self.assertIsNone(runtime.hints.control)
                host.events.extend([
                    Input("key_down", key="Tab", ctrl=True),
                    Input("key_up", key="Tab", ctrl=True),
                ])
                await eventually(lambda: runtime.hints.owner is button)
                clock.return_value = 101.2
                await eventually(lambda: runtime.hints.control is not None)
                button.destroy()
                await eventually(lambda: runtime.hints.control is None)
        finally:
            runtime._stop.set()
            await task

    async def test_dashboard_filters_edits_and_fits_small_viewport(self):
        from examples.dashboard import Dashboard

        app, host = Dashboard(), RecordingHost()
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            app.width, app.height = 640, 480
            await eventually(
                lambda: app.bounds.width == 640 and app.bounds.height == 480
            )
            self.assertTrue(
                all(child.bounds.bottom <= app.bounds.bottom for child in app.children)
            )
            app.query.text = "North"
            await eventually(lambda: len(app.grid.rows) == 2)
            self.assertEqual([slice_.key for slice_ in app.chart.slices], ["North"])
            app.grid.set_cell("0", "revenue", 2400, origin="user")
            await eventually(lambda: app._source[0].cells[2] == 2400)
            app.query.text = ""
            await eventually(lambda: len(app.grid.rows) == 6)
            self.assertEqual(app.grid.rows[0].cells[2], 2400)
            app.add_account.activate()
            await eventually(lambda: len(app.grid.rows) == 7)
            self.assertEqual(len(app._source), 7)
        finally:
            runtime._stop.set()
            await task

    async def test_delay_focus_input_leave_and_owner_disposal(self):
        class Demo(App):
            def build(self):
                self.owner = self.button(
                    text="Hint",
                    tooltip="A long hint " * 30,
                    left=240,
                    top=220,
                    width=140,
                    height=40,
                )
                self.editor = self.text_box(left=10, top=10, width=200)

        app, host = Demo(width=400, height=280), RecordingHost()
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            app.editor.focus()
            box = app.owner.bounds
            with patch("pysual.hints.monotonic", return_value=100) as clock:
                host.events.append(Input("pointer_move", box.x + 10, box.y + 10))
                await eventually(lambda: runtime.hints.owner is app.owner)
                self.assertIsNone(runtime.hints.control)
                clock.return_value = 100.6
                await eventually(lambda: runtime.hints.control is not None)
                hint = runtime.hints.control
                self.assertIsNotNone(hint)
                self.assertTrue(app.editor.focused)
                self.assertLessEqual(hint.bounds.right, app.bounds.right)
                self.assertLessEqual(hint.bounds.bottom, box.y)
                self.assertIsNot(
                    runtime.router.hit(app, hint.bounds.x + 1, hint.bounds.y + 1), hint
                )
                host.events.append(Input("pointer_leave"))
                await eventually(lambda: runtime.hints.control is None)
                self.assertIsNone(runtime.hints.control)
                self.assertTrue(app.editor.focused)
                host.events.append(Input("pointer_move", box.x + 10, box.y + 10))
                await eventually(lambda: runtime.hints.owner is app.owner)
                clock.return_value = 101.2
                await eventually(lambda: runtime.hints.control is not None)
                self.assertIsNotNone(runtime.hints.control)
                app.owner.destroy()
                await eventually(lambda: runtime.hints.control is None)
                self.assertIsNone(runtime.hints.control)
        finally:
            runtime._stop.set()
            await task
