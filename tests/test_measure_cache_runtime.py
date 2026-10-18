"""Measurement reuse respects host openings, metric changes and shutdown."""

from weakref import ref

from _ui_testcase import AsyncUIOwnerTestCase
from test_library import RecordingHost

from pysual import App, ChartSeries, Container, Control, LineChart, Rect, TextBox
from pysual._text_layout import TextLayoutCache
from pysual.layout import _size
from pysual.painting import Painter
from pysual.runtime import Runtime


class MetricHost(RecordingHost):
    resource_revision = 0

    def __init__(self, advance=40):
        super().__init__()
        self.advance = advance


class ChartMetricHost(MetricHost):
    """Opt into stable metrics while discarding them across host openings."""

    def __init__(self, advance=40):
        super().__init__(advance)
        self._text_layout_cache = TextLayoutCache()

    def open(self, title, width, height, resizable, scale):
        self._text_layout_cache.clear()
        super().open(title, width, height, resizable, scale)

    def close(self):
        self._text_layout_cache.clear()
        super().close()

    def measure(self, text, size, mono=False):
        width, height = super().measure(text, size, mono)
        return width * self.advance / 40, height


class HostMeasured(Control):
    def _initialize(self):
        super()._initialize()
        self._measure_calls = 0

    def measure(self, host):
        self._measure_calls += 1
        return host.advance, self.effective_row_height(24)


class MeasurementCacheRuntimeTests(AsyncUIOwnerTestCase):
    def setUp(self):
        self.app = App(width=640, height=400)
        # A valid fixed container can otherwise hide stale child geometry when
        # only the host's metrics change, without any model property mutation.
        self.panel = Container(parent=self.app, width=300, height=200)
        self.control = HostMeasured(parent=self.panel)
        self.runtimes = []

    async def asyncTearDown(self):
        for runtime in reversed(self.runtimes):
            if not runtime._cleaned:
                await runtime._cleanup()
        self.app.destroy()

    def start(self, host):
        runtime = Runtime(self.app, host)
        self.runtimes.append(runtime)
        runtime.start()
        return runtime

    def assert_released(self):
        for control in (self.app, self.panel, self.control):
            self.assertFalse(control._measure_cache)
            self.assertIsNone(control._measure_cache_host)
            self.assertIsNone(control._layout_measure_host)
            self.assertIsNone(control._layout_measure_environment)
            self.assertFalse(control._layout_valid)

    def test_open_discards_measurements_made_before_host_initialization(self):
        host = MetricHost(40)
        self.assertEqual(_size(self.control, host), (40, 24))
        host.advance = 80
        self.start(host)
        self.assertEqual(self.control.bounds.width, 80)

    async def test_close_releases_cached_hosts_and_same_host_reopen_remeasures(self):
        host = MetricHost(40)
        first = self.start(host)
        self.assertEqual(self.control.bounds.width, 40)
        await first._cleanup()
        self.assert_released()
        # A new opening can refresh metrics without changing host identity,
        # viewport, or an optional backend resource-revision counter.
        host.advance = 80
        second = self.start(host)
        self.assertEqual(self.control.bounds.width, 80)
        self.assertEqual(second.presentation.generation, first.presentation.generation + 1)

    async def test_reopen_on_different_host_rearranges_clean_descendants(self):
        first = self.start(MetricHost(40))
        await first._cleanup()
        self.start(MetricHost(90))
        self.assertEqual(self.control.bounds.width, 90)

    async def test_chart_axes_refresh_on_same_and_different_host_reopen(self):
        chart = LineChart(parent=self.app, width=560, height=300, show_legend=False,
            series=(ChartSeries("s", "S", ((0, 10000), (10, 10001))),))
        for same_host in (True, False):
            with self.subTest(same_host=same_host):
                ordinary = ChartMetricHost(40)
                first = self.start(ordinary)
                self.assertEqual(ordinary.texts[:5],
                    ["10000", "10000.25", "10000.5", "10000.75", "10001"])
                self.assertIsNotNone(chart._axis_layout)
                measured_revision = chart._measure_revision
                await first._cleanup()
                self.assertGreater(chart._measure_revision, measured_revision)
                wider = ordinary if same_host else ChartMetricHost(160)
                wider.advance = 160
                second = self.start(wider)
                self.assertEqual(wider.texts[:5], ["0", "0.25", "0.5", "0.75", "1"])
                self.assertIn("+10000", wider.texts)
                self.assertIsNotNone(chart._axis_layout)
                await second._cleanup()

    def test_chart_axes_discard_preopen_font_measurements(self):
        chart = LineChart(parent=self.app, width=560, height=300, show_legend=False,
            series=(ChartSeries("s", "S", ((0, 10000), (10, 10001))),))
        host = ChartMetricHost(40)
        chart.paint(Painter(host, Rect(0, 0, 560, 300), self.app.theme, chart))
        self.assertIsNotNone(chart._axis_layout)
        host.advance = 160
        self.start(host)
        self.assertEqual(host.texts[:5], ["0", "0.25", "0.5", "0.75", "1"])
        self.assertIn("+10000", host.texts)

    def test_destroyed_chart_releases_its_axis_measurement_host(self):
        chart = LineChart(parent=self.app, width=560, height=300, show_legend=False,
            series=(ChartSeries("s", "S", ((0, 10000), (10, 10001))),))
        host = ChartMetricHost(40)
        host_ref = ref(host)
        chart.paint(Painter(host, Rect(0, 0, 560, 300), self.app.theme, chart))
        self.assertIsNotNone(chart._axis_layout)
        chart.destroy()
        chart.destroy()  # Repeated destruction remains harmless.
        del host
        self.assertTrue(chart._disposed)
        self.assertIsNone(host_ref())

    def test_repaint_after_resource_change_remeasures_and_rearranges(self):
        host = MetricHost(40)
        runtime = self.start(host)
        calls = self.control._measure_calls
        runtime.invalidate()
        runtime._paint_frame()
        self.assertEqual(self.control._measure_calls, calls)
        host.advance = 80
        host.resource_revision += 1
        runtime.invalidate()
        runtime._paint_frame()
        self.assertGreater(self.control._measure_calls, calls)
        self.assertEqual(self.control.bounds.width, 80)

    def test_repaint_after_fixed_row_metric_change_rearranges(self):
        host = MetricHost()
        host.text_row_height = 16
        runtime = self.start(host)
        self.assertEqual(self.control.bounds.height, 16)
        host.text_row_height = 32
        runtime.invalidate()
        runtime._paint_frame()
        self.assertEqual(self.control.bounds.height, 32)

    def test_textbox_paint_host_does_not_replace_measurement_cache_identity(self):
        class TallFontHost(RecordingHost):
            def measure(self, text, size, mono=False):
                width, height = super().measure(text, size, mono)
                return width, height * 3

        ordinary, tall = RecordingHost(), TallFontHost()
        textbox = TextBox(parent=self.app, text="Different host metrics")
        original = _size(textbox, ordinary)
        textbox.paint(Painter(tall, Rect(0, 0, 220, 100), self.app.theme, textbox))
        # TextBox remembers the last paint host for document scrolling. That
        # independent state must not make its preferred-size cache trust entries
        # produced by the previous host, even with equal revision/viewport data.
        self.assertIs(textbox._measure_host, tall)
        measured = _size(textbox, tall)
        self.assertGreater(measured[1], original[1])
        self.assertEqual(measured, textbox.measure_available(tall))

    def test_first_frame_failure_releases_measurements(self):
        class FailedPresent(MetricHost):
            def present(self):
                raise RuntimeError("present failed")

        host = FailedPresent()
        with self.assertRaisesRegex(RuntimeError, "present failed"):
            self.start(host)
        self.assertTrue(host.closed)
        self.assert_released()
        self.assertIsNone(self.app._runtime)

    async def test_host_close_failure_still_releases_measurements(self):
        class FailedClose(MetricHost):
            def close(self):
                super().close()
                raise RuntimeError("close failed")

        runtime = self.start(FailedClose())
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            await runtime._cleanup()
        self.assert_released()
        self.assertIsNone(self.app._runtime)
