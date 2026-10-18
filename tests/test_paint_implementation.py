"""Completed paint snapshots guard the whole implementation, not a hook list."""

import copy
import pickle
from unittest.mock import patch
from typing import get_type_hints

from _ui_testcase import UIOwnerTestCase
from test_render_cache import svg_drawing
from test_retained_paint import PatchRecordingHost
from test_web_svg import RecordingRenderer

from pysual import App, Control, Label, ListView, Slider, TextBox
from pysual._paint_state import paint_inputs, painting_scope
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

    def test_instance_helpers_and_restoration_use_the_same_generic_guard(self):
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
                self.assertEqual(host.updates, [])

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

        # Label.paint masks Control.paint in normal attribute resolution but
        # reaches it through super(). Both implementations belong to the guard.
        with painting_scope():
            self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, tuple)
            with patch.object(Control, "paint", base_paint):
                self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, int)
                self.assert_repaint_matches_direct(app, control, host, retained)
            self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, tuple)
            self.assert_repaint_matches_direct(app, control, host, retained)

    def test_shadowed_base_paint_replacement_refreshes_surface_drawing(self):
        app = App(width=320, height=192, reduce_motion=True, cache_paint=False)
        self.addCleanup(app.destroy)
        control = Label(parent=app, text="Caption", width=180, height=35)
        host = RecordingRenderer()
        host.reset_scene("Implementation guard", 320, 192)
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

    def test_ancestor_changes_invalidate_checks_within_the_same_paint_scope(self):
        app, control, host, retained = self.fixture(
            ListView, items=("one", "two", "three"), row_height=20, width=120, height=90,
        )
        with painting_scope():
            original = paint_inputs(control, host, None, app.effective_theme)
            self.assertIsInstance(original.state, tuple)
            with patch.object(Control, "effective_row_height", lambda self, fallback: 40):
                changed = paint_inputs(control, host, None, app.effective_theme)
                self.assertIsInstance(changed.state, int)
                self.assert_repaint_matches_direct(app, control, host, retained)
            restored = paint_inputs(control, host, None, app.effective_theme)
            self.assertIsInstance(restored.state, tuple)

    def test_added_class_constants_disable_reuse_until_removed(self):
        app, control, host, _ = self.fixture(Slider, value=25, width=180, height=35)
        with painting_scope():
            self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, tuple)
            with patch.object(Slider, "_drawing_parameter", 20, create=True):
                self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, int)
            self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, tuple)

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
                with painting_scope():
                    self.assertIsInstance(
                        paint_inputs(control, host, None, app.effective_theme).state, tuple,
                    )
                control.invalidate()
                paint_tree(app, host, retained=retained)
                self.assertEqual(host.updates, [])

    def test_copy_and_pickle_metadata_preserve_unchanged_body_reuse(self):
        detached = Label(text="Copy source")
        self.addCleanup(detached.destroy)
        app, control, host, retained = self.fixture(
            Label, text="Cached caption", width=180, height=35,
        )
        with painting_scope():
            self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, tuple)
            for restore in (copy.deepcopy, lambda source: pickle.loads(pickle.dumps(source))):
                # copyreg caches discovered __slots__ on the class itself.
                # This must not make all future instances look customized.
                clone = restore(detached)
                self.addCleanup(clone.destroy)
                self.assertIsInstance(
                    paint_inputs(control, host, None, app.effective_theme).state, tuple,
                )
        control.invalidate()
        paint_tree(app, host, retained=retained)
        self.assertEqual(host.updates, [])

    def test_base_order_changes_disable_reuse_until_restored(self):
        app, control, host, _ = self.fixture(Slider, value=25, width=180, height=35)

        class Mixin:
            __slots__ = ()

        # Keep the resolved namespace unchanged while extending super()'s path.
        # Removing this declaration does not change the class's memory layout.
        del Mixin.__slots__
        original = Slider.__bases__
        with painting_scope():
            self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, tuple)
            try:
                Slider.__bases__ = (Control, Mixin)
                self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, int)
            finally:
                Slider.__bases__ = original
            self.assertIsInstance(paint_inputs(control, host, None, app.effective_theme).state, tuple)
