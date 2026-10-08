"""Constraint, track, wrapping and dock behavior through ordinary containers."""

import unittest

from pysual import App, Container, Control
from pysual.geometry import Rect
from pysual.layout import arrange
from test_library import RecordingHost


class MeasuredControl(Control):
    def _initialize(self):
        super()._initialize()
        self._measure_calls = 0

    def measure(self, host):
        self._measure_calls += 1
        return 60, 20


class ResponsiveLayoutTests(unittest.TestCase):
    def setUp(self):
        self.app = App()
        self.host = RecordingHost()

    def tearDown(self):
        self.app.destroy()

    def test_flex_releases_maximum_and_keeps_minimum_proportions(self):
        stack = self.app.panel(
            layout="stack", direction="horizontal", width=300, height=60, spacing=0
        )
        first = stack.panel(flex=1, min_width=90, max_width=100)
        second = stack.panel(flex=1, max_width=40)
        third = stack.panel(flex=2)
        arrange(self.app, self.host)
        self.assertEqual(
            (first.bounds.width, second.bounds.width, third.bounds.width), (90, 40, 170)
        )
        stack.width = 500
        arrange(self.app, self.host)
        self.assertEqual(
            (first.bounds.width, second.bounds.width, third.bounds.width),
            (100, 40, 360),
        )

    def test_flex_constraints_do_not_freeze_minimum_before_maximum(self):
        stack = self.app.panel(
            layout="stack", direction="horizontal", width=100, height=60, spacing=0
        )
        first = stack.panel(flex=1, min_width=60)
        second = stack.panel(flex=1, max_width=20)
        arrange(self.app, self.host)
        self.assertEqual((first.bounds.width, second.bounds.width), (80, 20))
        first.min_width = 90
        second.min_width = 20
        arrange(self.app, self.host)
        self.assertEqual((first.bounds.width, second.bounds.width), (90, 20))
        self.assertEqual(second._clip.width, 10)
        first.max_width = 90
        stack.width = 200
        arrange(self.app, self.host)
        self.assertEqual(second.bounds.right, 110)

    def test_large_finite_weights_preserve_proportional_layout(self):
        stack = self.app.panel(
            layout="stack", direction="horizontal", width=300, height=60, spacing=0
        )
        first = stack.panel(flex=1e308)
        second = stack.panel(flex=1e308)
        arrange(self.app, self.host)
        self.assertEqual((first.bounds.width, second.bounds.width), (150, 150))
        stack.layout = "grid"
        stack.column_tracks = ("1e308*", "1e308*")
        arrange(self.app, self.host)
        self.assertEqual((first.bounds.width, second.bounds.width), (150, 150))
        second.destroy()
        stack.column_tracks = ("1e-320*",)
        arrange(self.app, self.host)
        self.assertEqual(first.bounds.width, 300)
        stack.column_tracks = ("1e308*", "1e-320*")
        with self.assertRaisesRegex(ValueError, "finite range"):
            arrange(self.app, self.host)

    def test_allocated_flex_stack_does_not_measure_nested_grid_content(self):
        for direction, constraints, expected in (
            ("vertical", {"max_height": 40}, Rect(0, 0, 300, 150)),
            ("horizontal", {"max_width": 40}, Rect(0, 0, 250, 200)),
        ):
            with self.subTest(direction=direction):
                stack = self.app.panel(
                    layout="stack",
                    direction=direction,
                    width=300,
                    height=200,
                    spacing=10,
                )
                grid = stack.panel(layout="grid", spacing=10, flex=1)
                children = [MeasuredControl(parent=grid) for _ in range(4)]
                sibling = MeasuredControl(parent=stack, flex=1, **constraints)
                arrange(self.app, self.host)
                self.assertEqual(grid.bounds, expected)
                self.assertEqual([c._measure_calls for c in children], [0] * 4)
                self.assertEqual(sibling._measure_calls, 0)
                if direction == "vertical":
                    grid.width = 240
                else:
                    grid.height = 150
                arrange(self.app, self.host)
                self.assertEqual(
                    grid.bounds.width
                    if direction == "vertical"
                    else grid.bounds.height,
                    240 if direction == "vertical" else 150,
                )
                self.assertEqual([c._measure_calls for c in children], [0] * 4)
                stack.destroy()

    def test_intrinsic_stack_still_measures_flex_content(self):
        for direction in ("vertical", "horizontal"):
            with self.subTest(direction=direction):
                stack = self.app.panel(layout="stack", direction=direction)
                grid = stack.panel(layout="grid", spacing=10, flex=1)
                children = [MeasuredControl(parent=grid) for _ in range(4)]
                arrange(self.app, self.host)
                self.assertEqual(stack.bounds, Rect(0, 0, 130, 50))
                self.assertEqual(grid.bounds, stack.bounds)
                self.assertEqual([c._measure_calls for c in children], [1] * 4)
                stack.destroy()

    def test_intrinsic_nested_stack_includes_padding_margins_and_spacing(self):
        stack = self.app.panel(layout="stack", padding=4, spacing=6)
        stack.panel(width=50, height=20, margin=3)
        nested = stack.panel(
            layout="stack", direction="horizontal", padding=2, spacing=5
        )
        nested.panel(width=40, height=10)
        nested.panel(width=30, height=15)
        arrange(self.app, self.host)
        self.assertEqual((stack.bounds.width, stack.bounds.height), (87, 59))
        self.assertEqual((nested.bounds.width, nested.bounds.height), (79, 19))

    def test_grid_fixed_auto_and_weighted_tracks_keep_explicit_rows(self):
        grid = self.app.panel(
            layout="grid",
            width=500,
            height=180,
            spacing=10,
            column_tracks=(80, "auto", "*", "2*"),
            row_tracks=(40, "auto", "*"),
        )
        rows = [grid.label(text="abcd") for _ in range(4)]
        auto_row = grid.label(text="row", grid_row=1, grid_col=0, height=20)
        final = grid.label(text="last", grid_row=2, grid_col=0)
        arrange(self.app, self.host)
        self.assertEqual([row.bounds.width for row in rows], [80, 36, 118, 236])
        self.assertEqual([row.bounds.x for row in rows], [0, 90, 136, 264])
        self.assertEqual(auto_row.bounds.y, 50)
        self.assertEqual(final.bounds, Rect(0, 80, 80, 100))

    def test_grid_auto_spans_grow_content_and_implicit_rows_remain_stars(self):
        grid = self.app.panel(
            layout="grid",
            width=200,
            height=100,
            spacing=4,
            column_tracks=("auto", "auto"),
            row_tracks=(20,),
        )
        span = grid.panel(
            width=100, height=16, margin=2, grid_row=0, grid_col=0, grid_col_span=2
        )
        left = grid.label(text="a", grid_row=1, grid_col=0)
        right = grid.label(text="b", grid_row=1, grid_col=1)
        arrange(self.app, self.host)
        self.assertEqual((left.bounds.width, right.bounds.width), (50, 50))
        self.assertEqual(right.bounds.x, 54)
        self.assertEqual(left.bounds.height, 76)
        self.assertEqual(span.bounds.x, 2)

    def test_empty_grid_intrinsic_size_retains_declared_tracks(self):
        grid = self.app.panel(
            layout="grid",
            column_tracks=(80, 120),
            row_tracks=(40, 60),
            spacing=10,
            padding=4,
        )
        arrange(self.app, self.host)
        expected = Rect(0, 0, 218, 118)
        self.assertEqual(grid.bounds, expected)
        child = grid.panel()
        arrange(self.app, self.host)
        self.assertEqual(grid.bounds, expected)
        child.destroy()
        arrange(self.app, self.host)
        self.assertEqual(grid.bounds, expected)

    def test_allocated_fixed_and_star_grids_do_not_measure_unused_content(self):
        for tracks, expected in (
            ({}, Rect(155, 105, 145, 95)),
            (
                {"column_tracks": (80, "*"), "row_tracks": (40, "*")},
                Rect(90, 50, 210, 150),
            ),
        ):
            with self.subTest(tracks=tracks):
                grid = self.app.panel(
                    layout="grid", width=300, height=200, spacing=10, **tracks
                )
                children = [MeasuredControl(parent=grid) for _ in range(4)]
                children[0].max_width = 50
                children[0].min_height = 110
                arrange(self.app, self.host)
                self.assertEqual(children[-1].bounds, expected)
                self.assertEqual(children[0].bounds, Rect(0, 0, 50, 110))
                grid.width = 320
                arrange(self.app, self.host)
                self.assertEqual([c._measure_calls for c in children], [0] * 4)
                grid.destroy()

    def test_auto_tracks_still_measure_content_on_either_axis(self):
        for tracks, expected in (
            (
                {"column_tracks": (80, "auto", "*")},
                Rect(160, 0, 140, 200),
            ),
            (
                {"column_tracks": (80, "*"), "row_tracks": ("auto", "*")},
                Rect(0, 30, 80, 170),
            ),
        ):
            with self.subTest(tracks=tracks):
                grid = self.app.panel(
                    layout="grid", width=300, height=200, spacing=10, **tracks
                )
                children = [MeasuredControl(parent=grid) for _ in range(3)]
                arrange(self.app, self.host)
                self.assertEqual(children[-1].bounds, expected)
                self.assertEqual([c._measure_calls for c in children], [1] * 3)
                grid.destroy()

    def test_intrinsic_star_grid_still_measures_content(self):
        grid = self.app.panel(layout="grid", spacing=10)
        children = [MeasuredControl(parent=grid) for _ in range(4)]
        arrange(self.app, self.host)
        self.assertEqual(grid.bounds, Rect(0, 0, 130, 50))
        self.assertEqual(children[-1].bounds, Rect(70, 30, 60, 20))
        self.assertEqual([c._measure_calls for c in children], [1] * 4)

    def test_intrinsic_fixed_column_measures_wrapped_auto_row(self):
        grid = self.app.panel(
            layout="grid", column_tracks=(80,), row_tracks=("auto",), spacing=0
        )
        flow = grid.panel(layout="flow", direction="horizontal", spacing=0)
        children = [flow.panel(width=50, height=20) for _ in range(3)]
        for width, height in ((80, 60), (150, 20), (80, 60)):
            with self.subTest(width=width):
                grid.column_tracks = (width,)
                arrange(self.app, self.host)
                self.assertEqual(grid.bounds, Rect(0, 0, width, height))
                self.assertEqual(flow.bounds.height, height)
                self.assertTrue(all(child._clip == child.bounds for child in children))

    def test_grid_rejects_bad_track_values_and_column_span(self):
        for bad in (-1, "0*", "-2*", "NaN*", "infinity*", "content", True):
            with self.subTest(track=bad), self.assertRaises((ValueError, TypeError)):
                Container(column_tracks=(bad,))
        grid = self.app.panel(layout="grid", column_tracks=("*",))
        child = grid.panel(grid_col_span=2)
        with self.assertRaisesRegex(ValueError, "span"):
            grid.validate_layout()
        child.grid_col_span = 1
        child.grid_col = 0
        with self.assertRaisesRegex(ValueError, "together"):
            grid.validate_layout()

    def test_flow_wraps_and_reflows_on_resize(self):
        flow = self.app.panel(
            layout="flow", direction="horizontal", width=100, spacing=5
        )
        first = flow.panel(width=50, height=20)
        second = flow.panel(width=60, height=30)
        third = flow.panel(width=35, height=10)
        arrange(self.app, self.host)
        self.assertEqual(first.bounds, Rect(0, 0, 50, 20))
        self.assertEqual(second.bounds, Rect(0, 25, 60, 30))
        self.assertEqual(third.bounds, Rect(65, 25, 35, 10))
        self.assertEqual(flow.bounds.height, 55)
        flow.width = 160
        arrange(self.app, self.host)
        self.assertEqual(third.bounds, Rect(120, 0, 35, 10))
        self.assertEqual(flow.bounds.height, 30)

    def test_vertical_flow_keeps_margins_and_oversized_children(self):
        flow = self.app.panel(layout="flow", direction="vertical", height=50, spacing=3)
        first = flow.panel(width=20, height=30, margin=2)
        second = flow.panel(width=40, height=70)
        third = flow.panel(width=10, height=10)
        arrange(self.app, self.host)
        self.assertEqual(first.bounds, Rect(2, 2, 20, 30))
        self.assertEqual(second.bounds, Rect(27, 0, 40, 70))
        self.assertEqual(third.bounds, Rect(70, 0, 10, 10))
        self.assertEqual(flow.bounds.width, 80)
        self.assertEqual(second._clip.height, 50)

    def test_flow_intrinsic_wrapping_uses_clamped_requested_size(self):
        flow = self.app.panel(
            layout="flow", direction="horizontal", width=300, max_width=100, spacing=5
        )
        children = [flow.panel(width=60, height=20) for _ in range(3)]
        arrange(self.app, self.host)
        self.assertEqual((flow.bounds.width, flow.bounds.height), (100, 70))
        self.assertEqual(children[-1].bounds.bottom, flow.bounds.bottom)
        flow.width = None
        arrange(self.app, self.host)
        self.assertEqual((flow.bounds.width, flow.bounds.height), (60, 70))
        self.assertEqual(children[-1].bounds, children[-1]._clip)
        flow.direction = "vertical"
        flow.width, flow.max_width = None, None
        flow.height, flow.max_height = 300, 30
        arrange(self.app, self.host)
        self.assertEqual((flow.bounds.width, flow.bounds.height), (190, 30))
        self.assertEqual(children[-1].bounds, children[-1]._clip)

    def test_nested_stack_flow_height_tracks_available_width_and_resize(self):
        self.app.layout = "stack"
        self.host.size = 220, 300
        outer = self.app.panel(layout="stack", padding=10)
        inner = outer.panel(layout="stack", padding=5)
        flow = inner.panel(layout="flow", direction="horizontal", spacing=0)
        children = [flow.panel(width=90, height=30) for _ in range(3)]
        footer = outer.panel(height=20)
        arrange(self.app, self.host)
        self.assertEqual(flow.bounds.height, 60)
        self.assertEqual(children[-1].bounds, children[-1]._clip)
        self.assertGreaterEqual(footer.bounds.y, inner.bounds.bottom)
        self.host.size = 320, 300
        arrange(self.app, self.host)
        self.assertEqual(flow.bounds.height, 30)
        self.assertEqual(children[-1].bounds, children[-1]._clip)
        self.assertIsNone(flow.width)
        self.assertIsNone(flow.height)

    def test_available_measure_preserves_custom_container_measure_hook(self):
        class Custom(Container):
            def measure(self, host):
                return 110, 65

        self.app.layout = "stack"
        custom = Custom(parent=self.app)
        custom.panel(width=20, height=10)
        arrange(self.app, self.host)
        self.assertEqual(custom.bounds.height, 65)

    def test_dock_consumes_remaining_rectangle_in_order(self):
        dock = self.app.panel(
            layout="dock", width=300, height=200, padding=10, spacing=5
        )
        top = dock.panel(dock="top", height=30)
        left = dock.panel(dock="left", width=50, margin=2)
        bottom = dock.panel(dock="bottom", height=20)
        fill = dock.panel()
        arrange(self.app, self.host)
        self.assertEqual(top.bounds, Rect(10, 10, 280, 30))
        self.assertEqual(left.bounds, Rect(12, 47, 50, 141))
        self.assertEqual(bottom.bounds, Rect(69, 170, 221, 20))
        self.assertEqual(fill.bounds, Rect(69, 45, 221, 120))

    def test_dock_validates_only_visible_nonoverlay_order(self):
        dock = self.app.panel(layout="dock", width=300, height=200)
        fill = dock.panel()
        edge = dock.panel(dock="right", width=30)
        with self.assertRaisesRegex(ValueError, "last"):
            dock.validate_layout()
        fill.visible = False
        dock.sub_window(width=100, height=80)
        dock.validate_layout()
        arrange(self.app, self.host)
        self.assertEqual(edge.bounds.x, 270)
        fill.dock = "left"
        fill.visible = True
        dock.validate_layout()


if __name__ == "__main__":
    unittest.main()
