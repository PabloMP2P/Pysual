"""Preferred-size reuse preserves custom hooks and explicit invalidation."""

import unittest

from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Container, Control, Rect, prop
from pysual._engine import call
from pysual.errors import LifecycleError
from pysual.layout import _size, arrange
from pysual.painting import paint_tree
from pysual.schema import Dirty


class Measured(Control):
    caption: str = prop(default="four", affects=Dirty.MEASURE | Dirty.PAINT)

    def _initialize(self):
        super()._initialize()
        self._measure_calls = 0

    def measure(self, host):
        self._measure_calls += 1
        return len(self.caption) * 10, 20


class Available(Measured):
    def measure_available(self, host, *, width=None, height=None):
        self._measure_calls += 1
        preferred = len(self.caption) * 10
        return min(preferred, width or preferred), 40 if width and width < preferred else 20


class MeasurementCacheTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App()
        self.host = RecordingHost()

    def tearDown(self):
        self.app.destroy()

    def size(self, control, **limits):
        return _size(control, self.host, **limits)

    def test_custom_measure_reuses_clean_siblings_after_caption_change(self):
        parent = Container(parent=self.app, layout="stack", width=300, spacing=0)
        changed = Measured(parent=parent)
        clean = Measured(parent=parent)
        arrange(self.app, self.host)
        before = clean._measure_calls
        changed_before = changed._measure_calls
        changed.caption = "considerably longer"
        arrange(self.app, self.host)
        self.assertEqual(changed.bounds.width, 300)
        self.assertEqual(parent.bounds.width, 300)
        self.assertGreater(changed._measure_calls, changed_before)
        self.assertEqual(clean._measure_calls, before)

    def test_fixed_size_can_return_to_current_intrinsic_size(self):
        control = Measured(parent=self.app, width=80, height=30)
        self.assertEqual(self.size(control), (80, 30))
        self.assertEqual(self.size(control), (80, 30))
        self.assertEqual(control._measure_calls, 1)
        control.caption = "longer"
        self.assertEqual(self.size(control), (80, 30))
        control.update(width=None, height=None)
        self.assertEqual(self.size(control), (60, 20))

    def test_paint_only_changes_reuse_preferred_size(self):
        child = Measured(parent=self.app)
        self.assertEqual(self.size(child), (40, 20))
        child.foreground = "#ff0000"
        self.assertEqual(self.size(child), (40, 20))
        child.invalidate(Dirty.PAINT)
        self.assertEqual(self.size(child), (40, 20))
        self.assertEqual(child._measure_calls, 1)

    def test_subwindow_closable_refreshes_automatic_title_width(self):
        for use_update in (False, True):
            for child_width in (None, 120, 300):
                with self.subTest(update=use_update, child_width=child_width):
                    dialog = self.app.sub_window(
                        width=None, title="Project settings", closable=False
                    )
                    if child_width is not None:
                        dialog.panel(width=child_width, height=30)
                    for closable in (False, True, False):
                        if use_update:
                            dialog.update(closable=closable)
                        else:
                            dialog.closable = closable
                        arrange(self.app, self.host)
                        paint_tree(self.app, self.host)
                        expected = 324 if child_width == 300 else 202 if closable else 164
                        self.assertEqual(dialog.bounds.width, expected)
                        self.assertIn("Project settings", self.host.texts)
                    dialog.destroy()

    def test_available_constraints_distinguish_wrapped_measurements(self):
        control = Available(parent=self.app)
        self.assertEqual(self.size(control, width=30, height=100), (30, 40))
        self.assertEqual(self.size(control, width=30, height=100), (30, 40))
        self.assertEqual(control._measure_calls, 1)
        self.assertEqual(self.size(control, width=80, height=100), (40, 20))
        self.assertEqual(control._measure_calls, 2)
        self.size(control, width=80, height=50)
        self.assertEqual(control._measure_calls, 3)

    def test_constraint_cache_is_bounded(self):
        control = Available(parent=self.app)
        for width in range(1, 129):
            self.size(control, width=width)
        before = control._measure_calls
        self.size(control, width=128)
        self.assertEqual(control._measure_calls, before)
        self.size(control, width=1)
        self.assertGreater(control._measure_calls, before)

    def test_layout_size_hook_runs_even_when_preferred_size_is_reused(self):
        class Adjusted(Measured):
            def _initialize(self):
                super()._initialize()
                self._extra = 0

            def _layout_size(self, width, height):
                return width + self._extra, height + self.effective_row_height(0)

        self.host.text_row_height = 5
        control = Adjusted(parent=self.app, min_width=50, max_height=15)
        self.assertEqual(self.size(control), (50, 20))
        control._extra = 7
        self.assertEqual(self.size(control), (57, 20))
        self.assertEqual(control._measure_calls, 1)

    def test_parent_arrange_observes_caption_even_when_child_size_is_fixed(self):
        class CaptionLayout(Container):
            def arrange_children(self, area, measure):
                child = self.children[0]
                width, height = measure(child)
                return {child: Rect(area.x + len(child.caption), area.y, width, height)}

        parent = CaptionLayout(parent=self.app, width=300, height=80)
        child = Measured(parent=parent, width=80, height=20)
        arrange(self.app, self.host)
        self.assertEqual(child.bounds, Rect(4, 0, 80, 20))
        child.caption = "longer"
        arrange(self.app, self.host)
        self.assertEqual(child.bounds, Rect(6, 0, 80, 20))

    def test_private_and_peer_measurements_refresh_when_dependents_invalidate(self):
        class Dependent(Measured):
            def _initialize(self):
                super()._initialize()
                self._peer = None
                self._extra = 0

            def measure(self, host):
                self._measure_calls += 1
                return len(self._peer.caption) * 10 + self._extra, 20

        parent = Container(parent=self.app, layout="stack", spacing=0)
        peer = Measured(parent=parent)
        dependent = Dependent(parent=parent)
        dependent._peer = peer
        self.assertEqual(self.size(parent), (40, 40))
        peer.caption = "longer"
        dependent._extra = 15
        dependent.invalidate(Dirty.MEASURE)
        self.assertEqual(self.size(parent), (75, 40))

    def test_uncached_descendant_bypasses_every_intrinsic_ancestor(self):
        class External(Measured):
            def _initialize(self):
                super()._initialize()
                self._preferred = 40

            def measure(self, host):
                self._measure_calls += 1
                return self._preferred, 20

        outer = Container(parent=self.app, layout="stack", spacing=0)
        inner = Container(parent=outer, layout="stack", spacing=0)
        child = External(parent=inner, cache_measure=False)
        self.assertEqual(self.size(outer), (40, 20))
        child._preferred = 90
        self.assertEqual(self.size(outer), (90, 20))
        child.cache_measure = True
        self.assertEqual(self.size(outer), (90, 20))
        before = child._measure_calls
        self.assertEqual(self.size(outer), (90, 20))
        self.assertEqual(child._measure_calls, before)
        child.cache_measure = False
        child._preferred = 110
        self.assertEqual(self.size(outer), (110, 20))

    def test_structural_changes_refresh_intrinsic_size_and_opt_out_status(self):
        parent = Container(parent=self.app, layout="stack", spacing=0)
        child = Measured(parent=parent)
        self.assertEqual(self.size(parent), (40, 20))
        added = Measured(parent=parent, caption="longer", cache_measure=False)
        self.assertEqual(self.size(parent), (60, 40))
        added.destroy()
        self.assertEqual(self.size(parent), (40, 20))
        before = child._measure_calls
        self.assertEqual(self.size(parent), (40, 20))
        self.assertEqual(child._measure_calls, before)

    def test_explicit_parent_invalidation_refreshes_descendants_but_child_does_not(self):
        parent = Container(parent=self.app, layout="stack", spacing=0)
        first, second = Measured(parent=parent), Measured(parent=parent)
        self.size(parent)
        before = second._measure_calls
        first.invalidate(Dirty.MEASURE)
        self.size(parent)
        self.assertEqual(second._measure_calls, before)
        parent.invalidate(Dirty.MEASURE)
        self.size(parent)
        self.assertGreater(second._measure_calls, before)

    def test_arrange_properties_can_affect_custom_measurement(self):
        class Positioned(Measured):
            def measure(self, host):
                self._measure_calls += 1
                return self.left + self.parent.top + 40, 20

        parent = Container(parent=self.app)
        child = Positioned(parent=parent)
        self.assertEqual(self.size(child), (40, 20))
        child.left = 5
        self.assertEqual(self.size(child), (45, 20))
        parent.top = 10
        self.assertEqual(self.size(child), (55, 20))

    def test_atomic_and_child_batches_refresh_measurements(self):
        parent = Container(parent=self.app, layout="stack", spacing=0)
        first, second = Measured(parent=parent), Measured(parent=parent)
        self.assertEqual(self.size(parent), (40, 40))
        first.update(caption="longer", min_width=80)
        self.assertEqual(self.size(parent), (80, 40))
        parent.update_children({first: {"caption": "ten letters", "min_width": 0},
                                second: {"caption": "a"}})
        self.assertEqual(self.size(parent), (110, 40))

    def test_declared_measure_dirty_survives_custom_changed_hook(self):
        class CustomChanged(Measured):
            def _changed(self, field, old, value):
                # A hook can inspect/consume a field without forwarding it.
                self._last_change = field.name

        parent = Container(parent=self.app, layout="stack", spacing=0)
        child = CustomChanged(parent=parent)
        self.assertEqual(self.size(parent), (40, 20))
        child.caption = "longer"
        self.assertEqual(self.size(parent), (60, 20))
        child.update(caption="a")
        self.assertEqual(self.size(parent), (10, 20))

    def test_host_resources_viewport_theme_and_fixed_rows_refresh_context(self):
        class Contextual(Measured):
            def measure(self, host):
                self._measure_calls += 1
                return (self.effective_theme.tokens.font_size + host.size[0] + host.unit,
                        self.effective_row_height(11))

        self.host.unit, self.host.resource_revision = 1, 0
        child = Contextual(parent=self.app)
        initial = self.size(child)
        self.assertEqual(self.size(child), initial)
        self.assertEqual(child._measure_calls, 1)
        self.host.unit = 2
        self.host.resource_revision += 1
        self.assertEqual(self.size(child), (initial[0] + 1, 11))
        self.host.size = (800, 400)
        self.assertEqual(self.size(child), (initial[0] + 161, 11))
        self.app.theme = self.app.theme.derive(font_size=30)
        self.assertEqual(self.size(child), (832, 11))
        self.host.text_row_height = 24
        self.assertEqual(self.size(child), (832, 24))
        other = RecordingHost()
        other.unit = 4
        self.assertEqual(_size(child, other), (674, 11))
        self.assertIsNone(self.app._measurement_row_height)

    def test_invalidation_during_measurement_does_not_cache_stale_result(self):
        class Changing(Measured):
            def measure(self, host):
                self._measure_calls += 1
                if self._measure_calls == 1:
                    self.invalidate(Dirty.MEASURE)
                    return 40, 20
                return 80, 20

        child = Changing(parent=self.app)
        self.assertEqual(self.size(child), (40, 20))
        self.assertEqual(self.size(child), (80, 20))
        self.assertEqual(self.size(child), (80, 20))
        self.assertEqual(child._measure_calls, 2)

    def test_failed_measurement_is_retried_and_restores_row_context(self):
        class Failing(Measured):
            def measure(self, host):
                self._measure_calls += 1
                if self._measure_calls == 1:
                    raise ValueError("measurement failed")
                return 40, 20

        child = Failing(parent=self.app)
        self.host.text_row_height = 24
        with self.assertRaisesRegex(ValueError, "measurement failed"):
            self.size(child)
        self.assertIsNone(self.app._measurement_row_height)
        self.assertEqual(self.size(child), (40, 20))
        self.assertEqual(self.size(child), (40, 20))
        self.assertEqual(child._measure_calls, 2)

    def test_detached_measurement_adopts_new_parent_and_disposal_is_checked(self):
        child = Measured()
        self.assertEqual(self.size(child), (40, 20))
        before = child._measure_calls
        self.app.add(child)
        self.assertEqual(self.size(child), (40, 20))
        self.assertGreater(child._measure_calls, before)
        child.destroy()
        with self.assertRaises(LifecycleError):
            self.size(child)


class CallerMeasurementCacheTests(unittest.TestCase):
    def test_normal_caller_scalar_assignment_refreshes_owner_measurement(self):
        app, host = App(), RecordingHost()
        try:
            child = Measured(parent=app)
            self.assertEqual(call(_size, child, host), (40, 20))
            child.caption = "longer"
            self.assertEqual(call(_size, child, host), (60, 20))
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
