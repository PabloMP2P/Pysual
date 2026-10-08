"""Pointer queries inspect nearby controls and keep existing routing policies."""

from types import SimpleNamespace
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Button, Container, Dropdown, Rect, ScrollArea, get_theme
from pysual.host import Input
from pysual.input import Router, _HitIndex
from pysual.layout import arrange
from pysual.runtime import Runtime


class HitIndexTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=960, height=640, reduce_motion=True)
        self.host = RecordingHost()
        self.host.size = 960, 640
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.app._attach_dispatcher(self.runtime.dispatcher)
        self.router = self.runtime.router

    def tearDown(self):
        self.app._runtime = None
        self.app.destroy()

    def assert_direct_matches(self, scope, points):
        direct = Router(None)
        for x, y in points:
            with self.subTest(point=(x, y)):
                self.assertIs(self.router.hit(scope, x, y), direct.hit(scope, x, y))

    def test_heavy_pointer_move_and_click_inspect_nearby_controls_only(self):
        counts = []
        for count in (2000, 5000):
            with self.subTest(controls=count):
                field = Container(parent=self.app, width=920, height=600)
                cells = [Button(parent=field, left=index % 100 * 9, top=index // 100 * 11,
                                width=8, height=10)
                         for index in range(count)]
                arrange(self.app, self.host)
                index = self.router._hit_index
                self.assertIsNotNone(index, "Layout must prepare input geometry before the first pointer")
                original_contains, inspected = Rect.contains, [0]

                def contains(rect, x, y):
                    inspected[0] += 1
                    return original_contains(rect, x, y)

                with patch.object(Rect, "contains", contains):
                    for target in (cells[0], cells[count // 2], cells[-1]):
                        x, y = target._rect.x + 2, target._rect.y + 2
                        for kind in ("pointer_move", "pointer_down", "pointer_up"):
                            before = inspected[0]
                            self.router.process(Input(kind, x, y))
                            counts.append(inspected[0] - before)
                        self.assertIs(self.router.hover, target)
                        self.assertIs(self.router.focus, target)
                        self.assertIsNone(self.router.capture)
                self.assertIs(self.router._hit_index, index, "Interactions must reuse prepared geometry")
                field.destroy()
                self.router.reconcile()
        self.assertLess(max(counts), 32, "An event inspected distant controls in the dense field")

    def test_policy_topology_and_scroll_changes_match_recursive_hits(self):
        field = ScrollArea(parent=self.app, left=30, top=30, width=180, height=110)
        first = Button(parent=field, width=100, height=35)
        second = Button(parent=field, top=200, width=100, height=35)
        arrange(self.app, self.host)
        points = [(40, 40), (40, 100), (40, 230), (300, 50)]
        self.assert_direct_matches(self.app, points)
        first.enabled = False
        self.assert_direct_matches(self.app, points)
        first.enabled = True
        first.visible = False
        self.assert_direct_matches(self.app, points)
        field.scroll_y = 150
        arrange(self.app, self.host)
        self.assert_direct_matches(self.app, points)
        second.destroy()
        replacement = Button(parent=field, top=180, width=100, height=35)
        arrange(self.app, self.host)
        self.assert_direct_matches(self.app, points)
        self.assertIs(self.router.hit(self.app, replacement._clip.x + 2,
                                     replacement._clip.y + 2), replacement)

    def test_custom_inherited_enabled_policy_is_checked_on_current_candidates(self):
        class Policy(Container):
            def _initialize(self):
                super()._initialize()
                self._allowed = True

            @property
            def effective_enabled(self):
                return self._allowed and super().effective_enabled

        parent = Policy(parent=self.app, width=200, height=100)
        child = Button(parent=parent, width=100, height=35)
        arrange(self.app, self.host)
        index = self.router._hit_index
        self.assertIs(self.router.hit(self.app, 10, 10), child)
        parent._allowed = False
        parent.invalidate()
        self.assertIs(self.router.hit(self.app, 10, 10), self.app)
        self.assertIsNone(self.router.hit(parent, 10, 10))
        self.router.set_focus(child)
        self.assertIsNone(self.router.focus)
        self.assertIs(self.router._hit_index, index)
        parent._allowed = True
        self.assertIs(self.router.hit(self.app, 10, 10), child)

    def test_overlay_raise_and_scope_preserve_existing_hit_order(self):
        class Overlay(Container):
            _overlay = True

        first = Overlay(parent=self.app, width=180, height=90)
        child = Button(parent=first, width=80, height=30)
        second = Overlay(parent=self.app, left=90, width=180, height=90)
        arrange(self.app, self.host)
        self.assertIs(self.router.hit(self.app, 100, 40), second)
        self.router.process(Input("pointer_down", 10, 10))
        self.router.process(Input("pointer_up", 10, 10))
        self.assertIs(self.router.hit(self.app, 100, 40), first)
        self.assertIs(self.router.hit(first, 10, 10), child)
        self.assertIsNone(self.router.hit(second, 10, 10))
        self.runtime._modality._openings.append(
            SimpleNamespace(control=second, presentation=SimpleNamespace(modal=True))
        )
        # Modal painting raises its subtree above ordinary/modeless siblings.
        self.assertIs(self.router.hit(self.app, 100, 40), second)
        self.assertIs(self.router.hit(second, 230, 40), second)

    def test_descendants_cannot_escape_ancestor_hit_clip(self):
        parent = Container(parent=self.app, width=100, height=60)
        child = Button(parent=parent, width=80, height=30)
        arrange(self.app, self.host)
        # Custom geometry can have a child's clip outside its parent's body.
        # Recursive hit testing still rejects that entire subtree at the parent.
        child._clip = Rect(120, 10, 80, 30)
        self.router.layout_changed()
        self.assertIs(self.router.hit(self.app, 130, 20), self.app)
        self.assert_direct_matches(self.app, [(10, 10), (130, 20)])

    def test_capture_keeps_pointer_identity_and_hover_hit_separate(self):
        first = Button(parent=self.app, width=100, height=35)
        second = Button(parent=self.app, left=150, width=100, height=35)
        arrange(self.app, self.host)
        self.router.process(Input("pointer_down", 10, 10, pointer_id=7))
        self.router.process(Input("pointer_move", 160, 10, pointer_id=8))
        self.assertIs(self.router.capture, first)
        self.router.process(Input("pointer_move", 160, 10, pointer_id=7))
        self.assertIs(self.router.capture, first)
        self.assertIs(self.router.hover, second)
        self.router.process(Input("pointer_cancel", pointer_id=7))
        self.assertIsNone(self.router.capture)

    def test_popup_open_close_reuses_heavy_main_index_and_indexes_only_the_popup(self):
        self.app.theme = get_theme("neon")
        self.app._state = "RUNNING"
        for count in (100, 2000, 5000):
            with self.subTest(controls=count):
                field = Container(parent=self.app, width=760, height=600)
                cells = [Button(parent=field, left=index % 100 * 7,
                                top=index // 100 * 11, width=6, height=10)
                         for index in range(count)]
                owner = Dropdown(parent=self.app, left=800, top=15, width=140,
                                 height=34, items=tuple(str(i) for i in range(12)))
                arrange(self.app, self.host)
                main = self.router._hit_index
                with patch("pysual.input._HitIndex", wraps=_HitIndex) as builds:
                    owner.open()
                    popup = self.runtime.popup
                    arrange(self.app, self.host)
                    popup.reposition()
                    arrange(self.app, self.host)
                    self.assertIs(self.router._hit_index, main)
                    self.assertTrue(builds.call_args_list)
                    self.assertTrue(all(call.args[0] is popup for call in builds.call_args_list))
                    self.assertLessEqual(len(self.router._popup_hit_index.metadata), 2)
                    self.assertIs(self.router.hit(popup, popup._clip.x + 2,
                                                  popup._clip.y + 2), popup._children[0])
                    popup.dismiss()
                    arrange(self.app, self.host)
                    self.assertIs(self.router._hit_index, main)
                    self.assertIsNone(self.router._popup_hit_index)
                    self.assertTrue(all(call.args[0] is popup for call in builds.call_args_list))
                self.assertIs(self.router.hit(self.app, cells[-1]._clip.x + 2,
                                             cells[-1]._clip.y + 2), cells[-1])
                owner.destroy()
                field.destroy()
                self.router.reconcile()
        self.app._state = "CREATED"

    def test_popup_keeps_live_main_topology_and_visibility_changes(self):
        self.app._state = "RUNNING"
        owner = Dropdown(parent=self.app, width=140, height=34, items=("One", "Two"))
        first = Button(parent=self.app, top=330, width=100, height=35)
        hidden = Button(parent=self.app, left=150, top=330, width=100, height=35,
                        visible=False)
        arrange(self.app, self.host)
        owner.open()
        popup = self.runtime.popup
        arrange(self.app, self.host)
        main = self.router._hit_index
        first.enabled = False
        self.assertIs(self.router.hit(self.app, 10, 340), self.app)
        self.assertIs(self.router._hit_index, main)
        hidden.visible = True
        new = Button(parent=self.app, left=300, top=330, width=100, height=35)
        arrange(self.app, self.host)
        self.assertIsNot(self.router._hit_index, main)
        main = self.router._hit_index
        self.assertIs(self.router.hit(self.app, 160, 340), hidden)
        self.assertIs(self.router.hit(self.app, 310, 340), new)
        new.left = 450
        arrange(self.app, self.host)
        self.assertIsNot(self.router._hit_index, main)
        self.assertIs(self.router.hit(self.app, 310, 340), self.app)
        self.assertIs(self.router.hit(self.app, 460, 340), new)
        popup.dismiss()
        arrange(self.app, self.host)
        self.assertIs(self.router.hit(self.app, 460, 340), new)
        self.app._state = "CREATED"

    def test_clear_releases_geometry_and_input_control_references(self):
        button = Button(parent=self.app, width=100, height=35)
        arrange(self.app, self.host)
        self.router.process(Input("pointer_move", 10, 10))
        self.router.process(Input("pointer_down", 10, 10))
        self.assertIs(self.router.capture, button)
        self.router.clear()
        self.assertIsNone(self.router._hit_index)
        self.assertIsNone(self.router._popup_hit_index)
        self.assertIsNone(self.router._popup_hit_scope)
        self.assertEqual(self.router._hit_dirty, set())
        self.assertIsNone(self.router.hover)
        self.assertIsNone(self.router.capture)
        self.assertIsNone(self.router.focus)
