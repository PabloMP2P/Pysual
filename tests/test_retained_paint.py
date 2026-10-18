"""Retained scene updates stay local and compose like direct tree painting."""

import gc
import random
from functools import wraps
from types import SimpleNamespace
from weakref import ref
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase, ui_timeout
from test_library import RecordingHost

from pysual import App, Button, Container, Control, Dropdown, Label, Rect, ScrollArea, Style, get_theme
from pysual import ListView, SegmentedControl, SplitPane, TabControl, TabPage, TextBox, dark
from pysual._paint_state import custom_paint, paint_inputs
from pysual._text_layout import TextLayoutCache
from pysual.layout import _clear_measurements, arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.render_cache import RenderCache
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

    def test_fixed_row_metric_change_repaints_unchanged_bounds(self):
        self.host.text_row_height = 16
        listing = ListView(parent=self.app, width=200, height=128,
                           items=tuple(f"Row {index}" for index in range(12)))
        self.frame(layout=True)
        bounds = listing.bounds
        self.assertEqual(sum(command[0] == "text" for command in self.host.snapshot()), 8)
        for row_height, expected_rows in ((32, 4), (16, 8)):
            with self.subTest(row_height=row_height):
                self.host.text_row_height = row_height
                listing.invalidate()
                self.frame(layout=True)
                self.assertEqual(listing.bounds, bounds)
                self.assertIn(self.retained._records[listing].identity, self.host.updates)
                self.assertEqual(sum(command[0] == "text" for command in self.host.snapshot()),
                                 expected_rows)
                self.assert_direct_matches()
        listing.invalidate()
        self.frame()
        self.assertEqual(self.host.updates, [])

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

    def test_enabling_cache_after_restoring_builtin_hook_removes_uncached_admission(self):
        label = Label(parent=self.app, text="Caption", cache_paint=False,
                      width=120, height=30)
        self.frame(layout=True)
        original = Label.paint

        def custom_paint(self, painter):
            return original(self, painter)

        with patch.object(Label, "paint", custom_paint):
            label.invalidate()
            self.frame()
            self.frame()
            self.assertIn(self.retained._records[label].identity, self.host.updates)
        # Enabling the cache must revoke uncached admission even when restoring
        # the original implementation makes the control a built-in again.
        label.cache_paint = True
        self.frame()
        self.assert_direct_matches()
        self.frame()
        self.assertEqual(self.host.updates, [])

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

    def test_unchanged_builtin_body_stays_on_the_host(self):
        label = Label(parent=self.app, text="Same", width=80, height=24)
        self.frame(layout=True)
        self.assertTrue(self.host.updates)
        label.invalidate()
        self.frame()
        self.assertEqual(self.host.updates, [])
        label.text = "Changed"
        self.frame()
        self.assertTrue(self.host.updates)

    def test_caption_prefetch_preserves_custom_styles_and_batches_builtins(self):
        class CaptionLabel(Label):
            style_parts = ("caption",)

            def measure(self, host):
                return 160, 30

            def paint(self, painter):
                style = painter.style("caption")
                painter.text(self.text, 0, 0, size=style.font_size,
                             color=style.foreground, font_family=style.font_family)

        batches = []

        def measure_many(texts, size, mono=False):
            batches.append(tuple(texts))
            return [self.host.measure(text, size, mono) for text in texts]

        from pysual._text_layout import TextLayoutCache
        self.host._text_layout_cache = TextLayoutCache()
        self.host.measure_many = measure_many
        Label(parent=self.app, text="Builtin", width=160, height=30)
        CaptionLabel(parent=self.app, text="Custom", top=40, width=160, height=30)
        self.frame(layout=True)
        self.assertEqual(len(batches), 1)
        self.assertIn("Builtin", batches[0])
        self.assertNotIn("Custom", batches[0])
        self.assert_direct_matches()

    def test_inherited_body_repaints_private_custom_focus_state(self):
        class FocusButton(Button):
            def _initialize(self):
                super()._initialize()
                self._focus_color = "#ff0000"

            def paint_focus(self, painter, /):
                painter.rect(Rect(0, 0, 12, 12), self._focus_color)

        button = FocusButton(parent=self.app, text="Focus", width=100, height=30)
        self.frame(layout=True)
        self.runtime.router.set_focus(button)
        self.frame()
        before = self.host.snapshot()
        button._focus_color = "#00ff00"
        button.invalidate()
        self.frame()
        self.assertIn(self.retained._records[button].identity, self.host.updates)
        self.assertNotEqual(before, self.host.snapshot())
        self.assert_direct_matches()

    def test_framework_subclasses_keep_unchanged_bodies_and_repaint_changes(self):
        from pysual import Hyperlink
        from pysual._controls.choices import _Choices
        from pysual.data_grid import _BooleanEditor, _CellEditor
        from pysual.dates import _CalendarEntry, _CalendarNavButton
        from pysual.editors import _ColorChannel, _ColorEntry

        cases = (
            (Hyperlink, {"text": "Before"}, {"text": "After"}),
            (_CalendarNavButton, {"text": "Before"}, {"text": "After"}),
            (_ColorChannel, {"value": 0.25}, {"value": 0.75}),
            (_CalendarEntry, {"text": "Before"}, {"text": "After"}),
            (_ColorEntry, {"text": "Before"}, {"text": "After"}),
            (_CellEditor, {"text": "Before"}, {"text": "After"}),
            (_Choices, {"items": ("First", "Second")}, {"selected_index": 1}),
            (_BooleanEditor, {"items": ("First", "Second")}, {"selected_index": 1}),
        )
        controls = [
            kind(parent=self.app, top=index * 40, width=160, height=30, **properties)
            for index, (kind, properties, _change) in enumerate(cases)
        ]
        self.frame(layout=True)
        before = self.host.snapshot()
        for control in controls:
            control.invalidate()
        self.frame()
        self.assertEqual(self.host.updates, [])
        self.assertEqual(self.host.snapshot(), before)
        for control, (_kind, _properties, change) in zip(controls, cases):
            control.update(**change)
        self.frame(layout=True)
        self.assertTrue({self.retained._records[control].identity for control in controls}
                        <= set(self.host.updates))
        self.assertNotEqual(self.host.snapshot(), before)
        self.assert_direct_matches()

    def test_inherited_body_repaints_overridden_property_state(self):
        class CaptionDropdown(Dropdown):
            def _initialize(self):
                super()._initialize()
                self._caption = "Before"

            @property
            def selected_item(self):
                return self._caption

        dropdown = CaptionDropdown(parent=self.app, width=160, height=30)
        self.frame(layout=True)
        before = self.host.snapshot()
        dropdown._caption = "After"
        dropdown.invalidate()
        self.frame()
        self.assertIn(self.retained._records[dropdown].identity, self.host.updates)
        self.assertNotEqual(before, self.host.snapshot())
        self.assert_direct_matches()

    def test_repeated_local_edits_release_reconcile_scratch_without_cyclic_gc(self):
        import gc

        box = Container(parent=self.app, layout="stack")
        labels = [Label(parent=box, text=f"Item {index}", height=20) for index in range(40)]
        self.frame(layout=True)
        gc.collect()
        gc.disable()
        try:
            for index in range(12):
                labels[0].text = f"Item {index}"
                self.frame(layout=True)
            retained = [
                obj for obj in gc.get_objects()
                if isinstance(obj, dict) and obj
                and type(next(iter(obj.values()))).__name__ == "_PaintSegment"
                and len(obj) > 10
            ]
            self.assertEqual(len(retained), 1)
        finally:
            gc.enable()
            gc.collect()

    def test_cell_edit_repaints_the_retained_body(self):
        from pysual import DataGrid, GridColumn, GridRow

        grid = DataGrid(
            parent=self.app,
            columns=(GridColumn("name", "Name", width=120),),
            rows=(GridRow("a", ("Before",)),),
            width=200,
            height=120,
        )
        self.frame(layout=True)
        before = self.host.snapshot()
        epoch = grid._visual_epoch
        grid.set_cell("a", "name", "After")
        self.assertEqual(grid._visual_epoch, epoch + 1)
        self.frame()
        self.assertIn(self.retained._records[grid].identity, self.host.updates)
        self.assertNotEqual(before, self.host.snapshot())
        self.assert_direct_matches()

    def test_nonvisual_property_keeps_the_retained_body(self):
        button = Button(parent=self.app, text="Go", width=80, height=30)
        self.frame(layout=True)
        self.assertEqual(button._visual_epoch, 0)
        button.shortcut = "Ctrl+G"
        self.assertEqual(button._visual_epoch, 1)
        self.frame()
        self.assertEqual(self.host.updates, [])


class PaintDependencyTests(UIOwnerTestCase):
    def fixture(self):
        app = App(width=640, height=400, reduce_motion=True)
        host = PatchRecordingHost()
        host._text_layout_cache = TextLayoutCache()
        runtime = Runtime(app, host)
        app._runtime = runtime
        retained = runtime._retained_paint = RetainedPaintTree(host)
        self.addCleanup(app.destroy)
        self.addCleanup(setattr, app, "_runtime", None)
        self.addCleanup(retained.clear)
        return app, host, runtime, retained

    def test_fitting_changed_captions_only_measure_requested_full_strings(self):
        app, host, runtime, retained = self.fixture()
        label = Label(parent=app, text="Initial", width=400, height=30)
        arrange(app, host)
        singles, batches = [], []
        raw = host.measure

        def measure(text, size, mono=False):
            singles.append(text)
            return raw(text, size, mono)

        def measure_many(texts, size, mono=False):
            texts = tuple(texts)
            batches.append(texts)
            # Deliberately no backend cache side effect. The shared service must
            # consume these values instead of asking measure() for them again.
            return [raw(text, size, mono) for text in texts]

        host.measure, host.measure_many = measure, measure_many
        for index in range(200):
            label.text = f"Caption {index}"
            paint_tree(app, host, retained=retained)
        self.assertEqual(singles, [])
        self.assertEqual(batches, [(f"Caption {index}",) for index in range(200)])
        self.assertFalse(hasattr(host, "_metrics"))

    def test_only_overflowing_caption_requests_truncation_candidates(self):
        app, host, runtime, retained = self.fixture()
        Label(parent=app, text="Fits", width=400, height=30)
        Label(parent=app, text="Overflow caption", width=20, height=30, top=40)
        arrange(app, host)
        batches = []

        def measure_many(texts, size, mono=False):
            texts = tuple(texts)
            batches.append(texts)
            return [host.measure(text, size, mono) for text in texts]

        host.measure_many = measure_many
        paint_tree(app, host, retained=retained)
        measured = [text for batch in batches for text in batch]
        self.assertEqual(measured.count("Fits"), 1)
        self.assertNotIn("Fits…", measured)
        self.assertIn("O…", measured)
        before = host.snapshot()
        paint_tree(app, host)
        self.assertEqual(before, tuple(host._drawing))

    def test_wrapped_custom_hook_never_inherits_framework_trust(self):
        app, host, runtime, retained = self.fixture()

        @wraps(Label.paint)
        def paint(self, painter):
            self._calls += 1
            painter.text(str(self._calls), 0, 0)

        class WrappedLabel(Label):
            pass

        WrappedLabel.paint = paint
        label = WrappedLabel(parent=app, width=100, height=30, cache_paint=False)
        label._calls = 0
        self.assertTrue(custom_paint(label))
        arrange(app, host)
        for _ in range(3):
            paint_tree(app, host, retained=retained)
        self.assertEqual(label._calls, 3)

    def test_custom_inherited_helpers_use_revision_invalidation(self):
        app, host, runtime, retained = self.fixture()

        class TallRows(ListView):
            def _row_height(self):
                return self._private_height

        listing = TallRows(parent=app, items=("one", "two", "three"),
                           width=120, height=90)
        listing._private_height = 20
        arrange(app, host)
        paint_tree(app, host, retained=retained)
        before = host.snapshot()
        listing._private_height = 40
        listing.invalidate()
        paint_tree(app, host, retained=retained)
        self.assertNotEqual(before, host.snapshot())
        expected = host.snapshot()
        paint_tree(app, host)
        self.assertEqual(expected, tuple(host._drawing))

    def test_inherited_opt_out_keeps_private_navigation_state_visible(self):
        app, host, runtime, retained = self.fixture()
        choice = SegmentedControl(parent=app, items=("First", "Second"),
                                  width=220, height=36)
        arrange(app, host)
        choice._hover = True
        choice._hot_part = 0
        paint_tree(app, host, retained=retained)
        before = host.snapshot()
        choice._hot_part = 1
        choice.invalidate()
        paint_tree(app, host, retained=retained)
        self.assertNotEqual(before, host.snapshot())
        expected = host.snapshot()
        paint_tree(app, host)
        self.assertEqual(expected, tuple(host._drawing))

    def test_instance_editor_hooks_repaint_after_explicit_invalidation(self):
        app, host, runtime, retained = self.fixture()
        editor = TextBox(parent=app, text="Input", width=120, height=32)
        arrange(app, host)
        paint_tree(app, host, retained=retained)
        for name in ("_paint_inputs", "_prepare_paint", "paint_line"):
            with self.subTest(hook=name):
                original = getattr(editor, name)

                @wraps(original)
                def replacement(*args, **kwargs):
                    return original(*args, **kwargs)

                object.__setattr__(editor, name, replacement)
                try:
                    editor.invalidate()
                    paint_tree(app, host, retained=retained)
                    self.assertIn(retained._records[editor].identity, host.updates)
                finally:
                    object.__delattr__(editor, name)
                editor.invalidate()
                paint_tree(app, host, retained=retained)
                editor.invalidate()
                paint_tree(app, host, retained=retained)
                self.assertEqual(host.updates, [])

    def test_dynamic_custom_classes_are_collectable_after_hook_checks(self):
        references = []

        def create():
            class DynamicLabel(Label):
                def paint(self, painter):
                    # This closure deliberately keeps a reference to its class.
                    super().paint(painter)

            control = DynamicLabel()
            self.assertTrue(custom_paint(control))
            self.assertIsInstance(
                paint_inputs(control, PatchRecordingHost(), None, control.effective_theme)[0], int
            )
            control.destroy()
            references.append(ref(DynamicLabel))

        for _ in range(200):
            create()
        gc.collect()
        self.assertFalse(any(reference() is not None for reference in references))

    def test_preparation_finishes_caret_and_composition_before_either_cache_key(self):
        for composition in ("", "Preview text\nsecond preview row"):
            with self.subTest(composition=composition):
                app, host, runtime, retained = self.fixture()
                host.capabilities = host.capabilities | {"render_surfaces"}
                editor = TextBox(parent=app, text="A long final line extending beyond the editor",
                                 multiline=True, width=140, height=48)
                arrange(app, host)
                runtime.router.set_focus(editor)
                editor._caret = editor._anchor = len(editor.text)
                editor._composition = composition
                editor._reveal_after_paint = True
                cache = RenderCache(host)
                surface_key = cache.paint_key(editor, editor, app.effective_theme)
                self.assertFalse(editor._reveal_after_paint)
                self.assertGreater(editor._scroll_x, 0)
                segment_key = retained._key(editor, editor)
                self.assertEqual(surface_key, segment_key)
                paint_tree(app, host, editor, retained=retained)
                self.assertEqual(surface_key, paint_inputs(editor, host, editor,
                                                         app.effective_theme))
                before = host.snapshot()
                paint_tree(app, host, editor)
                self.assertEqual(before, tuple(host._drawing))

    def test_replacing_builtin_hook_repaints_each_invalidation(self):
        app, host, runtime, retained = self.fixture()
        label = Label(parent=app, text="Body", width=120, height=32)
        arrange(app, host)
        paint_tree(app, host, retained=retained)
        original = Label.paint
        calls = []

        @wraps(original)
        def replaced(self, painter):
            calls.append(self)
            original(self, painter)

        try:
            Label.paint = replaced
            for _ in range(3):
                label.invalidate()
                paint_tree(app, host, retained=retained)
            self.assertEqual(calls, [label] * 3)
        finally:
            Label.paint = original

    def test_measurement_callback_replacing_editor_hook_invalidates_prepared_contract(self):
        app, host, runtime, retained = self.fixture()
        editor = TextBox(parent=app, text="A long caption needing caret preparation",
                         width=100, height=40)
        arrange(app, host)
        runtime.router.set_focus(editor)
        host._text_layout_cache.clear()
        original, raw_measure = TextBox.paint, host.measure
        measured = []

        @wraps(original)
        def replaced(self, painter):
            return original(self, painter)

        def measure(text, size, mono=False):
            measured.append(text)
            TextBox.paint = replaced
            return raw_measure(text, size, mono)

        host.measure = measure
        try:
            snapshot = paint_inputs(editor, host, editor, app.effective_theme)
            self.assertTrue(measured)
            self.assertIsInstance(snapshot[0], int)
        finally:
            TextBox.paint = original

    def test_custom_body_snapshot_calls_its_layout_hook_once(self):
        app, host, runtime, retained = self.fixture()

        class CustomLabel(Label):
            def _paint_layout_key(self):
                self._layout_reads += 1
                return self._layout_reads

        label = CustomLabel(parent=app, text="Custom", width=100, height=30)
        label._layout_reads = 0
        arrange(app, host)
        before = label._layout_reads
        key = retained._key(label, None)
        self.assertIsInstance(key.state, int)
        self.assertEqual(label._layout_reads, before + 1)

    def test_user_registration_keeps_uncached_custom_callbacks(self):
        from pysual import register_control
        from pysual._registry import _registrations

        calls = []

        class RegisteredLabel(Label):
            @wraps(Label.paint)
            def paint(self, painter):
                calls.append(self)
                return super().paint(painter)

        register_control(RegisteredLabel, name="paint_dependency_test", factory=False)
        self.addCleanup(_registrations.pop, "paint_dependency_test")
        app, host, runtime, retained = self.fixture()
        label = RegisteredLabel(parent=app, text="Custom", cache_paint=False,
                                width=120, height=32)
        arrange(app, host)
        for _ in range(3):
            paint_tree(app, host, retained=retained)
        self.assertEqual(calls, [label] * 3)

    def test_reinstalling_builtin_catalog_keeps_replacement_callbacks(self):
        from pysual._catalog import install_builtin_controls

        app, host, runtime, retained = self.fixture()
        label = Label(parent=app, text="Original", width=120, height=32)
        arrange(app, host)
        paint_tree(app, host, retained=retained)
        original = Label.paint
        calls = []

        @wraps(original)
        def replaced(self, painter):
            calls.append(self)
            return original(self, painter)

        try:
            Label.paint = replaced
            install_builtin_controls()
            for _ in range(3):
                label.invalidate()
                paint_tree(app, host, retained=retained)
            self.assertEqual(calls, [label] * 3)
        finally:
            Label.paint = original


class IncrementalPaintTests(UIOwnerTestCase):
    def fixture(self, grid_type=Container):
        app = App(width=640, height=400, reduce_motion=True, theme=dark())
        host = PatchRecordingHost()
        grid = grid_type(parent=app, layout="grid", columns=3,
                         width=600, height=360, spacing=3)
        labels = [Label(parent=grid, text=f"Cell {index}") for index in range(12)]
        runtime = Runtime(app, host)
        app._runtime = runtime
        retained = runtime._retained_paint = RetainedPaintTree(host)
        self.addCleanup(app.destroy)
        self.addCleanup(setattr, app, "_runtime", None)
        self.addCleanup(retained.clear)
        return app, grid, labels, host, retained

    def assert_equivalent(self, actual, reference):
        app, grid, labels, host, retained = actual
        other, other_grid, other_labels, other_host, _ = reference
        arrange(app, host)
        paint_tree(app, host, retained=retained, layout_changed=True)

        # This reference discards layout history, then paints every body without
        # a retained planner or surface cache. Disabling cache_measure alone
        # would not bypass an incorrectly reused placement plan.
        _clear_measurements(other)
        arrange(other, other_host)
        paint_tree(other, other_host)
        self.assertEqual(host.snapshot(), tuple(other_host._drawing))
        for control, expected in zip((app, grid, *labels), (other, other_grid, *other_labels)):
            self.assertEqual(control.bounds, expected.bounds)
            self.assertEqual(control._clip, expected._clip)
            self.assertEqual(control._paint_clip, expected._paint_clip)

    def test_mixed_mutations_match_fresh_layout_and_direct_drawing(self):
        actual, reference = self.fixture(), self.fixture()
        randomizer = random.Random(5402)
        self.assert_equivalent(actual, reference)
        for step in range(72):
            operation = step % 12
            index = randomizer.randrange(len(actual[2]))
            amount = randomizer.randrange(1, 8)
            for app, grid, labels, host, _ in (actual, reference):
                child = labels[index]
                if operation == 0:
                    child.text = f"Changed {step}: café λ " * amount
                elif operation == 1:
                    child.update(width=35 + amount * 3, height=18 + amount, margin=amount / 2)
                elif operation == 2:
                    child.visible = not child.visible
                elif operation == 3:
                    grid.padding = amount
                elif operation == 4:
                    grid.column_tracks = ("auto", "*", "2*") if step % 24 == 4 else ()
                elif operation == 5:
                    host.size = (640 + amount * 10, 400 + amount * 5)
                    host.resource_revision += 1
                elif operation == 6:
                    app.theme = dark().derive(font_size=12 + amount)
                elif operation == 7:
                    child.invalidate(Dirty.MEASURE)
                elif operation == 8:
                    labels.append(Label(parent=grid, text=f"New {step}"))
                elif operation == 9:
                    labels.pop().destroy()
                elif operation == 10:
                    grid.update(spacing=amount, width=550 + amount * 5)
                else:
                    grid.update_children({labels[0]: {"text": f"Batch {step}", "margin": amount},
                                          labels[1]: {"visible": True, "width": None}})
            with self.subTest(step=step, operation=operation):
                self.assert_equivalent(actual, reference)

    def test_custom_content_bounds_keeps_descendant_dependencies(self):
        class ContentGutter(Container):
            def content_bounds(self, bounds):
                text = self.children[0].text if self.children else ""
                return super().content_bounds(bounds).inset(len(text) % 11)

        actual, reference = self.fixture(ContentGutter), self.fixture(ContentGutter)
        self.assert_equivalent(actual, reference)
        for text in ("a", "a much longer caption", "", "漢字 café"):
            for fixture in (actual, reference):
                fixture[2][0].text = text
            self.assert_equivalent(actual, reference)
