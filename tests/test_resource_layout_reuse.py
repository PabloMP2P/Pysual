"""Resource refresh reruns sizing while retaining unchanged leaf placement."""
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase
from test_retained_paint import PatchRecordingHost

from pysual import App, Container, Control, Label, Rect
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree
from pysual.runtime import Runtime
from pysual.schema import Dirty


class ResourceHost(PatchRecordingHost):
    metrics_revision = 0

    def __init__(self):
        super().__init__()
        self.measurements = 0
        self.image_extent = 30

    def measure(self, text, size, mono=False):
        self.measurements += 1
        return len(text) * 10, 20


class ResourceSized(Control):
    def _initialize(self):
        super()._initialize()
        self._measurements = self._paints = 0

    def measure(self, host):
        self._measurements += 1
        return host.image_extent, 20

    def paint(self, painter):
        self._paints += 1
        painter.rect(Rect(0, 0, painter.width, painter.height), "#ff0000")


class ResourceLayoutReuseTests(UIOwnerTestCase):
    def setUp(self):
        self.root = App(width=640, height=400)
        self.host = ResourceHost()
        self.runtime = Runtime(self.root, self.host)
        self.root._runtime = self.runtime
        self.runtime._retained_paint = RetainedPaintTree(self.host)

    def tearDown(self):
        self.root._runtime = None
        self.root.destroy()

    def observed_placements(self):
        return patch.object(self.runtime.router, "layout_control",
                            wraps=self.runtime.router.layout_control)

    def test_resource_refresh_remeasures_without_replacing_unchanged_leaves(self):
        first = Label(parent=self.root, text="First")
        second = Label(parent=self.root, text="Second", top=40)
        arrange(self.root, self.host)
        before = self.host.measurements
        bounds = first.bounds, second.bounds

        self.host.resource_revision += 1
        with self.observed_placements() as placed:
            arrange(self.root, self.host)

        self.assertGreater(self.host.measurements, before)
        self.assertEqual((first.bounds, second.bounds), bounds)
        visited = [call.args[0] for call in placed.call_args_list]
        self.assertIn(self.root, visited)
        self.assertNotIn(first, visited)
        self.assertNotIn(second, visited)

    def test_changed_resource_size_places_leaf_and_updates_hit_geometry(self):
        child = ResourceSized(parent=self.root, cache_paint=True)
        arrange(self.root, self.host)
        self.assertEqual(child.bounds.width, 30)

        self.host.image_extent = 80
        self.host.resource_revision += 1
        with self.observed_placements() as placed:
            arrange(self.root, self.host)

        self.assertEqual(child.bounds.width, 80)
        self.assertIn(child, [call.args[0] for call in placed.call_args_list])
        self.assertIs(self.runtime.router.hit(self.root, 60, 10), child)

    def test_existing_leaf_layout_dirt_survives_resource_refresh(self):
        child = ResourceSized(parent=self.root)
        arrange(self.root, self.host)
        child.invalidate(Dirty.MEASURE)
        self.host.resource_revision += 1

        with self.observed_placements() as placed:
            arrange(self.root, self.host)

        self.assertEqual(child.bounds.width, 30)
        self.assertIn(child, [call.args[0] for call in placed.call_args_list])

    def test_fixed_container_arranger_reads_captured_resource_host(self):
        host = self.host

        class Placement(Container):
            def arrange_children(self, area, measure):
                self._arranged_resource = host.image_extent
                return {child: Rect(area.x, area.y, host.image_extent, 20)
                        for child in self.children}

        panel = Placement(parent=self.root, width=200, height=100)
        child = Label(parent=panel, text="Child")
        arrange(self.root, host)
        bounds = panel.bounds
        host.image_extent = 80
        host.resource_revision += 1
        arrange(self.root, host)

        self.assertEqual(panel.bounds, bounds)
        self.assertEqual(panel._arranged_resource, 80)
        self.assertEqual(child.bounds.width, 80)

    def test_unchanged_geometry_still_remeasures_and_repaints_on_resources(self):
        child = ResourceSized(parent=self.root, cache_paint=True)
        self.runtime._paint_frame()
        before = child._measurements, child._paints
        bounds = child.bounds
        self.host.resource_revision += 1

        with self.observed_placements() as placed:
            self.runtime._paint_frame()

        self.assertEqual(child.bounds, bounds)
        self.assertGreater(child._measurements, before[0])
        self.assertGreater(child._paints, before[1])
        self.assertNotIn(child, [call.args[0] for call in placed.call_args_list])
