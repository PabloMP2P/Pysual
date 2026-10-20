"""Explicit metric-only reuse preserves conservative default sizing and painting."""
import copy
from types import SimpleNamespace
from unittest.mock import patch

from _terminal import MemoryTerminal
from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost
from test_retained_paint import PatchRecordingHost

from pysual import App, Container, Control, Label, Rect
from pysual._paint_state import measurement_uses_resources
from pysual.backends._web_pyodide import BundledSVGHost
from pysual.backends._web_svg import SVGRenderer
from pysual.backends.native import NativeHost
from pysual.backends.term_text import TermTextHost
from pysual.backends.terminal import TerminalHost
from pysual.layout import _clear_measurements, _size, arrange
from pysual.painting import RetainedPaintTree
from pysual.runtime import Runtime
from pysual.schema import Dirty


class MetricHost(RecordingHost):
    metrics_revision = 0
    resource_revision = 0
    unit = 10
    image_width = 20

    def __init__(self):
        super().__init__()
        self.measurements = 0

    def measure(self, text, size, mono=False):
        self.measurements += 1
        return len(text) * self.unit, 20


class Custom(Control):
    def _initialize(self):
        super()._initialize()
        self._measures = self._paints = 0

    def measure(self, host):
        self._measures += 1
        return host.image_width, host.unit

    def paint(self, painter):
        self._paints += 1
        painter.rect(Rect(0, 0, painter.width, painter.height), "#ff0000")


class MetricResourceSplitTests(UIOwnerTestCase):
    def setUp(self):
        self.root = App(width=640, height=400)
        self.host = MetricHost()

    def tearDown(self):
        self.root._runtime = None
        self.root.destroy()

    def test_builtin_default_remeasures_after_resource_and_metric_changes(self):
        child = Label(parent=self.root, text="abc")
        arrange(self.root, self.host)
        self.assertEqual(child.bounds.width, 30)
        before = self.host.measurements
        self.host.resource_revision += 1
        arrange(self.root, self.host)
        self.assertGreater(self.host.measurements, before)
        before = self.host.measurements
        self.host.unit = 15
        self.host.metrics_revision += 1
        self.host.resource_revision += 1
        arrange(self.root, self.host)
        self.assertEqual(child.bounds.width, 45)
        self.assertGreater(self.host.measurements, before)

    def test_custom_default_and_explicit_dependency_refresh(self):
        for declaration in (None, True):
            child = Custom(parent=self.root, measure_resources=declaration)
            self.assertEqual(_size(child, self.host)[0], self.host.image_width)
            self.host.image_width += 10
            self.host.resource_revision += 1
            self.assertEqual(_size(child, self.host)[0], self.host.image_width)
            self.assertEqual(child._measures, 2)

    def test_custom_opt_in_keeps_sizes_until_metrics_or_explicit_invalidation(self):
        child = Custom(parent=self.root, measure_resources=False)
        self.assertEqual(_size(child, self.host), (20, 10))
        self.host.resource_revision += 1
        self.assertEqual(_size(child, self.host), (20, 10))
        self.assertEqual(child._measures, 1)
        self.host.unit = 30
        self.host.metrics_revision += 1
        self.assertEqual(_size(child, self.host), (20, 30))
        self.host.image_width = 50
        child.invalidate(Dirty.MEASURE)
        self.assertEqual(_size(child, self.host), (50, 30))

    def test_legacy_host_keeps_combined_generation_even_for_opt_in(self):
        host = RecordingHost()
        host.resource_revision, host.image_width, host.unit = 0, 20, 10
        child = Custom(parent=self.root, measure_resources=False)
        self.assertEqual(_size(child, host)[0], 20)
        host.image_width, host.resource_revision = 50, 1
        self.assertEqual(_size(child, host)[0], 50)

    def test_cached_opt_in_ancestor_cannot_hide_custom_descendant(self):
        parent = Container(parent=self.root, layout="stack", padding=0, measure_resources=False)
        nested = Container(parent=parent, layout="stack", padding=0)
        child = Custom(parent=nested)
        self.assertEqual(_size(parent, self.host)[0], 20)
        self.host.image_width = 70
        self.host.resource_revision += 1
        self.assertEqual(_size(parent, self.host)[0], 70)
        self.assertEqual(child._measures, 2)

    def test_custom_subclass_keeps_conservative_default_without_overriding_measure(self):
        class CustomLabel(Label):
            pass
        child = CustomLabel(parent=self.root, text="abc")
        _size(child, self.host)
        before = self.host.measurements
        self.host.resource_revision += 1
        _size(child, self.host)
        self.assertGreater(self.host.measurements, before)

    def test_replaced_builtin_method_and_instance_shadow_keep_resource_dependency(self):
        child = Label(parent=self.root, text="abc")
        _size(child, self.host)
        def measure(control, host):
            return host.image_width, 20
        with patch.object(Label, "measure", measure):
            self.host.resource_revision += 1
            self.assertEqual(_size(child, self.host)[0], 20)
            self.host.image_width = 80
            self.host.resource_revision += 1
            self.assertEqual(_size(child, self.host)[0], 80)
        object.__setattr__(child, "measure", lambda host: (host.image_width, 20))
        self.host.image_width = 90
        self.host.resource_revision += 1
        self.assertEqual(_size(child, self.host)[0], 90)

    def test_only_explicit_resource_opt_out_changes_the_policy(self):
        child = Label(parent=self.root, text="abc")
        self.assertTrue(measurement_uses_resources(child))
        with patch.object(Label, "_control_height", lambda *args: 99):
            self.assertTrue(measurement_uses_resources(child))
        child.measure_resources = False
        self.assertFalse(measurement_uses_resources(child))

    def test_resource_change_during_measurement_cannot_publish_stale_cache(self):
        class Changing(Custom):
            def measure(self, host):
                result = super().measure(host)
                if self._measures == 1:
                    host.resource_revision += 1
                    host.image_width = 80
                return result
        child = Changing(parent=self.root)
        self.assertEqual(_size(child, self.host)[0], 20)
        self.assertEqual(child._measure_cache, [])
        self.assertEqual(_size(child, self.host)[0], 80)

    def test_nested_query_observes_resource_change_inside_parent_measurement(self):
        class Parent(Container):
            def measure(self, host):
                child = self.children[0]
                before = _size(child, host)
                host.resource_revision += 1
                host.image_width = before[0] + 40
                return _size(child, host)
        parent = Parent(parent=self.root)
        child = Custom(parent=parent)
        self.assertEqual(_size(child, self.host)[0], 20)
        self.assertEqual(_size(parent, self.host)[0], 60)
        self.assertEqual(parent._measure_cache, [])

    def test_builtin_can_explicitly_request_conservative_resource_invalidation(self):
        child = Label(parent=self.root, text="abc", measure_resources=True)
        _size(child, self.host)
        before = self.host.measurements
        self.host.resource_revision += 1
        _size(child, self.host)
        self.assertGreater(self.host.measurements, before)

    def test_resource_invalidation_survives_failed_measurement(self):
        class Failing(Custom):
            def measure(self, host):
                if host.image_width == 30:
                    raise ValueError("temporarily unavailable")
                return super().measure(host)
        child = Failing(parent=self.root)
        _size(child, self.host)
        self.host.image_width, self.host.resource_revision = 30, 1
        with self.assertRaises(ValueError):
            _size(child, self.host)
        self.host.image_width = 40
        self.assertEqual(_size(child, self.host)[0], 40)

    def test_reparent_copy_clear_and_host_reuse_do_not_retain_stale_context(self):
        detached = Container(layout="stack", padding=0, measure_resources=False)
        child = Custom(parent=detached)
        _size(detached, self.host)
        clone = copy.deepcopy(detached)
        self.addCleanup(clone.destroy)
        self.assertIsNone(clone._measure_resource_context)
        self.root.add(detached)
        self.assertIsNone(detached._measure_resource_context)
        self.host.image_width, self.host.resource_revision = 60, 1
        self.assertEqual(_size(detached, self.host)[0], 60)
        other = MetricHost()
        other.image_width = 90
        self.assertEqual(_size(detached, other)[0], 90)
        self.assertEqual(_size(detached, self.host)[0], 60)
        _clear_measurements(self.root)
        for node in (self.root, detached, child):
            self.assertIsNone(node._measure_resource_context)

    def test_runtime_preserves_custom_geometry_and_repaints_metric_only_controls(self):
        class Host(PatchRecordingHost):
            metrics_revision = 0
            unit = 10
            image_width = 20
        host = Host()
        runtime = Runtime(self.root, host)
        self.root._runtime = runtime
        runtime._retained_paint = RetainedPaintTree(host)
        custom = Custom(parent=self.root)
        metric_only = Custom(parent=self.root, top=50, measure_resources=False)
        runtime._paint_frame()
        measured = metric_only._measures
        paints = metric_only._paints
        host.image_width, host.resource_revision = 70, 1
        runtime._paint_frame()
        self.assertEqual(custom.bounds.width, 70)
        self.assertEqual(metric_only.bounds.width, 20)
        self.assertEqual(metric_only._measures, measured)
        self.assertGreater(metric_only._paints, paints)

    def test_custom_arrangement_remains_resource_sensitive(self):
        host = self.host
        class Placement(Container):
            def arrange_children(self, area, measure):
                return {child: Rect(area.x, area.y, host.image_width, 20)
                        for child in self.children}
        panel = Placement(parent=self.root, width=200, height=100)
        child = Label(parent=panel, text="abc")
        arrange(self.root, host)
        self.assertEqual(child.bounds.width, 20)
        host.image_width, host.resource_revision = 90, 1
        arrange(self.root, host)
        self.assertEqual(child.bounds.width, 90)

    def test_web_and_terminal_image_reloads_do_not_advance_metrics(self):
        svg = SVGRenderer()
        metrics = svg.metrics_revision
        svg.reload_image("missing.png")
        self.assertEqual(svg.metrics_revision, metrics)
        self.assertGreater(svg.resource_revision, 0)
        svg.reset_scene("new", 640, 400)
        self.assertGreater(svg.metrics_revision, metrics)
        terminal = TermTextHost(io=MemoryTerminal(), probe_timeout=0)
        terminal.reload_image("missing.png")
        self.assertEqual(terminal.metrics_revision, 0)
        wrapper = TerminalHost()
        wrapper._host = terminal
        self.assertEqual(wrapper.metrics_revision, terminal.metrics_revision)
        terminal.close()

    def test_bundled_bridge_font_generation_is_not_mistaken_for_image_only(self):
        host = object.__new__(BundledSVGHost)
        SVGRenderer.__init__(host)
        host.bridge = SimpleNamespace(resourceRevision=2)
        self.assertEqual(host.metrics_revision, 2)
        host.reload_image("missing.png")
        self.assertEqual(host.metrics_revision, 2)
        host.bridge.resourceRevision += 1
        self.assertEqual(host.metrics_revision, 3)

    def test_native_image_ack_preserves_metrics_but_unknown_changes_do_not(self):
        host = NativeHost()
        info = {"width": 640, "height": 400, "scale": 1, "resource_revision": 5}
        host._accept_viewport(info)
        host.info.update(info)
        before = host.metrics_revision
        with patch.object(host, "_request", return_value={**info, "resource_revision": 6}):
            host.reload_image("missing.png")
        self.assertEqual(host.metrics_revision, before)
        host._accept_viewport({**info, "resource_revision": 8}, image_only=True)
        self.assertEqual(host.metrics_revision, before + 1)
        host._accept_viewport({**info, "resource_revision": 9})
        self.assertEqual(host.metrics_revision, before + 2)
        host._accept_viewport({**info, "resource_revision": 10, "scale": 2}, image_only=True)
        self.assertEqual(host.metrics_revision, before + 3)
