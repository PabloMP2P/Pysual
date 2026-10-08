"""Fixed text rows share paint, hit-testing, paging and scroll geometry."""

from math import ceil
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pysual import (
    App,
    Container,
    Control,
    DataGrid,
    GridColumn,
    GridRow,
    ListView,
    TextBox,
    TreeNode,
    TreeView,
)
from pysual.backends.term_text import TermTextHost
from pysual.host import Input, text_row_height
from pysual.layout import _size, arrange
from pysual.painting import paint_tree

from test_library import RecordingHost


class CellHost(TermTextHost):
    def __init__(self):
        super().__init__(probe_timeout=0, environ={})
        self.size = (320, 240)
        self._renderer.resize(*self.size)

    def present(self):
        pass


class FixedCollectionRowsTests(unittest.TestCase):
    def app(self):
        app = App()
        self.addCleanup(app.destroy)
        return app

    def test_optional_host_metric_ignores_invalid_hints(self):
        self.assertEqual(text_row_height(object(), 34), 34)
        for value in (None, 0, -2, True, "16", float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertEqual(text_row_height(SimpleNamespace(text_row_height=value), 34), 34)
        self.assertEqual(text_row_height(SimpleNamespace(text_row_height=16), 34), 16)

    def test_fixed_grid_has_correct_row_geometry_before_its_first_paint(self):
        app, host = self.app(), CellHost()
        app.layout, app.column_tracks, app.row_tracks = "grid", (200,), (64,)
        listing = ListView(parent=app, items=("A", "B", "C", "D"))
        with patch.object(
            ListView, "measure", side_effect=AssertionError("unexpected measure")
        ):
            arrange(app, host)
        self.assertEqual(listing.effective_row_height(34), 16)
        self.assertEqual(listing.index_at(40, 24), 1)
        listing.handle_input(Input("key_down", key="PageDown"))
        self.assertEqual(listing.selected_index, 3)
        paint_tree(app, host)
        self.assertEqual(listing.index_at(40, 24), 1)

    def test_metric_is_available_inside_custom_measurement(self):
        class Rows(Control):
            def measure(self, host):
                return 100, 3 * self.effective_row_height(30)

        app = self.app()
        rows = Rows(parent=app)
        self.assertEqual(rows.measure_available(CellHost()), (100, 48))
        self.assertEqual(rows.effective_row_height(30), 30)
        self.assertEqual(rows.measure_available(RecordingHost()), (100, 90))
        arrange(app, CellHost())
        self.assertEqual(rows.bounds.height, 48)
        arrange(app, RecordingHost())
        self.assertEqual(rows.bounds.height, 90)

    def test_cross_host_measurement_preserves_arranged_geometry_and_caches(self):
        app, host = self.app(), CellHost()
        listing = ListView(parent=app, items=("A", "B", "C"), height=64)
        sibling = ListView(parent=app, items=("D", "E", "F"), height=64, left=100)
        arrange(app, host)

        def state():
            return tuple(
                (
                    control.effective_row_height(34),
                    control.bounds,
                    control._clip,
                    control._paint_revision,
                    control._layout_row_height,
                    control.index_at(control.bounds.x + 40, 24),
                )
                for control in (listing, sibling)
            )

        before = state()
        self.assertEqual(listing.index_at(40, 24), 1)
        listing.measure_available(RecordingHost())
        self.assertEqual(state(), before)
        app.measure_available(RecordingHost())
        self.assertEqual(state(), before)
        app.measure_content(RecordingHost())
        self.assertEqual(state(), before)
        self.assertIsNone(app._measurement_row_height)

    def test_nested_measurement_and_exceptions_restore_the_previous_metric(self):
        app = self.app()
        samples = []

        class Inner(Control):
            def measure(self, host):
                samples.append(self.effective_row_height(30))
                raise RuntimeError("measurement failed")

        inner = Inner(parent=app)

        class Outer(Control):
            def measure(self, host):
                samples.append(self.effective_row_height(30))
                try:
                    inner.measure_available(SimpleNamespace(text_row_height=24))
                finally:
                    samples.append(self.effective_row_height(30))

        outer = Outer(parent=app)
        # Establish actual geometry without invoking the deliberately failing hooks.
        app.layout, app.column_tracks, app.row_tracks = "grid", (160, 160), (64,)
        arrange(app, CellHost())
        before = (outer._paint_revision, inner._paint_revision)
        with self.assertRaisesRegex(RuntimeError, "measurement failed"):
            outer.measure_available(RecordingHost())
        self.assertEqual(samples, [30, 24, 30])
        self.assertEqual(outer.effective_row_height(30), 16)
        self.assertEqual(inner.effective_row_height(30), 16)
        self.assertEqual((outer._paint_revision, inner._paint_revision), before)
        self.assertIsNone(app._measurement_row_height)

    def test_layout_wraps_custom_measure_available_overrides(self):
        app = self.app()
        samples = []

        class Rows(Control):
            def measure_available(self, host, **limits):
                samples.append(self.effective_row_height(30))
                return 100, 3 * self.effective_row_height(30)

        rows = Rows(parent=app)
        arrange(app, CellHost())
        self.assertEqual(samples, [16])
        self.assertEqual(rows.bounds.height, 48)
        self.assertEqual(_size(rows, RecordingHost()), (100, 90))
        self.assertEqual(samples, [16, 30])
        self.assertEqual(rows.effective_row_height(30), 16)
        self.assertEqual(rows.bounds.height, 48)

    def test_metric_changes_reach_unmeasured_descendants_with_unchanged_bounds(self):
        app, host = self.app(), CellHost()
        app.layout, app.column_tracks, app.row_tracks = "grid", (200,), (96,)
        panel = Container(
            parent=app, layout="grid", column_tracks=(200,), row_tracks=(96,)
        )
        listing = ListView(parent=panel, items=tuple(str(i) for i in range(20)))
        arrange(app, host)
        bounds, revision = listing.bounds, listing._paint_revision
        self.assertEqual(listing._visible_rows(), 6)
        host.text_row_height = 24
        arrange(app, host)
        self.assertEqual(listing.bounds, bounds)
        self.assertEqual(listing._visible_rows(), 4)
        self.assertGreater(listing._paint_revision, revision)
        pixel = RecordingHost()
        pixel.size = host.size
        arrange(app, pixel)
        self.assertEqual(listing.bounds, bounds)
        self.assertEqual(listing.effective_row_height(34), 34)

    def test_detached_subtree_adopts_its_new_tree_metric_immediately(self):
        app = self.app()
        arrange(app, RecordingHost())
        panel = Container()
        listing = ListView(parent=panel, items=("A", "B"))
        self.assertEqual(listing.effective_row_height(34), 34)
        arrange(panel, CellHost())
        self.assertEqual(listing.effective_row_height(34), 16)
        app.add(panel)
        self.assertEqual(listing.effective_row_height(34), 34)
        arrange(app, CellHost())
        self.assertEqual(listing.effective_row_height(34), 16)
        late = ListView(parent=panel)
        self.assertEqual(late.effective_row_height(34), 16)

    def test_painting_never_changes_collection_or_editor_row_geometry(self):
        app, host = self.app(), CellHost()
        listing = ListView(parent=app, items=("A", "B"))
        tree = TreeView(parent=app, nodes=(TreeNode("a", "A"),))
        grid = DataGrid(parent=app)
        entry = TextBox(parent=app, multiline=True, text="A\nB")
        arrange(app, host)

        def metrics():
            return (
                listing._row_height(),
                tree._row_height(),
                grid._row_height(),
                grid._header_height(),
                entry._line_height(),
            )

        self.assertEqual(metrics(), (16,) * 5)
        paint_tree(app, RecordingHost())
        self.assertEqual(metrics(), (16,) * 5)

    def test_tree_icons_and_expanders_stay_on_their_rows_at_fractional_origins(self):
        app, host = self.app(), CellHost()
        tree = TreeView(
            parent=app,
            width=220,
            height=64,
            nodes=(
                TreeNode(
                    "a", "Alpha", icon="download", children=(TreeNode("c", "Child"),)
                ),
                TreeNode("b", "Beta", icon="download"),
            ),
        )
        for quarter in range(64):
            with self.subTest(top=quarter / 4):
                tree.top = quarter / 4
                arrange(app, host)
                paint_tree(app, host)
                rows = host._renderer.snapshot_rows()
                alpha = next(row for row in rows if "Alpha" in row)
                beta = next(row for row in rows if "Beta" in row)
                self.assertIn("↓", alpha)
                self.assertIn("›", alpha)
                self.assertIn("↓", beta)

    def test_list_terminal_rows_stay_adjacent_at_every_origin_and_font_size(self):
        app, host = self.app(), CellHost()
        labels = tuple(f"Item {i}" for i in range(4))
        listing = ListView(parent=app, items=labels, width=200, height=64, selected_index=1)
        for top in range(16):
            for size in (11, 14, 22):
                with self.subTest(top=top, size=size):
                    listing.top, listing.font_size = top, size
                    arrange(app, host)
                    paint_tree(app, host)
                    rows = host._renderer.snapshot_rows()
                    positions = [next(i for i, row in enumerate(rows) if label in row)
                                 for label in labels]
                    self.assertEqual(positions, list(range(positions[0], positions[0] + 4)))
                    self.assertEqual(listing._visible_rows(), 4)
                    for index, terminal_row in enumerate(positions):
                        self.assertEqual(listing.index_at(40, terminal_row * 16 + 8), index)

    def test_list_paging_reveal_drop_slots_and_scrollbar_use_fixed_advance(self):
        app, host = self.app(), CellHost()
        listing = ListView(parent=app, items=tuple(str(i) for i in range(20)),
                           width=200, height=64, top=5, row_height=34, selected_index=0)
        arrange(app, host)
        listing.handle_input(Input("key_down", key="PageDown"))
        self.assertEqual((listing.selected_index, listing._scroll), (4, 1))
        self.assertEqual(listing.index_at(40, 13), 1)
        self.assertEqual(listing.insertion_index(40, 34), 3)
        listing.reveal(19)
        self.assertEqual(listing._scroll, 16)
        track, thumb = listing._scrollbar()
        self.assertAlmostEqual(thumb.bottom, track.bottom)
        listing.height = 96
        arrange(app, host)
        paint_tree(app, host)
        self.assertEqual((listing._scroll, listing._visible_rows()), (14, 6))
        listing.handle_input(Input("wheel", delta=-1))
        self.assertEqual(listing._scroll, 11)

    def test_tree_rows_use_same_geometry_for_paint_picking_and_navigation(self):
        app, host = self.app(), CellHost()
        tree = TreeView(parent=app, nodes=tuple(TreeNode(str(i), f"Node {i}") for i in range(20)),
                        top=9, width=220, height=64, row_height=30)
        arrange(app, host)
        paint_tree(app, host)
        rows = host._renderer.snapshot_rows()
        for index in range(4):
            row = next(i for i, value in enumerate(rows) if f"Node {index}" in value)
            self.assertEqual(row, ceil(9 / 16 - 0.5) + index)
            self.assertEqual(tree.key_at(80, row * 16 + 8), str(index))
        tree.handle_input(Input("pointer_down", x=80, y=56))
        self.assertEqual(tree.selected_key, "2")
        tree.handle_input(Input("key_down", key="PageDown"))
        self.assertEqual(tree.selected_key, "6")
        self.assertEqual(tree._scroll, 3)
        tree.reveal("19")
        self.assertEqual(tree._scroll, 16)

    def test_grid_header_rows_and_scroll_geometry_use_the_fixed_advance(self):
        app, host = self.app(), CellHost()
        grid = DataGrid(parent=app, columns=(GridColumn("name", "Name", width=160),),
                        rows=tuple(GridRow(str(i), (f"Value {i}",)) for i in range(20)),
                        top=5, width=220, height=92, row_height=30, header_height=34)
        arrange(app, host)
        self.assertEqual((grid._header_height(), grid._page_rows()), (16, 4))
        paint_tree(app, host)
        rows = host._renderer.snapshot_rows()
        header_row = next(i for i, value in enumerate(rows) if "Name" in value)
        for index in range(4):
            self.assertIn(f"Value {index}", rows[header_row + index + 1])
        grid.handle_input(Input("pointer_down", x=40, y=61))
        self.assertEqual(grid.selected_key, "2")
        grid.handle_input(Input("key_down", key="PageDown"))
        self.assertEqual((grid.selected_key, grid._scroll), ("6", 3))
        grid.handle_input(Input("key_down", key="End"))
        self.assertEqual((grid.selected_key, grid._scroll), ("19", 16))
        vertical, _ = grid._thumbs()
        self.assertAlmostEqual(vertical.bottom, 16 + 64)

    def test_grid_boolean_editor_sizes_its_three_choices_with_host_rows(self):
        app = self.app()
        for host, list_height in ((CellHost(), 48), (RecordingHost(), 90)):
            with self.subTest(host=type(host).__name__):
                grid = DataGrid(parent=app,
                                columns=(GridColumn("flag", "Flag", kind="bool", editable=True),),
                                rows=(GridRow("one", (True,)),), selected_key="one")
                arrange(app, host)
                with patch("pysual.data_grid.Popup.show"):
                    grid.begin_edit()
                popup = grid._popup
                self.addCleanup(popup.destroy)
                self.assertEqual(popup.height, list_height + 50)
                self.assertEqual(popup._children[0].height, list_height)

    def test_pixel_hosts_keep_requested_collection_spacing(self):
        app, host = self.app(), RecordingHost()
        listing = ListView(parent=app, items=("A", "B", "C"), width=200, height=90, row_height=30)
        tree = TreeView(parent=app, nodes=(TreeNode("a", "A"),), width=200, height=90, row_height=30)
        grid = DataGrid(parent=app, width=220, height=136, row_height=30, header_height=34)
        arrange(app, host)
        self.assertEqual(listing._visible_rows(), 3)
        self.assertEqual(tree._page_rows(), 3)
        self.assertEqual((grid._header_height(), grid._page_rows()), (34, 3))
        self.assertEqual(listing.index_at(40, 35), 1)


if __name__ == "__main__":
    unittest.main()
