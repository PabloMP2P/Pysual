"""Custom measurements retain nested host metrics and retry semantics."""

from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Container, Control
from pysual.layout import _size, arrange


class RowControl(Control):
    def measure(self, host):
        return host.unit, self.effective_row_height(99)


def metric_host(unit=20, row_height=12, *, split=True):
    host = RecordingHost()
    host.unit = unit
    host.text_row_height = row_height
    host.resource_revision = 0
    if split:
        host.metrics_revision = 0
    return host


class MeasureContextCompatibilityTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=640, height=400)

    def tearDown(self):
        self.app.destroy()

    def test_nested_metric_change_refreshes_child_and_retries_outer_result(self):
        for split in (False, True):
            with self.subTest(split_metrics=split):
                host = metric_host(split=split)
                observations = []

                class Parent(Container):
                    def measure(self, host):
                        child = self.children[0]
                        result = _size(child, host)
                        if host.unit == 20:
                            host.unit, host.text_row_height = 35, 24
                            host.resource_revision += 1
                            if split:
                                host.metrics_revision += 1
                            observations.append(_size(child, host))
                            observations.append(self.effective_row_height(99))
                        return result

                parent = Parent(parent=self.app)
                RowControl(parent=parent)
                # A hook can change metrics while it works. Its returned result
                # belongs to the original context and must not survive a retry.
                self.assertEqual(_size(parent, host), (20, 12))
                self.assertEqual(observations, [(35, 24), 12])
                self.assertEqual(_size(parent, host), (35, 24))
                self.assertEqual(parent.effective_row_height(99), 99)
                parent.destroy()

    def test_nested_host_queries_restore_parent_metrics_and_refresh_child(self):
        host = metric_host()
        alternate = metric_host(unit=70, row_height=28)
        observations = []

        class Parent(Container):
            def measure(self, host):
                child = self.children[0]
                observations.append(_size(child, host))
                observations.append(_size(child, alternate))
                observations.append(self.effective_row_height(99))
                return _size(child, host)

        parent = Parent(parent=self.app)
        child = RowControl(parent=parent)
        self.assertEqual(_size(parent, host), (20, 12))
        self.assertEqual(observations, [(20, 12), (70, 28), 12])
        self.assertEqual(_size(child, alternate), (70, 28))
        self.assertEqual(_size(child, host), (20, 12))
        self.assertEqual(parent.effective_row_height(99), 99)

    def test_failed_nested_query_restores_context_for_direct_measure_available(self):
        host = metric_host()
        alternate = metric_host(unit=70, row_height=28)
        observations = []

        class Inner(RowControl):
            def measure(self, host):
                observations.append(self.effective_row_height(99))
                if host.unit == 70:
                    raise ValueError("metrics temporarily unavailable")
                return super().measure(host)

        inner = Inner(parent=self.app)

        class Outer(Control):
            def measure(self, host):
                observations.append(self.effective_row_height(99))
                try:
                    _size(inner, alternate)
                except ValueError:
                    observations.append(self.effective_row_height(99))
                return inner.measure_available(host)

        outer = Outer(parent=self.app)
        self.assertEqual(outer.measure_available(host), (20, 12))
        self.assertEqual(observations, [12, 28, 12, 12])
        self.assertEqual(outer.effective_row_height(99), 99)
        alternate.unit = 71
        self.assertEqual(_size(inner, alternate), (71, 28))
        self.assertEqual(inner.effective_row_height(99), 99)

    def test_direct_measure_available_uses_requested_host_without_changing_geometry(self):
        arranged = metric_host(unit=20, row_height=12)
        alternate = metric_host(unit=70, row_height=28)
        child = RowControl(parent=self.app)
        arrange(self.app, arranged)
        bounds = child.bounds
        self.assertEqual(child.measure_available(alternate), (70, 28))
        self.assertEqual(_size(child, alternate), (70, 28))
        self.assertEqual(child.bounds, bounds)
        self.assertEqual(child.effective_row_height(99), 12)
        self.assertEqual(_size(child, arranged), (20, 12))
        self.assertEqual(child.bounds, bounds)

    def test_legacy_host_query_preserves_other_custom_resource_dependencies(self):
        host = metric_host()
        host.image_width = 20
        legacy = metric_host(split=False)

        class ResourceControl(Control):
            def measure(self, host):
                return host.image_width, 10

        child = ResourceControl(parent=self.app)
        branch = Container(parent=self.app, layout="stack")
        RowControl(parent=branch)
        self.assertEqual(_size(child, host), (20, 10))
        # Public cross-host content measurement sizes a different branch.
        branch.measure_available(legacy)
        host.image_width = 60
        host.resource_revision += 1
        self.assertEqual(_size(child, host), (60, 10))
