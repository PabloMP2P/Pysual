"""Tree contracts and shared input behavior through the installed library."""

import asyncio
from dataclasses import FrozenInstanceError
import unittest
from unittest.mock import patch
from _ui_testcase import AsyncUIOwnerTestCase

from pysual import App, ChangeEvent, TreeEvent, TreeNode, TreeView
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


NODES = (
    TreeNode(
        "root",
        "Project",
        (
            TreeNode(
                "src", "Source", (TreeNode("app", "app.py"), TreeNode("api", "api.py"))
            ),
            TreeNode("assets", "Assets", (TreeNode("image", "image.png"),)),
        ),
    ),
    TreeNode("readme", "README.md"),
)


class TreeDataTests(unittest.TestCase):
    def test_data_validation_and_atomic_replacement(self):
        with self.assertRaises(FrozenInstanceError):
            NODES[0].text = "mutated"
        for args in (("", "Text"), ("key", 42), ("key", "Text", [])):
            with self.assertRaises((TypeError, ValueError)):
                TreeNode(*args)
        tree = TreeView(nodes=NODES, selected_key="app")
        try:
            self.assertEqual(tree.expanded_keys, ("root", "src"))
            self.assertEqual(tree.selected_node.text, "app.py")
            for values in (
                {"selected_key": "missing"},
                {"expanded_keys": ("app",)},
                {"expanded_keys": ("root", "root")},
                {"nodes": (TreeNode("root", "Duplicate"), *NODES)},
            ):
                before = tree.nodes, tree.selected_key, tree.expanded_keys
                with self.assertRaises(ValueError):
                    setattr(tree, *next(iter(values.items())))
                self.assertEqual(
                    (tree.nodes, tree.selected_key, tree.expanded_keys), before
                )
        finally:
            tree.destroy()

    def test_selection_survives_reorder_and_reconciles_collapse_and_removal(self):
        tree = TreeView(nodes=NODES, selected_key="app")
        try:
            tree.nodes = (NODES[1], NODES[0])
            self.assertEqual(tree.selected_key, "app")
            tree.toggle("src")
            self.assertEqual(tree.selected_key, "src")
            tree.toggle("root")
            self.assertEqual(tree.selected_key, "root")
            tree.selected_key = "image"
            self.assertEqual(set(tree.expanded_keys), {"root", "assets"})
            tree.reveal("api")
            self.assertEqual(tree.selected_key, "image")
            self.assertIn("src", tree.expanded_keys)
            tree.nodes = (NODES[1],)
            self.assertIsNone(tree.selected_node)
            self.assertIsNone(tree.selected_key)
            self.assertEqual(tree.expanded_keys, ())
        finally:
            tree.destroy()

    def test_large_tree_paints_viewport_and_clamps_scroll_after_resize(self):
        app = App()
        try:
            tree = app.tree_view(
                nodes=(
                    TreeNode(
                        "all",
                        "Records",
                        tuple(TreeNode(str(i), f"Record {i}") for i in range(100_000)),
                    ),
                ),
                expanded_keys=("all",),
                width=240,
                height=90,
                row_height=30,
            )
            host = RecordingHost()
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(tree._painted_rows, 3)
            self.assertEqual(host.texts, ["Records", "Record 0", "Record 1"])
            tree.selected_key = "99999"
            paint_tree(app, host)
            self.assertEqual(host.texts[-1], "Record 99999")
            self.assertEqual(tree._painted_rows, 3)
            tree.height = 300
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(tree._painted_rows, 10)
            self.assertEqual(host.texts[-1], "Record 99999")
            tree.nodes = (TreeNode("only", "Only row"),)
            paint_tree(app, host)
            self.assertEqual(host.texts, ["Only row"])
            self.assertIsNone(tree._scrollbar())
        finally:
            app.destroy()

    def test_fractional_wheel_gestures_preserve_total_movement(self):
        app = App()
        try:
            tree = app.tree_view(
                nodes=tuple(TreeNode(str(i), f"Row {i}") for i in range(100)),
                height=90,
            )
            arrange(app, RecordingHost())
            for _ in range(100):
                tree.handle_input(Input("wheel", delta=0.01))
            self.assertEqual(tree._scroll, 3)
            tree.handle_input(Input("wheel", delta=1))
            self.assertEqual(tree._scroll, 6)
            for _ in range(100):
                tree.handle_input(Input("wheel", delta=-0.01))
            self.assertEqual(tree._scroll, 3)
        finally:
            app.destroy()

    def test_typeahead_reveals_a_match_that_is_already_selected(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        tree = app.tree_view(
            nodes=tuple(TreeNode(str(i), f"Alpha {i}") for i in range(20))
            + (TreeNode("zebra", "Zebra"),),
            width=240, height=90, row_height=30,
        )
        arrange(app, host)
        tree.selected_key = "zebra"
        self.assertEqual(tree._scroll, 18)
        for key in ("z", "e"):
            with self.subTest(prefix_key=key):
                tree.handle_input(Input("wheel", delta=-100))
                self.assertEqual(tree._scroll, 0)
                tree.handle_input(Input("key_down", key=key))
                paint_tree(app, host)
                self.assertEqual(tree.selected_key, "zebra")
                self.assertEqual(tree._scroll, 18)
                self.assertIn("Zebra", host.texts)

    def test_navigation_starts_a_new_typeahead_search(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        tree = app.tree_view(nodes=(TreeNode("a", "Apple"), TreeNode("c", "Cherry"),
                                   TreeNode("b", "Banana")), width=240, height=100)
        arrange(app, host)
        for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
                    "Home", "End", "PageUp", "PageDown"):
            with self.subTest(key=key), patch("pysual.tree.monotonic", return_value=10):
                tree.handle_input(Input("blur"))
                tree.handle_input(Input("key_down", key="a"))
                self.assertEqual(tree.selected_key, "a")
                tree.handle_input(Input("key_down", key=key))
                tree.handle_input(Input("key_down", key="b"))
                self.assertEqual(tree.selected_key, "b")

    def test_outer_clip_limits_paint_work(self):
        app = App()
        try:
            area = app.scroll_area(width=250, height=90)
            tree = area.tree_view(
                nodes=tuple(TreeNode(str(i), f"Row {i}") for i in range(1000)),
                width=240,
                height=30_000,
                row_height=30,
            )
            host = RecordingHost()
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(host.texts, ["Row 0", "Row 1", "Row 2"])
            area.scroll_y = 1500
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(host.texts, ["Row 50", "Row 51", "Row 52"])
            self.assertEqual(tree._painted_rows, 3)
        finally:
            app.destroy()

    def test_key_at_respects_outer_clip_after_outer_and_tree_scrolling(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        area = app.scroll_area(width=220, height=90)
        tree = area.tree_view(
            nodes=tuple(TreeNode(str(i), f"Row {i}") for i in range(100)),
            width=200, height=300, row_height=30,
        )
        arrange(app, host)
        for outer_scroll, wheel_delta, expected in ((0, 0, "1"), (60, 0, "3"), (60, 1, "6")):
            with self.subTest(outer_scroll=outer_scroll, wheel_delta=wheel_delta):
                area.scroll_y = outer_scroll
                if wheel_delta:
                    tree.handle_input(Input("wheel", delta=wheel_delta))
                arrange(app, host)
                self.assertEqual(tree.key_at(50, 40), expected)
                self.assertIsNone(tree.key_at(50, 160))
                self.assertIsNone(tree.key_at(195, 40))  # Scrollbar track.


class TreeRuntimeTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        changes, activation_starts, activations = [], [], []

        class Demo(App):
            def build(self):
                self.navigation = self.tree_view(nodes=NODES, width=240, height=150)

            def navigation_on_changed(self, event: ChangeEvent[str | None]):
                changes.append((event.old_value, event.new_value, event.origin))

            async def navigation_on_activated(self, event: TreeEvent):
                activation_starts.append(event.key)
                await asyncio.sleep(0.01)
                activations.append(event.key)

        self.changes, self.activations = changes, activations
        self.activation_starts = activation_starts
        self.app = Demo()
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task
        self.tree = self.app.navigation
        self.tree.focus()

    async def asyncTearDown(self):
        if not self.task.done():
            await self.runtime.request_close()
        await self.task

    async def inputs(self, *events):
        self.host.events.extend(events)
        await eventually(lambda: not self.host.events)
        await self.runtime.dispatcher.drain()
        frames = self.app.frame_count
        self.app.request_frame()
        await eventually(lambda: self.app.frame_count > frames)
        self.runtime.dispatcher.raise_errors()

    async def key(self, key):
        await self.inputs(Input("key_down", key=key))

    async def test_keyboard_navigation_typeahead_and_activation_snapshots(self):
        await self.key("Home")
        self.assertEqual(self.tree.selected_key, "root")
        await self.key("ArrowRight")
        self.assertEqual(self.tree.expanded_keys, ("root",))
        await self.key("ArrowRight")
        self.assertEqual(self.tree.selected_key, "src")
        await self.key("ArrowRight")
        await self.key("ArrowRight")
        self.assertEqual(self.tree.selected_key, "app")
        await self.inputs(
            Input("key_down", key="Enter"),
            Input("key_down", key="ArrowDown"),
            Input("key_down", key="Enter"),
        )
        self.assertEqual(self.activation_starts, ["app", "api"])
        # Dispatch preserves input order, but independent awaited handlers may
        # finish in either order. Each must retain its own activation snapshot.
        self.assertCountEqual(self.activations, ["app", "api"])
        await self.key("ArrowLeft")
        self.assertEqual(self.tree.selected_key, "src")
        await self.key("Space")
        self.assertNotIn("src", self.tree.expanded_keys)
        await self.key("End")
        self.assertEqual(self.tree.selected_key, "readme")
        await self.key("a")
        self.assertEqual(self.tree.selected_key, "assets")
        await self.key("PageUp")
        self.assertEqual(self.tree.selected_key, "root")
        await self.key("PageDown")
        self.assertEqual(self.tree.selected_key, "readme")
        self.assertIn((None, "root", "user"), self.changes)

    async def test_pointer_disclosure_selection_scrollbar_and_disabled_input(self):
        box = self.tree.bounds
        await self.inputs(
            Input("pointer_down", box.x + 12, box.y + 15),
            Input("pointer_up", box.x + 12, box.y + 15),
        )
        self.assertIn("root", self.tree.expanded_keys)
        self.assertIsNone(self.tree.selected_key)
        await self.inputs(
            Input("pointer_down", box.x + 65, box.y + 45),
            Input("pointer_up", box.x + 65, box.y + 45),
        )
        self.assertEqual(self.tree.selected_key, "src")
        self.tree.nodes = tuple(TreeNode(str(i), f"Row {i}") for i in range(100))
        await self.inputs(Input("wheel", box.x + 50, box.y + 20, delta=1))
        self.assertEqual(self.host.texts[0], "Row 3")
        thumb = self.tree._scrollbar()
        await self.inputs(
            Input("pointer_down", box.x + thumb.x + 2, box.y + thumb.y + 2),
            Input("pointer_move", box.right - 5, box.bottom + 100),
            Input("pointer_up", box.right - 5, box.bottom + 100),
        )
        self.assertEqual(self.host.texts[-1], "Row 99")
        self.assertIsNone(self.tree._drag)
        self.tree.enabled = False
        await self.inputs(
            Input("pointer_down", box.x + 50, box.y + 20),
            Input("pointer_up", box.x + 50, box.y + 20),
            Input("key_down", key="Home"),
        )
        self.assertIsNone(self.tree.selected_key)
        self.assertFalse(self.tree.focused)
        # Capture cancellation must also work when focus is explicitly disabled.
        self.tree.enabled = True
        self.tree.focusable = False
        thumb = self.tree._scrollbar()
        await self.inputs(
            Input("pointer_down", box.x + thumb.x + 2, box.y + thumb.y + 2)
        )
        self.tree.enabled = False
        await asyncio.sleep(0.035)
        self.tree.enabled = True
        await self.inputs(Input("pointer_move", box.right - 5, box.y + 10))
        self.assertEqual(self.host.texts[-1], "Row 99")
