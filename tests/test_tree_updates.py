"""Tree transactions stage derived state before hooks and event delivery."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost, eventually

from pysual import App, Button, Container, TreeNode, TreeView
from pysual.runtime import Runtime


NODES = (TreeNode("root", "Root", (TreeNode("child", "Child"),)),)
ROUTES = ("update", "reversed", "children", "children_reversed", "set_tree")


def replace_tree(root, tree, route, nodes=NODES, **properties):
    values = {"nodes": nodes, **properties}
    if "reversed" in route:
        values = dict(reversed(tuple(values.items())))
    if route.startswith("children"):
        root.update_children({tree: values})
    elif route == "set_tree":
        tree.set_tree(**values)
    else:
        tree.update(**values)


def tree_state(tree):
    return (
        dict(tree._values), dict(tree._nodes), dict(tree._parents),
        list(tree._rows), dict(tree._positions), set(tree._expanded),
        tree._scroll, tree._drag, tree.dirty, tree._paint_revision,
    )


class TreeUpdateTests(UIOwnerTestCase):
    def keep(self, item):
        self.addCleanup(item.destroy)
        return item

    def test_replacement_routes_install_indexes_before_changed_hooks(self):
        observed = []

        class ObservedTree(TreeView):
            def _changed(self, field, old, value):
                # A subclass may inspect public/derived state before super().
                observed.append((
                    self.selected_key, self.selected_node, self.expanded_keys,
                    tuple(self._positions), dict(self._parents),
                ))
                super()._changed(field, old, value)

        for route in ROUTES:
            with self.subTest(route=route):
                root = self.keep(Container())
                tree = ObservedTree(parent=root)
                observed.clear()
                replace_tree(root, tree, route, selected_key="child")
                expected = (
                    "child", NODES[0].children[0], ("root",),
                    ("root", "child"), {"root": None, "child": "root"},
                )
                self.assertTrue(observed)
                self.assertTrue(all(state == expected for state in observed), observed)
                self.assertEqual(tree._scroll, 1)  # selected row revealed pre-layout

    def test_retained_selection_reveals_its_new_parent_and_none_stays_explicit(self):
        for route in ROUTES:
            with self.subTest(route=route):
                root = self.keep(Container())
                tree = TreeView(parent=root, nodes=(TreeNode("child", "Child"),),
                                selected_key="child")
                replace_tree(root, tree, route)
                self.assertEqual(tree.selected_key, "child")
                self.assertEqual(tree.expanded_keys, ("root",))
                self.assertIn("child", tree._positions)
                replace_tree(root, tree, route, selected_key=None, expanded_keys=())
                self.assertIsNone(tree.selected_node)
                self.assertEqual(tree.expanded_keys, ())
                self.assertEqual(tuple(tree._positions), ("root",))

    def test_expansion_only_collapses_but_explicit_selection_reveals(self):
        nodes = (TreeNode("root", "Root", (
            TreeNode("branch", "Branch", (TreeNode("leaf", "Leaf"),)),
        )),)
        for batched in (False, True):
            with self.subTest(batched=batched):
                root = self.keep(Container())
                tree = TreeView(parent=root, nodes=nodes, selected_key="leaf")

                def update(**values):
                    if batched:
                        root.update_children({tree: values})
                    else:
                        tree.update(**values)

                update(expanded_keys=("root",))
                self.assertEqual(tree.selected_key, "branch")
                update(expanded_keys=())
                self.assertEqual(tree.selected_key, "root")
                update(expanded_keys=(), selected_key="leaf")
                self.assertEqual(tree.selected_key, "leaf")
                self.assertEqual(set(tree.expanded_keys), {"root", "branch"})
                tree.expanded_keys = ()
                self.assertEqual(tree.selected_key, "root")

    def test_failed_candidates_preserve_public_derived_and_sibling_state(self):
        changed = []

        class RestrictedTree(TreeView):
            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if name == "selected_key" and self.selected_node is not None:
                    if self.selected_node.text == "Blocked":
                        raise ValueError("blocked selection")
                if name == "expanded_keys" and "blocked_parent" in value:
                    raise ValueError("blocked expansion")

            def _changed(self, field, old, value):
                changed.append(field.name)
                super()._changed(field, old, value)

        candidates = (
            (NODES, {"selected_key": "missing"}),
            (NODES, {"expanded_keys": ("child",)}),
            (NODES, {"expanded_keys": ("root", "root")}),
            (NODES, {"selected_key": []}),
            ((TreeNode("same", "A"), TreeNode("same", "B")), {}),
            ((TreeNode("new", "Blocked"),), {"selected_key": "new"}),
            ((TreeNode("blocked_parent", "Parent", (TreeNode("new", "New"),)),),
             {"selected_key": "new"}),
        )
        for route in ROUTES:
            root = self.keep(Container())
            tree = RestrictedTree(parent=root, nodes=NODES, selected_key="child")
            before = tree_state(tree)
            changed.clear()
            for nodes, properties in candidates:
                with self.subTest(route=route, properties=properties):
                    with self.assertRaises((TypeError, ValueError)):
                        replace_tree(root, tree, route, nodes, **properties)
                    self.assertEqual(tree_state(tree), before)
                    self.assertEqual(changed, [])
            sibling = Button(parent=root, text="Original")
            with self.assertRaises(ValueError):
                root.update_children({
                    tree: {"nodes": (TreeNode("new", "New"),), "selected_key": "new"},
                    sibling: {"background": "bad color"},
                })
            self.assertEqual(tree_state(tree), before)
            self.assertEqual(sibling.text, "Original")
            self.assertEqual(changed, [])
            with self.assertRaises(ValueError):
                root.update_children({
                    sibling: {"text": "Must not change"},
                    tree: {"nodes": (TreeNode("new", "Blocked"),), "selected_key": "new"},
                })
            self.assertEqual(tree_state(tree), before)
            self.assertEqual(sibling.text, "Original")
            self.assertEqual(changed, [])


class TreeUpdateEventTests(AsyncUIOwnerTestCase):
    async def test_events_describe_only_final_selection_and_expansion(self):
        app = App()
        tree = TreeView(parent=app)
        observed = []

        def record(event):
            observed.append((event.property_name, event.old_value, event.new_value,
                             tree.selected_key, tree.selected_node,
                             tuple(tree._positions), dict(tree._parents)))

        tree.changed.connect(record)
        tree.expanded_changed.connect(record)
        runtime = Runtime(app, RecordingHost())
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            app.update_children({tree: {"selected_key": "child", "nodes": NODES}})
            await eventually(lambda: len(observed) == 2)
            self.assertEqual({row[:3] for row in observed}, {
                ("selected_key", None, "child"), ("expanded_keys", (), ("root",)),
            })
            expected = ("child", NODES[0].children[0], ("root", "child"),
                        {"root": None, "child": "root"})
            self.assertTrue(all(row[3:] == expected for row in observed))
            with self.assertRaises(ValueError):
                tree.update(nodes=(), selected_key="missing")
            await asyncio.sleep(0)
            self.assertEqual(len(observed), 2)
        finally:
            if not task.done():
                await runtime.request_close()
            await task
