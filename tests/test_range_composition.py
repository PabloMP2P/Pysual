"""Shared range behavior preserves each control's transactional contracts."""

from _ui_testcase import UIOwnerTestCase

from pysual import NumericInput, ProgressBar, Slider, prop


class RangeCompositionTests(UIOwnerTestCase):
    def keep(self, control):
        self.addCleanup(control.destroy)
        return control

    @staticmethod
    def state(control):
        return getattr(control, "minimum", 0), control.maximum, control.value

    @staticmethod
    def set_range(control, low, high, **kwargs):
        if isinstance(control, ProgressBar):
            control.set_range(high, **kwargs)
        else:
            control.set_range(low, high, **kwargs)

    def test_updates_validate_the_complete_candidate_without_changing_live_state(self):
        for kind in (Slider, NumericInput, ProgressBar):
            with self.subTest(control=kind.__name__):
                control = self.keep(kind(value=5, maximum=10))
                changes = {"maximum": 30, "value": 25}
                if kind is not ProgressBar:
                    changes["minimum"] = 20
                # Each individual assignment would violate the old range.
                control.update(**changes)
                expected = self.state(control)
                revision = control._paint_revision
                with self.assertRaises(ValueError):
                    control.update(maximum=40, value=50, enabled=False)
                self.assertEqual(self.state(control), expected)
                self.assertTrue(control.enabled)
                self.assertEqual(control._paint_revision, revision)
                with self.assertRaises(ValueError):
                    control.maximum = 24
                self.assertEqual(self.state(control), expected)

    def test_set_range_clamps_and_notifications_see_the_complete_commit(self):
        for kind in (Slider, NumericInput, ProgressBar):
            with self.subTest(control=kind.__name__):
                snapshots = []

                class ObservedControl(kind):
                    def _changed(control, field, old, value):
                        snapshots.append((field.name, self.state(control)))
                        super()._changed(field, old, value)

                control = self.keep(ObservedControl(value=80, maximum=100))
                self.set_range(control, 10, 20)
                expected = (0 if kind is ProgressBar else 10, 20, 20)
                self.assertEqual(self.state(control), expected)
                self.assertTrue(snapshots)
                self.assertTrue(all(state == expected for _, state in snapshots))
                self.assertEqual([name for name, _ in snapshots].count("value"), 1)
                snapshots.clear()
                revision = control._paint_revision
                self.set_range(control, 10, 20)
                self.assertEqual(snapshots, [])
                self.assertEqual(control._paint_revision, revision)
                with self.assertRaisesRegex(ValueError, "value <= maximum"):
                    self.set_range(control, 10, 30, value=31)
                self.assertEqual(snapshots, [])
                self.assertEqual(self.state(control), expected)
                self.assertEqual(control._paint_revision, revision)

    def test_field_validation_precedes_range_operations_and_never_partially_commits(self):
        for kind in (Slider, NumericInput, ProgressBar):
            with self.subTest(control=kind.__name__):
                control = self.keep(kind(value=5, maximum=10))
                original = self.state(control)
                for invalid in (True, "10", float("inf"), float("nan")):
                    with self.subTest(invalid=invalid):
                        with self.assertRaises((TypeError, ValueError)):
                            self.set_range(control, 0, invalid)
                        with self.assertRaises((TypeError, ValueError)):
                            self.set_range(control, 0, 20, value=invalid)
                        self.assertEqual(self.state(control), original)

    def test_control_specific_bound_rules_and_optional_numeric_bounds(self):
        slider = self.keep(Slider(value=5, maximum=10))
        with self.assertRaisesRegex(ValueError, "Slider requires minimum < maximum"):
            slider.set_range(5, 5)
        self.assertEqual(self.state(slider), (0, 10, 5))

        numeric = self.keep(NumericInput(value=5))
        numeric.set_range(7, 7)
        self.assertEqual(self.state(numeric), (7, 7, 7))
        numeric.set_range(None, -2)
        self.assertEqual(self.state(numeric), (None, -2, -2))
        numeric.set_range(3, None)
        self.assertEqual(self.state(numeric), (3, None, 3))
        numeric.set_range(None, None, value=-1000)
        self.assertEqual(self.state(numeric), (None, None, -1000))
        with self.assertRaisesRegex(ValueError, "minimum must not exceed maximum"):
            numeric.set_range(20, 10)
        with self.assertRaisesRegex(ValueError, "decimals must be between 0 and 8"):
            numeric.update(minimum=0, maximum=10, value=5, decimals=9)
        self.assertEqual(self.state(numeric), (None, None, -1000))

        progress = self.keep(ProgressBar(value=5, maximum=10))
        with self.assertRaises(ValueError):
            progress.set_range(0)
        with self.assertRaises(ValueError):
            progress.set_range(20, value=-1)
        self.assertEqual(self.state(progress), (0, 10, 5))

    def test_subclasses_retain_field_and_candidate_validation(self):
        class LimitedSlider(Slider):
            value: float = prop(default=5.0, minimum=5, changed="changed")

            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if self.maximum > 50:
                    raise ValueError("custom maximum limit")

        control = self.keep(LimitedSlider(value=10, maximum=20))
        with self.assertRaises(ValueError):
            control.set_range(0, 50, value=4)
        with self.assertRaises(ValueError):
            control.set_range(0, 3)
        with self.assertRaisesRegex(ValueError, "custom maximum limit"):
            control.set_range(30, 60)
        self.assertEqual(self.state(control), (0, 20, 10))
        control.set_range(30, 40)
        self.assertEqual(self.state(control), (30, 40, 30))
