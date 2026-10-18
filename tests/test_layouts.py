"""Constraint, track, wrapping and dock behavior through ordinary containers."""

import gc
from random import Random
from unittest.mock import patch
from weakref import ref
import unittest

from _ui_testcase import UIOwnerTestCase

from pysual import App, Container, Control, Dirty, Label, ScrollArea
from pysual.geometry import Rect
from pysual.layout import _clear_measurements, arrange
from pysual.host import Viewport
import pysual.layout as layout
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


class CountingChildren(list):
    def __init__(self, children):
        super().__init__(children)
        self.visits = 0

    def __iter__(self):
        for child in super().__iter__():
            self.visits += 1
            yield child


class IncrementalGridTests(UIOwnerTestCase):
    def setUp(self):
        self.root = App(width=640, height=400)
        self.host = RecordingHost()

    def tearDown(self):
        self.root.destroy()

    def test_fixed_grid_content_edit_does_not_scan_siblings(self):
        grid = Container(parent=self.root, layout="grid", width=600, height=350,
                         columns=20, spacing=0)
        labels = [Label(parent=grid, text=str(i)) for i in range(2000)]
        grid._children = CountingChildren(grid._children)
        arrange(self.root, self.host)
        plan = grid._layout_plan
        boxes = plan.boxes
        grid._children.visits = 0
        labels[1000].text = "Changed content"
        self.assertTrue(self.root.dirty & Dirty.MEASURE)
        self.assertEqual(grid._layout_children_dirty, {labels[1000]})
        arrange(self.root, self.host)
        self.assertIs(grid._layout_plan, plan)
        self.assertIs(grid._layout_plan.boxes, boxes)
        self.assertEqual(grid._children.visits, 0)
        self.assertFalse(labels[1000].dirty & Dirty.MEASURE)

    def test_fixed_grid_geometry_edits_only_recompute_changed_child_boxes(self):
        grid = Container(parent=self.root, layout="grid", width=600, height=350,
                         columns=20, spacing=0)
        labels = [Label(parent=grid, text=str(i)) for i in range(2000)]
        grid._children = CountingChildren(grid._children)
        arrange(self.root, self.host)
        plan = grid._layout_plan
        original_slots = plan.slots
        grid._children.visits = 0
        for values, expected in (
            ({"margin": 1}, Rect(1, 176, 28, 1.5)),
            ({"width": 10, "height": 15}, Rect(1, 176, 10, 15)),
            ({"min_width": 20, "max_height": 10}, Rect(1, 176, 20, 10)),
            ({"width": None, "height": None, "margin": 0}, Rect(0, 175, 30, 3.5)),
        ):
            with self.subTest(values=values):
                labels[1000].update(**values)
                with (patch.object(layout, "_grid_tracks", wraps=layout._grid_tracks) as tracks,
                      patch.object(layout, "_grid_child_box", wraps=layout._grid_child_box) as boxes):
                    arrange(self.root, self.host)
                self.assertEqual(tracks.call_count, 0)
                self.assertEqual(boxes.call_count, 1)
                self.assertEqual(grid._children.visits, 0)
                self.assertIs(grid._layout_plan, plan)
                self.assertIs(plan.slots, original_slots)
                self.assertEqual(labels[1000].bounds, expected)

        grid.update_children({label: {"margin": 2} for label in labels[:16]})
        with (patch.object(layout, "_grid_tracks", wraps=layout._grid_tracks) as tracks,
              patch.object(layout, "_grid_child_box", wraps=layout._grid_child_box) as boxes):
            arrange(self.root, self.host)
        self.assertEqual(tracks.call_count, 0)
        self.assertEqual(boxes.call_count, 16)
        self.assertEqual(grid._children.visits, 0)

    def test_structure_and_auto_tracks_rebuild_track_allocation(self):
        grid = Container(parent=self.root, layout="grid", width=300, height=200,
                         columns=3, spacing=0)
        labels = [Label(parent=grid, text=str(i)) for i in range(6)]
        arrange(self.root, self.host)
        for change in (lambda: labels[0].update(grid_col_span=2),
                       lambda: labels[1].update(visible=False),
                       lambda: grid.update(column_tracks=("auto", "*", "*")),
                       lambda: labels[0].update(margin=10)):
            change()
            with patch.object(layout, "_grid_tracks", wraps=layout._grid_tracks) as tracks:
                arrange(self.root, self.host)
            self.assertGreater(tracks.call_count, 0)

    def test_custom_arranger_geometry_changes_settle_in_the_current_pass(self):
        class UpdatingGrid(Container):
            def arrange_children(self, area, measure):
                if getattr(self, "_change_sibling", False):
                    self._change_sibling = False
                    self.children[1].margin = 7
                return None

        grid = UpdatingGrid(parent=self.root, layout="grid", width=200, height=100,
                            columns=2, spacing=0)
        first, second = Label(parent=grid), Label(parent=grid)
        arrange(self.root, self.host)
        grid._change_sibling = True
        first.text = "Trigger the custom hook"
        arrange(self.root, self.host)
        self.assertEqual(second.bounds, Rect(107, 7, 86, 86))

    def test_dirty_custom_siblings_keep_tree_order_when_observing_geometry(self):
        class ObservingContainer(Container):
            def _initialize(self):
                super()._initialize()
                self._hash = 0
                self._previous = None
                self._observations = []

            def __hash__(self):
                return self._hash

            def arrange_children(self, area, measure):
                if self._previous is not None:
                    self._observations.append(self._previous.bounds.width)
                return None

        grid = Container(parent=self.root, layout="grid", width=200, height=100,
                         columns=2, spacing=0)
        first = ObservingContainer(width=40)
        first._hash = 7
        grid.add(first)
        second = ObservingContainer(width=30)
        second._hash = 1
        second._previous = first
        grid.add(second)
        arrange(self.root, self.host)
        first.width, second.width = 70, 50
        second._observations.clear()
        arrange(self.root, self.host)
        incremental = list(second._observations)
        self.assertEqual(incremental, [70])
        second._observations.clear()
        _clear_measurements(self.root)
        arrange(self.root, self.host)
        self.assertEqual(second._observations, incremental)

    def test_arrange_hook_can_destroy_and_add_children_during_dirty_plan_reuse(self):
        class StructuralGrid(Container):
            def arrange_children(self, area, measure):
                if getattr(self, "_replace_child", False):
                    self._replace_child = False
                    self.children[1].destroy()
                    Label(parent=self, text="Added", width=45)
                return None

        grid = StructuralGrid(parent=self.root, layout="grid", width=200, height=100,
                              columns=2, spacing=0)
        first, removed = Label(parent=grid), Label(parent=grid)
        arrange(self.root, self.host)
        # Both original controls are pending when the hook replaces one.
        first.width, removed.width = 50, 60
        grid._replace_child = True
        arrange(self.root, self.host)
        self.assertTrue(removed._disposed)
        self.assertEqual(grid.children[1].bounds, Rect(100, 0, 45, 100))

    def test_overlay_geometry_edits_preserve_normal_grid_slots(self):
        class Overlay(Label):
            _overlay = True

        grid = Container(parent=self.root, layout="grid", width=200, height=100,
                         columns=1, spacing=0)
        ordinary = Label(parent=grid)
        overlay = Overlay(parent=grid, width=20, height=10)
        arrange(self.root, self.host)
        slot = grid._layout_plan.slots[ordinary]
        overlay.update(width=40, left=15, top=8)
        with patch.object(layout, "_grid_tracks", wraps=layout._grid_tracks) as tracks:
            arrange(self.root, self.host)
        self.assertEqual(tracks.call_count, 0)
        self.assertEqual(overlay.bounds, Rect(15, 8, 40, 10))
        self.assertEqual(ordinary.bounds, slot)

    def test_custom_ancestor_measure_content_observes_descendant_changes(self):
        class ContentSized(Container):
            def measure_content(self, host, *, width=None, height=None):
                return 100 + len(self.children[0].children[0].text) * 10, 100

        outer = ContentSized(parent=self.root)
        grid = Container(parent=outer, layout="grid", width=200, height=100,
                         columns=1, spacing=0)
        label = Label(parent=grid, text="a")
        arrange(self.root, self.host)
        self.assertEqual(outer.bounds.width, 110)
        label.text = "abcdefghij"
        arrange(self.root, self.host)
        self.assertEqual(outer.bounds.width, 200)

    def test_custom_content_bounds_rechecks_grid_plan_area(self):
        class ContentInset(Container):
            def content_bounds(self, rect):
                return rect.inset(len(self.children[0].text))

        grid = ContentInset(parent=self.root, layout="grid", width=200, height=100,
                            columns=1, spacing=0)
        label = Label(parent=grid, text="a")
        arrange(self.root, self.host)
        self.assertEqual(label.bounds, Rect(1, 1, 198, 98))
        label.text = "abcdef"
        arrange(self.root, self.host)
        self.assertEqual(label.bounds, Rect(6, 6, 188, 88))

    def test_custom_measurement_and_content_bounds_observe_cell_geometry(self):
        class ContentSized(Container):
            def measure_content(self, host, *, width=None, height=None):
                child = self.children[0].children[0]
                return 100 + 10 * child.margin + (child.width or 0), 100

        class InsetGrid(Container):
            def content_bounds(self, rect):
                return rect.inset(self.children[0].margin)

        outer = ContentSized(parent=self.root)
        grid = InsetGrid(parent=outer, layout="grid", width=200, height=100,
                         columns=1, spacing=0)
        label = Label(parent=grid)
        arrange(self.root, self.host)
        self.assertEqual(outer.bounds.width, 100)
        label.margin = 5
        arrange(self.root, self.host)
        self.assertEqual(outer.bounds.width, 150)
        self.assertEqual(label.bounds, Rect(10, 10, 180, 80))
        label.width = 40
        arrange(self.root, self.host)
        self.assertEqual(outer.bounds.width, 190)
        self.assertEqual(label.bounds, Rect(10, 10, 40, 80))

    def test_consumed_change_and_invalidate_hooks_cannot_lose_geometry(self):
        class Consumed(Label):
            def _changed(self, field, old, value):
                pass

            def invalidate(self, affects=Dirty.PAINT):
                pass

        grid = Container(parent=self.root, layout="grid", width=200, height=100,
                         columns=1, spacing=0)
        label = Consumed(parent=grid, text="a")
        arrange(self.root, self.host)
        label.width = 75
        arrange(self.root, self.host)
        self.assertEqual(label.bounds.width, 75)
        label.update(width=90, margin=5)
        arrange(self.root, self.host)
        self.assertEqual(label.bounds, Rect(5, 5, 90, 90))

    def test_atomic_batch_accounts_for_all_geometry_before_first_notification(self):
        host, root = self.host, self.root

        class Observed(Label):
            def _changed(self, field, old, value):
                if field.name == "text":
                    arrange(root, host)
                    self._observed = self.bounds

        grid = Container(parent=root, layout="grid", width=200, height=100,
                         columns=1, spacing=0)
        label = Observed(parent=grid, text="a")
        arrange(root, host)
        label.update(text="b", width=70, margin=4)
        self.assertEqual(label._observed, Rect(4, 4, 70, 92))

    def test_failed_notification_leaves_committed_geometry_arrangeable(self):
        class Failing(Label):
            def _changed(self, field, old, value):
                raise RuntimeError("notification failed")

        grid = Container(parent=self.root, layout="grid", width=200, height=100,
                         columns=1, spacing=0)
        label = Failing(parent=grid)
        arrange(self.root, self.host)
        with self.assertRaisesRegex(RuntimeError, "notification failed"):
            label.width = 80
        arrange(self.root, self.host)
        self.assertEqual(label.bounds.width, 80)

    def test_uncached_external_measurement_reaches_custom_ancestor(self):
        class External(Label):
            def measure(self, host):
                return self._preferred, 20

        outer = Container(parent=self.root, layout="stack", spacing=0)
        label = External(parent=outer, cache_measure=False)
        label._preferred = 40
        arrange(self.root, self.host)
        self.assertEqual(outer.bounds.width, 40)
        label._preferred = 95
        arrange(self.root, self.host)
        self.assertEqual(outer.bounds.width, 95)

    def test_failed_arranger_keeps_dirty_path_for_retry(self):
        class Unreliable(Container):
            def arrange_children(self, area, measure):
                if getattr(self, "_fail", False):
                    self._fail = False
                    raise ValueError("try again")
                return None

        grid = Unreliable(parent=self.root, layout="grid", width=200, height=100)
        label = Label(parent=grid, text="a")
        arrange(self.root, self.host)
        label.width = 80
        grid._fail = True
        with self.assertRaisesRegex(ValueError, "try again"):
            arrange(self.root, self.host)
        arrange(self.root, self.host)
        self.assertEqual(label.bounds.width, 80)

    def test_cached_layout_matches_cold_evaluator_across_mutations(self):
        reference = App(width=640, height=400)
        self.addCleanup(reference.destroy)
        reference_host = RecordingHost()
        pairs = []
        for root in (self.root, reference):
            grid = Container(parent=root, layout="grid", width=600, height=300,
                             columns=4, spacing=3)
            labels = [Label(parent=grid, text=str(i)) for i in range(12)]
            pairs.append((root, grid, labels))
        random = Random(73018)
        for step in range(60):
            index = random.randrange(12)
            values = random.choice([
                {"text": "x" * random.randrange(1, 50)},
                {"width": random.choice([None, 20, 70])},
                {"font_size": random.choice([12, 18, 25])},
                {"margin": random.choice([0, 2, 7])},
                {"visible": random.choice([True, False])},
                {"min_height": random.choice([0, 15, 30])},
            ])
            for root, grid, labels in pairs:
                labels[index].update(**values)
                if step % 10 == 0:
                    grid.column_tracks = ("auto", "*", "2*", 40) if step % 20 else ()
                if step % 15 == 0:
                    grid.spacing = step % 7
            _clear_measurements(reference)
            arrange(self.root, self.host)
            arrange(reference, reference_host)
            for actual, expected in zip(pairs[0][2], pairs[1][2]):
                if actual.visible:
                    self.assertEqual((actual.bounds, actual._clip, actual._paint_clip),
                                     (expected.bounds, expected._clip, expected._paint_clip))

    def test_incremental_geometry_matches_cold_layout_for_spans_constraints_and_scrolling(self):
        class Overlay(Label):
            _overlay = True

        reference = App(width=640, height=400)
        self.addCleanup(reference.destroy)
        pairs = []
        for root in (self.root, reference):
            grid = Container(parent=root, layout="grid", width=300, height=200,
                             column_tracks=("*", 50, "2*", "*"), spacing=3)
            labels = [Label(parent=grid, text=str(i)) for i in range(10)]
            overlay = Overlay(parent=grid, width=40, height=15, top=10)
            scroll = ScrollArea(parent=root, left=320, width=280, height=200,
                                layout="grid", column_tracks=(180, 180),
                                row_tracks=(80, 80, 80), spacing=3)
            rows = [Label(parent=scroll, text=str(i)) for i in range(6)]
            pairs.append((grid, labels, overlay, scroll, rows))
        mutations = [
            lambda grid, labels, overlay, scroll, rows: labels[0].update(margin=7),
            lambda grid, labels, overlay, scroll, rows: labels[1].update(width=35, height=60),
            lambda grid, labels, overlay, scroll, rows: labels[1].update(min_width=40, max_height=20),
            lambda grid, labels, overlay, scroll, rows: labels[0].update(grid_col_span=2, grid_row_span=2),
            lambda grid, labels, overlay, scroll, rows: overlay.update(width=65, left=25, top=35),
            lambda grid, labels, overlay, scroll, rows: scroll.update(scroll_x=35, scroll_y=25),
            lambda grid, labels, overlay, scroll, rows: rows[0].update(width=220, margin=5),
            lambda grid, labels, overlay, scroll, rows: labels[0].update(width=10, min_width=45, margin=3),
            lambda grid, labels, overlay, scroll, rows: labels[1].update(width=None, height=None, max_height=None),
            lambda grid, labels, overlay, scroll, rows: labels[2].update(visible=False),
            lambda grid, labels, overlay, scroll, rows: labels[2].update(visible=True, max_width=25),
            lambda grid, labels, overlay, scroll, rows: grid.update(row_tracks=(40, "2*", "*")),
        ]
        for step, change in enumerate(mutations):
            with self.subTest(step=step):
                for pair in pairs:
                    change(*pair)
                _clear_measurements(reference)
                arrange(self.root, self.host)
                arrange(reference, self.host)
                def controls(pair):
                    grid, labels, overlay, scroll, rows = pair
                    return (grid, *labels, overlay, scroll, *rows)
                for actual, expected in zip(controls(pairs[0]), controls(pairs[1])):
                    if actual.visible:
                        self.assertEqual((actual.bounds, actual._clip, actual._paint_clip),
                                         (expected.bounds, expected._clip, expected._paint_clip))
                self.assertEqual(pairs[0][3]._scroll_limits, pairs[1][3]._scroll_limits)


class CustomLayoutTests(UIOwnerTestCase):
    def test_fixed_grid_recomputes_custom_ancestor_measurement(self):
        class PreferredFromCaption(Container):
            def measure(self, host):
                return 100 + len(self.children[0].children[0].text) * 10, 100

        class AvailableFromCaption(Container):
            def measure_available(self, host, *, width=None, height=None):
                return 100 + len(self.children[0].children[0].text) * 10, 100

        for parent_type in (PreferredFromCaption, AvailableFromCaption):
            with self.subTest(measurement_hook=parent_type.__name__):
                app = App(width=640, height=400)
                self.addCleanup(app.destroy)
                outer = parent_type(parent=app)
                grid = Container(parent=outer, layout="grid", columns=1,
                                 width=100, height=100)
                child = Label(parent=grid, text="a")
                host = RecordingHost()
                arrange(app, host)
                self.assertEqual(outer.bounds.width, 110)
                child.text = "abcdefghij"
                arrange(app, host)
                self.assertEqual(outer.bounds.width, 200)

    def test_fixed_grid_recomputes_custom_layout_size(self):
        class SizedGrid(Container):
            def _layout_size(self, width, height):
                inset = len(self.children[0].text) if self.children else 0
                return width - inset, height

        for instance_hook in (False, True):
            with self.subTest(instance_hook=instance_hook):
                root = Container(width=640, height=400)
                self.addCleanup(root.destroy)
                grid_type = Container if instance_hook else SizedGrid
                grid = grid_type(parent=root, layout="grid", columns=1,
                                 width=200, height=100, padding=0, spacing=0)
                child = Label(parent=grid, text="a")
                if instance_hook:
                    grid._layout_size = lambda width, height: (
                        width - len(child.text), height
                    )
                host = RecordingHost()
                arrange(root, host)
                self.assertEqual(grid.bounds.width, 199)
                self.assertEqual(child.bounds.width, 199)
                child.text = "abcdefghij"
                arrange(root, host)
                self.assertEqual(grid.bounds.width, 190)
                self.assertEqual(child.bounds.width, 190)

    def test_grid_recomputes_an_overridden_app_viewport(self):
        class CustomViewport(App):
            @property
            def viewport(self):
                children = self.children
                inset = len(children[0].text) if children else 0
                return Viewport(640 - inset, 400)

        app = CustomViewport(layout="grid", columns=1, width=640, height=400,
                             padding=0, spacing=0)
        self.addCleanup(app.destroy)
        child = Label(parent=app, text="a")
        host = RecordingHost()
        arrange(app, host)
        self.assertEqual(child.bounds.width, 639)
        child.text = "abcdefghij"
        arrange(app, host)
        self.assertEqual(child.bounds.width, 630)

    def test_fixed_grid_recomputes_custom_content_bounds_after_child_change(self):
        class GutterGrid(Container):
            def content_bounds(self, bounds):
                children = self.children
                return bounds.inset(len(children[0].text) if children else 0)

        host = RecordingHost()
        grid = GutterGrid(layout="grid", width=640, height=400, columns=1, spacing=0)
        self.addCleanup(grid.destroy)
        child = Label(parent=grid, text="a")
        arrange(grid, host)
        self.assertEqual(child.bounds, Rect(1, 1, 638, 398))
        child.text = "abcdefghij"
        arrange(grid, host)
        self.assertEqual(child.bounds, Rect(10, 10, 620, 380))

    def test_grid_recomputes_custom_ancestor_content_bounds(self):
        class GutterStack(Container):
            def content_bounds(self, bounds):
                children = self.children
                leaves = children[0].children if children else ()
                return bounds.inset(len(leaves[0].text) if leaves else 0)

        for fixed in (False, True):
            with self.subTest(fixed_grid=fixed):
                host = RecordingHost()
                stack = GutterStack(layout="stack", width=640, height=400, spacing=0)
                self.addCleanup(stack.destroy)
                grid = Container(parent=stack, layout="grid", flex=1, columns=1, spacing=0,
                                 width=200 if fixed else None, height=100 if fixed else None)
                child = Label(parent=grid, text="a")
                arrange(stack, host)
                self.assertEqual(child.bounds.x, 1)
                child.text = "abcdefghij"
                arrange(stack, host)
                self.assertEqual(child.bounds.x, 10)

    def test_fixed_grid_does_not_hide_custom_ancestor_arrangement(self):
        class Positioned(Container):
            def arrange_children(self, area, measure):
                grid = self.children[0]
                return {grid: Rect(area.x + len(grid.children[0].text), area.y, 200, 100)}

        host = RecordingHost()
        parent = Positioned(width=640, height=400)
        self.addCleanup(parent.destroy)
        grid = Container(parent=parent, layout="grid", columns=1, width=200, height=100)
        child = Label(parent=grid, text="a")
        arrange(parent, host)
        self.assertEqual(child.bounds.x, 1)
        child.text = "abcdefghij"
        arrange(parent, host)
        self.assertEqual(child.bounds.x, 10)

    def test_destroyed_grid_releases_cached_children(self):
        grid = Container(layout="grid", columns=1, width=640, height=400)
        self.addCleanup(grid.destroy)
        child = Label(parent=grid, text="cached")
        child_ref = ref(child)
        arrange(grid, RecordingHost())
        grid.destroy()
        del child
        gc.collect()
        self.assertIsNone(child_ref())

    def test_destroyed_child_is_released_without_another_layout_pass(self):
        grid = Container(layout="grid", columns=1, width=640, height=400)
        self.addCleanup(grid.destroy)
        child = Label(parent=grid, text="cached")
        child_ref = ref(child)
        arrange(grid, RecordingHost())
        child.destroy()
        del child
        gc.collect()
        self.assertIsNone(child_ref())


if __name__ == "__main__":
    unittest.main()
