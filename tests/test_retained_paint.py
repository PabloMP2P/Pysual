"""Retained scene updates stay local and compose like direct tree painting."""

from types import SimpleNamespace
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase, ui_timeout
from test_library import RecordingHost

from pysual import App, Button, Container, Control, Dropdown, Rect, ScrollArea, Style, get_theme
from pysual import SplitPane, TabControl, TabPage
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.runtime import Runtime
from pysual.schema import Dirty


class PatchRecordingHost(RecordingHost):
    capabilities = RecordingHost.capabilities | {"scene_patches"}
    native_retained = True
    render_scale = 1
    resource_revision = 0

    def __init__(self):
        super().__init__()
        self.segments, self.order = {}, []
        self.updates, self.removed = [], []
        self._clip = None
        self._drawing = []

    def begin(self, background):
        self._clip = None
        self._drawing = []

    def begin_scene(self, background):
        self.updates, self.removed = [], []

    def begin_segment(self, identity, bounds):
        self._segment = identity
        self._clip = None
        self._drawing = []

    def end_segment(self):
        self.updates.append(self._segment)
        self.segments[self._segment] = tuple(self._drawing)

    def end_scene(self, order=None, remove=()):
        self.removed = list(remove)
        for identity in remove:
            self.segments.pop(identity, None)
        if order is not None:
            self.order = list(order)
        assert set(self.order) == set(self.segments)
        self.present()

    def clip(self, rect):
        self._clip = rect

    def rect(self, *args, **kwargs):
        self._drawing.append(("rect", self._clip, args, kwargs))

    def line(self, *args, **kwargs):
        self._drawing.append(("line", self._clip, args, kwargs))

    def text(self, *args, **kwargs):
        self._drawing.append(("text", self._clip, args, kwargs))

    def image(self, *args, **kwargs):
        self._drawing.append(("image", self._clip, args, kwargs))

    def request_transition(self, duration):
        pass

    def snapshot(self):
        return tuple(command for identity in self.order for command in self.segments[identity])


class CountingButton(Button):
    def _initialize(self):
        super()._initialize()
        self._paints = self._theme_reads = 0

    @property
    def effective_theme(self):
        self._theme_reads += 1
        return super().effective_theme

    def paint(self, painter, /):
        self._paints += 1
        super().paint(painter)


class RetainedPaintTests(UIOwnerTestCase):
    def setUp(self):
        self.app, self.host = App(width=640, height=400), PatchRecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.retained = self.runtime._retained_paint = RetainedPaintTree(self.host)

    def tearDown(self):
        self.app._runtime = None
        self.app.destroy()

    def frame(self, *, layout=False):
        if layout:
            arrange(self.app, self.host)
        paint_tree(self.app, self.host, self.runtime.router.focus,
                   retained=self.retained, layout_changed=layout)

    def assert_direct_matches(self):
        retained = self.host.snapshot()
        paint_tree(self.app, self.host, self.runtime.router.focus)
        self.assertEqual(retained, tuple(self.host._drawing))

    def test_batch_flush_keeps_local_changes_from_custom_invalidate(self):
        class StatusContainer(Container):
            def invalidate(self, affects=Dirty.PAINT):
                if getattr(self, "_children", ()):
                    self.background = (
                        "#ff0000" if self._children[0].text == "After" else "#00ff00"
                    )
                super().invalidate(affects)

        parent = StatusContainer(parent=self.app, width=200, height=100)
        child = Button(parent=parent, text="Before", width=100, height=30)
        self.frame(layout=True)
        before = parent._paint_local_revision
        parent.update_children({child: {"text": "After"}})
        self.assertEqual(parent.background, "#ff0000")
        self.assertGreater(parent._paint_local_revision, before)
        self.assertIn(parent, self.retained._dirty)
        self.frame(layout=True)
        self.assert_direct_matches()

    def test_hover_and_focus_touch_only_changed_controls_without_clean_tree_reads(self):
        controls = [CountingButton(parent=self.app, text=str(index), width=30, height=24,
                                   left=index % 20 * 31, top=index // 20 * 25)
                    for index in range(160)]
        self.frame(layout=True)
        baseline = [(control._paints, control._theme_reads) for control in controls]
        self.runtime.router._set_hover(controls[31])
        self.frame()
        self.assertEqual(len(self.host.updates), 1)
        for index, control in enumerate(controls):
            if index != 31:
                self.assertEqual((control._paints, control._theme_reads), baseline[index])
        self.runtime.router.set_focus(controls[31])
        self.frame()
        self.assertEqual(len(self.host.updates), 1)
        self.runtime.router.set_focus(controls[32])
        self.frame()
        self.assertEqual(len(self.host.updates), 2)
        self.assert_direct_matches()

    def test_reflow_add_remove_and_visibility_reuse_stable_identity_and_unaffected_bodies(self):
        parent = Container(parent=self.app, layout="stack", width=160, height=220)
        first = CountingButton(parent=parent, text="First", width=120, height=30)
        second = CountingButton(parent=parent, text="Second", width=120, height=30)
        outsider = CountingButton(parent=self.app, text="Stable", left=250, width=120, height=30)
        self.frame(layout=True)
        identity = self.retained._records[second].identity
        outsider_paints = outsider._paints
        first.visible = False
        self.frame(layout=True)
        self.assertEqual(self.retained._records[second].identity, identity)
        self.assertTrue(self.host.removed)
        self.assertEqual(outsider._paints, outsider_paints)
        self.assert_direct_matches()
        first.visible = True
        third = CountingButton(parent=parent, text="Third", width=120, height=30)
        self.frame(layout=True)
        self.assertEqual(self.retained._records[second].identity, identity)
        third.destroy()
        self.frame(layout=True)
        self.assert_direct_matches()

    @ui_timeout(30)
    def test_neon_dropdown_reuses_5000_clean_controls_through_repeated_open_close(self):
        self.app.theme = get_theme("neon")
        field = Container(parent=self.app, top=50, width=520, height=320)
        controls = [CountingButton(parent=field, text=str(index), width=5, height=6,
                                   left=index % 100 * 5.2, top=index // 100 * 6.4)
                    for index in range(5000)]
        dropdown = Dropdown(parent=self.app, left=530, top=10, width=100, height=34,
                            items=tuple(str(index) for index in range(12)))
        self.runtime._paint_frame()
        baseline = [(control._paints, control._theme_reads) for control in controls]
        identities = [self.retained._records[control].identity for control in controls]
        main_index = self.runtime.router._hit_index
        self.app._state = "RUNNING"
        try:
            for cycle in range(3):
                with self.subTest(cycle=cycle):
                    with patch.object(self.retained, "_key", wraps=self.retained._key) as keys:
                        dropdown.open()
                        self.runtime._paint_frame()
                        self.assertLess(len(keys.call_args_list), 12)
                    popup = self.runtime.popup
                    self.assertTrue(popup.is_open)
                    self.assertIs(self.runtime.router._hit_index, main_index)
                    self.assertEqual([(c._paints, c._theme_reads) for c in controls], baseline)
                    self.assertEqual([self.retained._records[c].identity for c in controls], identities)
                    with patch.object(self.retained, "_key", wraps=self.retained._key) as keys:
                        popup.dismiss()
                        self.runtime._paint_frame()
                        self.assertLess(len(keys.call_args_list), 12)
                    self.assertTrue(self.host.removed)
                    self.assertIs(self.runtime.router._hit_index, main_index)
                    self.assertEqual([(c._paints, c._theme_reads) for c in controls], baseline)
            self.assert_direct_matches()
        finally:
            if self.runtime.popup is not None:
                self.runtime.popup.dismiss()
            self.app._state = "CREATED"

    def test_cached_subtree_offsets_survive_insertion_removal_and_inherited_changes(self):
        first = Container(parent=self.app, width=160, height=150)
        child = CountingButton(parent=first, text="First", width=100, height=30)
        stable = Container(parent=self.app, left=200, width=160, height=150)
        last = CountingButton(parent=stable, text="Last", width=100, height=30)
        self.frame(layout=True)
        last_reads = last._theme_reads
        for index in range(3):
            added = Button(parent=first, text=str(index), top=40, width=100, height=30)
            self.frame(layout=True)
            self.assertEqual(last._theme_reads, last_reads)
            added.destroy()
            self.frame(layout=True)
            self.assertEqual(last._theme_reads, last_reads)
        child_paints = child._paints
        first.theme_override = get_theme("neon")
        self.frame(layout=True)
        self.assertGreater(child._paints, child_paints)
        self.assertEqual(last._theme_reads, last_reads)
        self.assert_direct_matches()

    def test_cache_admission_changes_during_unrelated_topology_reconciliation(self):
        custom = CountingButton(parent=self.app, text="Custom", width=100, height=30)
        sibling = Container(parent=self.app, left=200, width=150, height=150)
        self.frame(layout=True)
        custom.cache_paint = False
        Button(parent=sibling, text="Added", width=100, height=30)
        self.frame(layout=True)
        before = custom._paints
        self.frame()
        self.assertEqual(custom._paints, before + 1)
        custom.cache_paint = True
        sibling._children[0].destroy()
        self.frame(layout=True)
        before = custom._paints
        self.frame()
        self.assertEqual(custom._paints, before)

    def test_inherited_enabled_and_resource_changes_refresh_descendants(self):
        parent = Container(parent=self.app, width=180, height=100)
        button = CountingButton(parent=parent, text="Child", width=100, height=30)
        self.frame(layout=True)
        before = button._paints
        parent.enabled = False
        self.frame()
        self.assertGreater(button._paints, before)
        self.assert_direct_matches()
        before = button._paints
        self.host.resource_revision += 1
        self.frame()
        self.assertGreater(button._paints, before)

    def test_tab_headers_refresh_for_selected_and_hidden_page_properties(self):
        tabs = TabControl(parent=self.app, width=400, height=220)
        pages = [TabPage(parent=tabs, title=f"Page {index}") for index in range(2)]
        outside = CountingButton(parent=self.app, left=450, width=140, height=30)
        self.frame(layout=True)
        tab_identity = self.retained._records[tabs].identity
        self.assertFalse(pages[1].visible)
        for page in pages:
            for field, value in (("title", "Renamed page"), ("icon", "search"),
                                 ("enabled", False), ("enabled", True)):
                with self.subTest(selected=page is pages[0], field=field, value=value):
                    outside_paints = outside._paints
                    setattr(page, field, value)
                    self.frame(layout=field == "title")
                    self.assertIn(tab_identity, self.host.updates)
                    self.assertEqual(outside._paints, outside_paints)
                    self.assert_direct_matches()

    def test_scrollbar_extent_updates_after_child_resize_without_repainting_on_hover(self):
        scroll = ScrollArea(parent=self.app, width=200, height=100)
        child = Button(parent=scroll, text="Child", width=100, height=40)
        outside = CountingButton(parent=self.app, left=250, width=140, height=30)
        self.frame(layout=True)
        identity = self.retained._records[scroll].identity
        for height in (200, 350, 40):
            with self.subTest(height=height):
                outside_paints = outside._paints
                child.height = height
                self.frame(layout=True)
                self.assertIn(identity, self.host.updates)
                self.assertEqual(outside._paints, outside_paints)
                self.assertEqual(scroll._scroll_limits[1] > 0, height > 100)
                self.assert_direct_matches()
        self.runtime.router._set_hover(child)
        self.frame()
        self.assertNotIn(identity, self.host.updates)
        self.assert_direct_matches()

    def test_split_divider_tracks_child_size_constraints_and_visibility(self):
        pane = SplitPane(parent=self.app, width=320, height=180)
        first = Container(parent=pane)
        second = Container(parent=pane)
        self.frame(layout=True)
        identity = self.retained._records[pane].identity
        for control, field, value in ((first, "min_width", 210),
                                      (second, "visible", False),
                                      (second, "visible", True)):
            with self.subTest(field=field, value=value):
                setattr(control, field, value)
                self.frame(layout=True)
                self.assertIn(identity, self.host.updates)
                self.assert_direct_matches()

    def test_custom_inherited_policy_repaints_only_descendants_when_policy_changes(self):
        class Policy(Container):
            def _initialize(self):
                super()._initialize()
                self._allowed = True

            @property
            def effective_enabled(self):
                return self._allowed and super().effective_enabled

        parent = Policy(parent=self.app, width=200, height=100)
        child = CountingButton(parent=parent, text="Child", width=100, height=30)
        outside = CountingButton(parent=self.app, left=250, width=140, height=30)
        self.frame(layout=True)
        for allowed in (False, True):
            with self.subTest(allowed=allowed):
                outside_state = outside._paints, outside._theme_reads
                parent._allowed = allowed
                parent.invalidate()
                self.frame()
                self.assertEqual(child.effective_enabled, allowed)
                self.assertIn(self.retained._records[child].identity, self.host.updates)
                self.assertEqual((outside._paints, outside._theme_reads), outside_state)
                self.assert_direct_matches()
        before = child._paints, child._theme_reads
        parent.invalidate()
        self.frame()
        self.assertEqual((child._paints, child._theme_reads), before)
        self.assertNotIn(self.retained._records[child].identity, self.host.updates)

    def test_shadow_clip_theme_and_dpi_changes_match_direct_painting(self):
        self.app.theme = self.app.theme.styled(
            Button, Style(fill="#eeeeee", shadow="#00000080", shadow_blur=24)
        )
        parent = Container(parent=self.app, left=30, top=30, width=180, height=100)
        button = CountingButton(parent=parent, text="Shadow", left=8, top=15,
                                width=100, height=40)
        self.frame(layout=True)
        previous_clip, previous_bounds = button._paint_clip, button.bounds
        parent.background = "#123456"
        self.frame(layout=True)
        self.assertEqual(button.bounds, previous_bounds)
        self.assertNotEqual(button._paint_clip, previous_clip)
        self.assert_direct_matches()
        self.app.theme = self.app.theme.styled(Button, Style(fill="#ffcc44", shadow="#00000080",
                                                            shadow_blur=12))
        self.frame(layout=True)
        self.assert_direct_matches()
        self.host.render_scale = 1.5
        self.frame()
        self.assert_direct_matches()

    def test_scrolling_updates_viewport_bodies_and_keeps_unrelated_segments(self):
        scroll = ScrollArea(parent=self.app, width=180, height=100)
        Button(parent=scroll, text="Scrollable", top=180, width=100, height=40)
        outside = CountingButton(parent=self.app, text="Stable", left=250,
                                 width=100, height=30)
        self.frame(layout=True)
        before = outside._paints
        scroll.scroll_y = 150
        self.frame(layout=True)
        self.assertEqual(outside._paints, before)
        self.assert_direct_matches()

    def test_drag_feedback_follows_children_and_is_removed_on_target_change(self):
        parent = Container(parent=self.app, width=180, height=100)
        Button(parent=parent, text="Child", width=100, height=30)
        other = Button(parent=self.app, text="Other", left=250, width=100, height=30)
        self.frame(layout=True)
        self.runtime.router.drop_target = parent
        self.frame()
        self.assertEqual(len(self.host.updates), 1)
        self.assert_direct_matches()
        self.runtime.router.drop_target = other
        self.frame()
        self.assertEqual(len(self.host.removed), 1)
        self.assert_direct_matches()
        self.runtime.router.drop_target = None
        self.frame()
        self.assertEqual(self.host.updates, [])
        self.assert_direct_matches()

    def test_modal_order_change_reorders_existing_segments_without_repainting(self):
        first = Button(parent=self.app, text="First", width=100, height=30)
        Button(parent=self.app, text="Second", left=30, width=100, height=30)
        self.frame(layout=True)
        previous = list(self.host.order)
        self.runtime._modality._openings.append(
            SimpleNamespace(control=first, presentation=SimpleNamespace(modal=True))
        )
        self.frame()
        self.assertNotEqual(self.host.order, previous)
        self.assertEqual(self.host.updates, [])
        self.assert_direct_matches()

    def test_custom_uncached_hooks_and_descendant_dependent_parent_remain_compatible(self):
        class CustomParent(Container):
            def _initialize(self):
                super()._initialize()
                self._paints = 0

            def paint(self, painter, /):
                self._paints += 1
                painter.rect(Rect(0, 0, 20, 20), self._children[0].foreground or "#ffffff")

        class CustomControl(Control):
            def _initialize(self):
                super()._initialize()
                self._paints = 0

            def paint(self, painter, /):
                self._paints += 1

        parent = CustomParent(parent=self.app, cache_paint=True, width=180, height=100)
        child = Button(parent=parent, text="Child", width=100, height=30)
        custom = CustomControl(parent=self.app, left=250, width=100, height=30)
        self.frame(layout=True)
        before = parent._paints
        child.foreground = "#123456"
        self.frame()
        self.assertEqual(parent._paints, before + 1)
        before = custom._paints
        self.app.request_frame()
        self.frame()
        self.assertEqual(custom._paints, before + 1)
        custom.cache_paint = True
        self.frame()
        before = custom._paints
        self.app.request_frame()
        self.frame()
        self.assertEqual(custom._paints, before)
        custom.cache_paint = False
        self.frame()
        before = custom._paints
        self.app.request_frame()
        self.frame()
        self.assertEqual(custom._paints, before + 1)

    def test_batched_descendant_origins_are_retained(self):
        parent = Container(parent=self.app, width=180, height=100)
        buttons = [Button(parent=parent, text=str(index), top=index * 35, width=100, height=30)
                   for index in range(2)]
        self.frame(layout=True)
        parent.update_children({button: {"foreground": "#123456"} for button in buttons})
        self.frame()
        self.assert_direct_matches()

    def test_failed_ack_retries_bodies_order_and_structural_removals(self):
        first = Button(parent=self.app, text="First", width=100, height=30)
        second = Button(parent=self.app, text="Second", left=150, width=100, height=30)
        self.frame(layout=True)
        removed_id = self.retained._records[first].identity
        committed_segments, committed_order = dict(self.host.segments), list(self.host.order)
        acknowledge = self.host.end_scene

        def fail_ack(order=None, remove=()):
            self.host.segments, self.host.order = committed_segments, committed_order
            raise RuntimeError("native scene acknowledgment failed")

        self.host.end_scene = fail_ack
        first.destroy()
        second.left = 200
        with self.assertRaisesRegex(RuntimeError, "acknowledgment failed"):
            self.frame(layout=True)
        self.host.end_scene = acknowledge
        self.frame()
        self.assertIn(removed_id, self.host.removed)
        self.assert_direct_matches()
