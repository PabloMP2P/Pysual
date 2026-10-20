"""Implementation changes use ordinary invalidation in retained and surface paint."""

import copy
import pickle
from unittest.mock import patch
from typing import get_type_hints

from _ui_testcase import UIOwnerTestCase
from test_render_cache import svg_drawing
from test_retained_paint import PatchRecordingHost
from test_web_svg import RecordingRenderer

from pysual import App, Container, Control, Label, ListView, Slider, SplitPane, TextBox
from pysual._paint_state import paint_inputs
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.render_cache import RenderCache
from pysual.runtime import Runtime


class ImplementationTests(UIOwnerTestCase):
    def fixture(self, kind, **properties):
        app = App(width=400, height=200, reduce_motion=True)
        host = PatchRecordingHost()
        runtime = Runtime(app, host)
        app._runtime = runtime
        retained = runtime._retained_paint = RetainedPaintTree(host)
        control = kind(parent=app, **properties)
        self.addCleanup(app.destroy)
        self.addCleanup(setattr, app, "_runtime", None)
        arrange(app, host)
        paint_tree(app, host, retained=retained)
        return app, control, host, retained

    def assert_repaint_matches_direct(self, app, control, host, retained):
        before = host.snapshot()
        control.invalidate()
        paint_tree(app, host, retained=retained)
        actual = host.snapshot()
        self.assertNotEqual(actual, before)
        self.assertIn(retained._records[control].identity, host.updates)
        paint_tree(app, host)
        self.assertEqual(actual, tuple(host._drawing))

    def test_instance_helpers_and_restoration_use_explicit_invalidation(self):
        cases = (
            (ListView, dict(items=("one", "two", "three"), row_height=20,
                            width=120, height=90), "_row_height", lambda: 40),
            (Slider, dict(value=25, width=180, height=35), "_track", lambda: (25, 100)),
            (TextBox, dict(text="Input", width=120, height=32), "_text_left", lambda: 50),
        )
        for kind, properties, helper, replacement in cases:
            with self.subTest(control=kind.__name__):
                app, control, host, retained = self.fixture(kind, **properties)
                object.__setattr__(control, helper, replacement)
                self.assert_repaint_matches_direct(app, control, host, retained)
                object.__delattr__(control, helper)
                self.assert_repaint_matches_direct(app, control, host, retained)
                control.invalidate()
                paint_tree(app, host, retained=retained)
                self.assertIn(retained._records[control].identity, host.updates)
                paint_tree(app, host, retained=retained)
                self.assertNotIn(retained._records[control].identity, host.updates)

    def test_class_helper_replacement_and_restoration_repaint(self):
        app, control, host, retained = self.fixture(Slider, value=25, width=180, height=35)
        with patch.object(Slider, "_track", lambda self: (25, 100)):
            self.assert_repaint_matches_direct(app, control, host, retained)
        self.assert_repaint_matches_direct(app, control, host, retained)

    def test_shadowed_base_paint_replacement_refreshes_retained_drawing(self):
        app, control, host, retained = self.fixture(
            Label, text="Caption", width=180, height=35,
        )

        def base_paint(self, painter):
            painter.text("From super", 0, 0)

        # Label.paint reaches Control.paint through super(); invalidation must
        # refresh that output without a separate declaration of the dependency.
        with patch.object(Control, "paint", base_paint):
            self.assert_repaint_matches_direct(app, control, host, retained)
        self.assert_repaint_matches_direct(app, control, host, retained)

    def test_shadowed_base_paint_replacement_refreshes_surface_drawing(self):
        app = App(width=320, height=192, reduce_motion=True, cache_paint=False)
        self.addCleanup(app.destroy)
        control = Label(parent=app, text="Caption", width=180, height=35)
        host = RecordingRenderer()
        host.reset_scene("Implementation invalidation", 320, 192)
        self.addCleanup(host.close_scene)
        cache = RenderCache(host)
        self.addCleanup(cache.clear)
        arrange(app, host)
        for _ in range(3):
            paint_tree(app, host, cache=cache)
        self.assertGreater(cache.stats.hits, 0)
        original = svg_drawing(host.export_svg())

        def base_paint(self, painter):
            painter.text("From super", 0, 0)

        with patch.object(Control, "paint", base_paint):
            control.invalidate()
            paint_tree(app, host, cache=cache)
            changed = svg_drawing(host.export_svg())
            self.assertNotEqual(changed, original)
            paint_tree(app, host)
            self.assertEqual(svg_drawing(host.export_svg()), changed)
            # Admit the replacement too, so restoring the base implementation
            # must reject a resident surface containing its previous drawing.
            paint_tree(app, host, cache=cache)
            paint_tree(app, host, cache=cache)
        control.invalidate()
        paint_tree(app, host, cache=cache)
        self.assertEqual(svg_drawing(host.export_svg()), original)

    def test_inherited_helper_changes_repaint_after_invalidation(self):
        app, control, host, retained = self.fixture(
            ListView, items=("one", "two", "three"), row_height=20, width=120, height=90,
        )
        with patch.object(Control, "effective_row_height", lambda self, fallback: 40):
            self.assert_repaint_matches_direct(app, control, host, retained)
        self.assert_repaint_matches_direct(app, control, host, retained)

    def test_new_class_drawing_dependency_needs_only_explicit_invalidation(self):
        original = Label.paint

        def paint(control, painter):
            original(control, painter)
            painter.text(control._drawing_caption, 0, 0)

        with (patch.object(Label, "paint", paint),
              patch.object(Label, "_drawing_caption", "Before", create=True)):
            app, control, host, retained = self.fixture(Label, width=180, height=35)
            Label._drawing_caption = "After"
            self.assert_repaint_matches_direct(app, control, host, retained)
            Label._drawing_caption = "Before"
            self.assert_repaint_matches_direct(app, control, host, retained)

    def test_annotation_introspection_preserves_reuse_for_related_controls(self):
        fixtures = [
            self.fixture(kind, width=180, height=35)
            for kind in (Slider, Label, TextBox)
        ]
        # Python 3.14 lazily adds __annotations_cache__ along the MRO. Earlier
        # versions can similarly materialize __annotations__ on first access.
        get_type_hints(Slider)
        for app, control, host, retained in fixtures:
            with self.subTest(control=type(control).__name__):
                key = paint_inputs(control, host, None, app.effective_theme)
                paint_tree(app, host, retained=retained)
                self.assertNotIn(retained._records[control].identity, host.updates)
                self.assertEqual(paint_inputs(control, host, None, app.effective_theme), key)

    def test_copy_and_pickle_metadata_preserve_unchanged_body_reuse(self):
        detached = Label(text="Copy source")
        self.addCleanup(detached.destroy)
        app, control, host, retained = self.fixture(
            Label, text="Cached caption", width=180, height=35,
        )
        key = paint_inputs(control, host, None, app.effective_theme)
        for restore in (copy.deepcopy, lambda source: pickle.loads(pickle.dumps(source))):
            # copyreg may cache discovered __slots__ on the class itself.
            # This does not affect an unchanged instance's drawing state.
            clone = restore(detached)
            self.addCleanup(clone.destroy)
            self.assertEqual(clone.text, detached.text)
            self.assertEqual(paint_inputs(control, host, None, app.effective_theme), key)
        paint_tree(app, host, retained=retained)
        self.assertNotIn(retained._records[control].identity, host.updates)

    def test_base_order_changes_repaint_after_explicit_invalidation(self):
        class Mixin:
            __slots__ = ()

            def _mixin_caption(self):
                return "From mixin"

        original_paint = Label.paint

        def paint(control, painter):
            original_paint(control, painter)
            caption = getattr(control, "_mixin_caption", None)
            if caption is not None:
                painter.text(caption(), 0, 0)

        # Keep Control first so changing bases preserves the object layout.
        del Mixin.__slots__
        with patch.object(Label, "paint", paint):
            app, control, host, retained = self.fixture(
                Label, text="Caption", width=180, height=35,
            )
            original = Label.__bases__
            try:
                Label.__bases__ = (Control, Mixin)
                self.assert_repaint_matches_direct(app, control, host, retained)
            finally:
                Label.__bases__ = original
            self.assert_repaint_matches_direct(app, control, host, retained)

    def test_existing_arranger_private_drawing_state_refreshes_at_fixed_bounds(self):
        original_arrange, original_paint = SplitPane.arrange_children, SplitPane.paint
        caption = ["Before"]

        def arrange_children(control, area, measure):
            result = original_arrange(control, area, measure)
            control._arranged_caption = caption[0]
            return result

        def paint(control, painter):
            original_paint(control, painter)
            painter.text(control._arranged_caption, 0, 0)

        with (patch.object(SplitPane, "arrange_children", arrange_children),
              patch.object(SplitPane, "paint", paint)):
            for mode in ("retained", "surface"):
                with self.subTest(mode=mode):
                    caption[0] = "Before"
                    app = App(width=400, height=200, reduce_motion=True, cache_paint=False)
                    self.addCleanup(app.destroy)
                    host = PatchRecordingHost() if mode == "retained" else RecordingRenderer()
                    if mode == "surface":
                        host.reset_scene("Arrangement revision", 400, 200)
                        self.addCleanup(host.close_scene)
                    runtime = Runtime(app, host)
                    app._runtime = runtime
                    self.addCleanup(setattr, app, "_runtime", None)
                    retained = RetainedPaintTree(host) if mode == "retained" else None
                    runtime._retained_paint = retained
                    cache = RenderCache(host) if mode == "surface" else None
                    self.addCleanup((retained if retained is not None else cache).clear)
                    pane = SplitPane(parent=app, width=240, height=120, cache_measure=False)
                    _ = pane.first, pane.second

                    def frame():
                        paint_tree(app, host, retained=retained, cache=cache)

                    def snapshot():
                        return (host.snapshot() if retained is not None
                                else svg_drawing(host.export_svg()))

                    arrange(app, host)
                    bounds = pane.bounds
                    for _ in range(3):
                        frame()
                    before = snapshot()
                    # An uncached layout hook runs on the requested pass and
                    # changes its own drawing state, without property writes.
                    caption[0] = "After"
                    arrange(app, host)
                    self.assertEqual(pane.bounds, bounds)
                    frame()
                    actual = snapshot()
                    self.assertNotEqual(actual, before)
                    if retained is not None:
                        self.assertIn(retained._records[pane].identity, host.updates)
                    paint_tree(app, host)
                    direct = (tuple(host._drawing) if retained is not None
                              else svg_drawing(host.export_svg()))
                    self.assertEqual(actual, direct)

    def test_direct_subtree_arrangement_refreshes_cached_ancestor_without_waking(self):
        class Summary(Container):
            def paint(self, painter):
                painter.text(self.children[0]._arranged_caption, 0, 0)

        original_arrange = SplitPane.arrange_children
        caption = ["Before"]

        def arrange_children(control, area, measure):
            result = original_arrange(control, area, measure)
            control._arranged_caption = caption[0]
            return result

        with patch.object(SplitPane, "arrange_children", arrange_children):
            app = App(width=400, height=200, reduce_motion=True, cache_paint=False)
            self.addCleanup(app.destroy)
            host = PatchRecordingHost()
            host.size = (400, 200)
            runtime = Runtime(app, host)
            app._runtime = runtime
            self.addCleanup(setattr, app, "_runtime", None)
            retained = runtime._retained_paint = RetainedPaintTree(host)
            parent = Summary(parent=app, width=400, height=200, cache_paint=True)
            nested = SplitPane(parent=parent, width=400, height=200, cache_measure=False)
            _ = nested.first, nested.second
            arrange(app, host)
            paint_tree(app, host, retained=retained)
            before = host.snapshot()
            bounds = parent.bounds, nested.bounds
            wakes = []
            app._wake = lambda: wakes.append(True)
            caption[0] = "After"
            arrange(nested, host)
            self.assertEqual(wakes, [])
            self.assertEqual((parent.bounds, nested.bounds), bounds)
            paint_tree(app, host, retained=retained)
            actual = host.snapshot()
            self.assertNotEqual(actual, before)
            self.assertIn(retained._records[parent].identity, host.updates)
            paint_tree(app, host)
            self.assertEqual(actual, tuple(host._drawing))
