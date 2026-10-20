"""Text drawing prepares locally and observes current state at its point of use."""

from collections import Counter
from contextlib import contextmanager
from contextvars import copy_context
import gc
import sys
from weakref import ref

from _ui_testcase import UIOwnerTestCase
from test_render_cache import svg_drawing
from test_retained_paint import PatchRecordingHost
from test_web_svg import RecordingRenderer

from pysual import App, TextBox
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.render_cache import RenderCache
from pysual.runtime import Runtime


@contextmanager
def preparation_calls():
    # Observe without changing method dispatch or implementation provenance.
    code = TextBox._prepare_text.__code__
    calls = Counter()
    previous = sys.getprofile()

    def observe(frame, event, argument):
        if event == "call" and frame.f_code is code:
            calls[frame.f_locals["self"]] += 1

    sys.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(previous)


@contextmanager
def scrollbar_calls():
    code = TextBox._sync_scrollbars.__code__
    calls = Counter()
    previous = sys.getprofile()

    def observe(frame, event, argument):
        if event == "call" and frame.f_code is code:
            calls[frame.f_locals["self"]] += 1

    sys.setprofile(observe)
    try:
        yield calls
    finally:
        sys.setprofile(previous)


class PreparedPaintTests(UIOwnerTestCase):
    def fixture(self, mode="retained", kind=TextBox, host_type=None, **properties):
        app = App(width=320, height=192, reduce_motion=True, cache_paint=False)
        self.addCleanup(app.destroy)
        host = (host_type or (RecordingRenderer if mode == "surface" else PatchRecordingHost))()
        if mode == "surface":
            host.reset_scene("Prepared painting", 320, 192)
            self.addCleanup(host.close_scene)
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        retained = RetainedPaintTree(host) if mode == "retained" else None
        if retained is not None:
            runtime._retained_paint = retained
            self.addCleanup(retained.clear)
        cache = RenderCache(host) if mode == "surface" else None
        if cache is not None:
            self.addCleanup(cache.clear)
        editor = kind(parent=app, text="Before", width=180, height=48, **properties)
        arrange(app, host)

        def frame():
            paint_tree(app, host, runtime.router.focus, retained=retained, cache=cache)

        def snapshot():
            if mode == "surface":
                return svg_drawing(host.export_svg())
            return host.snapshot() if retained is not None else tuple(host._drawing)

        def assert_direct():
            actual = snapshot()
            paint_tree(app, host, runtime.router.focus)
            direct = (svg_drawing(host.export_svg()) if mode == "surface"
                      else tuple(host._drawing))
            self.assertEqual(actual, direct)

        return app, editor, host, runtime, frame, assert_direct

    def test_stable_editor_repaints_prepare_once_and_match_direct(self):
        for mode in ("retained", "surface", "direct"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(
                    mode, multiline=True,
                )
                runtime.router.set_focus(editor)
                for index, text in enumerate(("One\tline", "Long updated line beyond the viewport\nLast line")):
                    editor.text = text
                    editor._caret = editor._anchor = len(text)
                    editor._composition = "Preview"
                    editor._reveal_after_paint = True
                    frame()  # Finish reveal/normalization before a stable repaint.
                    editor.foreground = "#123456" if index else "#654321"
                    with preparation_calls() as calls:
                        frame()
                    self.assertEqual(calls[editor], 1)
                    self.assertFalse(editor._reveal_after_paint)
                    assert_direct()

    def test_completed_preparation_does_not_repeat_scrollbar_measurement(self):
        for mode in ("retained", "surface", "direct"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(
                    mode, multiline=True,
                )
                editor.text = "First line\nSecond line\nThird line\nFourth line"
                frame()
                editor.foreground = "#123456"
                with scrollbar_calls() as calls:
                    frame()
                self.assertEqual(calls[editor], 1)
                assert_direct()

    def test_single_line_drawing_uses_text_changed_by_body_callback(self):
        for mode in ("retained", "surface", "direct"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(mode)
                rectangle = host.rect
                changed = False

                def mutate(bounds, *args, **kwargs):
                    nonlocal changed
                    if not changed and bounds == editor.bounds:
                        changed = True
                        editor.text = "After callback"
                    return rectangle(bounds, *args, **kwargs)

                host.rect = mutate
                frame()
                self.assertTrue(changed)
                self.assertEqual(editor._scroll_axes, (False, False))
                assert_direct()

    def test_body_callback_scrollbar_mutation_rejects_preparation(self):
        for mode in ("retained", "surface"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(
                    mode, multiline=True,
                )
                editor.text = "First line\nSecond line\nThird line\nFourth line"
                rectangle = host.rect
                changed = False

                def mutate(bounds, *args, **kwargs):
                    nonlocal changed
                    if not changed and bounds == editor.bounds:
                        changed = True
                        editor._scroll_axes = (False, False)
                    return rectangle(bounds, *args, **kwargs)

                host.rect = mutate
                with scrollbar_calls() as calls:
                    frame()
                self.assertTrue(changed)
                self.assertEqual(calls[editor], 1)
                self.assertEqual(editor._scroll_axes, (False, True))
                assert_direct()

    def test_clip_callback_rechecks_preparation_after_scrollbar_reuse(self):
        for mode in ("retained", "surface"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(
                    mode, multiline=True,
                )
                rectangle, clip = host.rect, host.clip
                armed = changed = False

                def arm(bounds, *args, **kwargs):
                    nonlocal armed
                    if bounds == editor.bounds:
                        armed = True
                    return rectangle(bounds, *args, **kwargs)

                def mutate(rect):
                    nonlocal changed
                    if armed and not changed:
                        changed = True
                        editor.text = "After clip"
                    return clip(rect)

                host.rect, host.clip = arm, mutate
                with preparation_calls() as calls:
                    frame()
                self.assertTrue(changed)
                self.assertEqual(calls[editor], 1)
                assert_direct()


    def test_clean_editor_reuses_body_and_invalidation_prepares_again(self):
        for mode in ("retained", "surface"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(mode)
                for _ in range(3):
                    frame()
                with preparation_calls() as calls:
                    frame()
                self.assertEqual(calls[editor], 0)
                editor.invalidate()
                with preparation_calls() as calls:
                    frame()
                self.assertEqual(calls[editor], 1)
                assert_direct()

    def test_changed_environment_focus_and_private_inputs_repaint(self):
        for changed in ("metrics", "scale", "resources", "focus", "caret"):
            with self.subTest(changed=changed):
                app, editor, host, runtime, frame, assert_direct = self.fixture()
                frame()
                if changed == "metrics":
                    host.text_row_height = 24
                elif changed == "scale":
                    host.render_scale = 2
                elif changed == "resources":
                    host.resource_revision += 1
                elif changed == "focus":
                    runtime.router.set_focus(editor)
                elif changed == "caret":
                    editor._caret = editor._anchor = 0
                    editor.invalidate()
                with preparation_calls() as calls:
                    frame()
                self.assertEqual(calls[editor], 1)
                assert_direct()

    def test_snapshot_callbacks_are_observed_before_text_drawing(self):
        class CallbackHost(PatchRecordingHost):
            callback = None

            @property
            def resource_revision(self):
                callback, self.callback = self.callback, None
                if callback is not None:
                    callback()
                return 0

        for changed in ("text", "caret", "helper"):
            with self.subTest(changed=changed):
                app, editor, host, runtime, frame, assert_direct = self.fixture(host_type=CallbackHost)
                frame()

                def mutate():
                    if changed == "text":
                        editor.text = "Changed during snapshot"
                    elif changed == "caret":
                        editor._caret = editor._anchor = 0
                    else:
                        object.__setattr__(editor, "_text_left", lambda: 40)

                host.callback = mutate
                editor.invalidate()
                frame()
                self.assertIsNone(host.callback)
                assert_direct()

    def test_alternate_direct_host_restores_measurement_owner_before_reuse(self):
        for mode in ("retained", "surface"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(mode)
                for _ in range(3):
                    frame()
                other_host = PatchRecordingHost()
                paint_tree(app, other_host)
                self.assertIs(editor._measure_host, other_host)
                editor.invalidate()
                with preparation_calls() as calls:
                    frame()
                self.assertEqual(calls[editor], 1)
                self.assertIs(editor._measure_host, host)
                # Detached interactions use the last painting host for width
                # measurement, even if an alternate host had equal metrics.
                app._runtime = None
                measured = []
                measure = host.measure

                def record(text, size, mono=False):
                    measured.append(text)
                    return measure(text, size, mono)

                host.measure = record
                editor._width_cache_key = None
                editor._document_width()
                self.assertTrue(measured)
                app._runtime = runtime
                assert_direct()

    def test_measurement_mutation_cannot_hide_the_next_requested_frame(self):
        for mode in ("retained", "surface"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(mode)
                measure = host.measure
                changed = False

                def mutate(text, size, mono=False):
                    nonlocal changed
                    if not changed and text == "M":
                        changed = True
                        editor.text = "After measurement"
                    return measure(text, size, mono)

                host.measure = mutate
                with preparation_calls() as calls:
                    frame()
                self.assertTrue(changed)
                self.assertEqual(calls[editor], 1)
                # The callback happened during drawing. Its invalidation must
                # survive the frame that began before this text was committed.
                with preparation_calls() as calls:
                    frame()
                self.assertEqual(calls[editor], 1)
                assert_direct()

    def test_body_callback_mutation_rejects_prepared_text(self):
        for mode in ("retained", "surface"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(mode)
                rectangle = host.rect
                changed = False

                def mutate(bounds, *args, **kwargs):
                    nonlocal changed
                    if not changed and bounds == editor.bounds:
                        changed = True
                        editor.text = "After body callback"
                    return rectangle(bounds, *args, **kwargs)

                host.rect = mutate
                with preparation_calls() as calls:
                    frame()
                self.assertTrue(changed)
                self.assertEqual(calls[editor], 1)
                assert_direct()

    def test_actual_focus_change_rejects_preparation_when_focus_argument_is_unchanged(self):
        app, editor, host, runtime, frame, assert_direct = self.fixture()
        runtime.router.focus = editor
        editor._composition = "Composition preview"
        editor._reveal_after_paint = False
        rectangle = host.rect
        changed = False

        def mutate(bounds, *args, **kwargs):
            nonlocal changed
            if not changed and bounds == editor.bounds:
                changed = True
                # Keep public revisions and paint_tree's original argument
                # unchanged to exercise the actual focused input itself.
                runtime.router.focus = None
            return rectangle(bounds, *args, **kwargs)

        host.rect = mutate
        with preparation_calls() as calls:
            paint_tree(app, host, retained=runtime._retained_paint)
        self.assertTrue(changed)
        self.assertEqual(calls[editor], 1)
        assert_direct()

    def test_instance_hook_replaced_after_preparation_runs_at_draw_time(self):
        app, editor, host, runtime, frame, assert_direct = self.fixture()
        rectangle = host.rect
        original = editor._prepare_text
        replacements = []
        changed = False

        def prepare(painter, style, viewport):
            replacements.append(editor)
            return original(painter, style, viewport)

        def mutate(bounds, *args, **kwargs):
            nonlocal changed
            if not changed and bounds == editor.bounds:
                changed = True
                object.__setattr__(editor, "_prepare_text", prepare)
            return rectangle(bounds, *args, **kwargs)

        host.rect = mutate
        frame()
        self.assertEqual(replacements, [editor])
        assert_direct()

    def test_custom_editor_keeps_direct_preparation(self):
        class CustomEditor(TextBox):
            def _prepare_paint(self, host):
                raise AssertionError("Custom preparation must not be trusted")

        for mode in ("retained", "surface", "direct"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(mode, CustomEditor)
                with preparation_calls() as calls:
                    frame()
                self.assertEqual(calls[editor], 1)
                assert_direct()

    def test_reentrant_painting_uses_independent_preparations(self):
        app, editor, host, runtime, frame, assert_direct = self.fixture()
        other_app, other, other_host, other_runtime, nested, assert_other = self.fixture()
        measure = host.measure
        entered = False

        def reenter(text, size, mono=False):
            nonlocal entered
            if not entered:
                entered = True
                nested()
            return measure(text, size, mono)

        host.measure = reenter
        with preparation_calls() as calls:
            frame()
        self.assertTrue(entered)
        self.assertEqual((calls[editor], calls[other]), (1, 1))
        assert_direct()
        assert_other()

    def test_failed_drawing_discards_preparation_before_retry(self):
        app, editor, host, runtime, frame, assert_direct = self.fixture()
        rectangle = host.rect
        failed = False

        def fail(bounds, *args, **kwargs):
            nonlocal failed
            if not failed and bounds == editor.bounds:
                failed = True
                raise RuntimeError("body failed")
            return rectangle(bounds, *args, **kwargs)

        host.rect = fail
        with self.assertRaisesRegex(RuntimeError, "body failed"):
            frame()
        editor.text = "Retry uses current text"
        with preparation_calls() as calls:
            frame()
        self.assertEqual(calls[editor], 1)
        assert_direct()


    def test_private_mutation_in_measurement_repaints_on_the_next_frame(self):
        app, editor, host, runtime, frame, assert_direct = self.fixture()
        runtime.router.focus = editor
        editor._caret = editor._anchor = len(editor.text)
        editor._reveal_after_paint = False
        measure = host.measure
        changed = False

        def mutate(text, size, mono=False):
            nonlocal changed
            if not changed and text == "M":
                changed = True
                editor._caret = editor._anchor = 0
                editor.invalidate()
            return measure(text, size, mono)

        host.measure = mutate
        with preparation_calls() as calls:
            frame()
        self.assertTrue(changed)
        self.assertEqual(calls[editor], 1)
        with preparation_calls() as calls:
            frame()
        self.assertEqual(calls[editor], 1)
        assert_direct()

    def test_clip_callbacks_change_helpers_and_caret_before_local_preparation(self):
        for changed in ("helper", "caret"):
            with self.subTest(changed=changed):
                app, editor, host, runtime, frame, assert_direct = self.fixture()
                runtime.router.focus = editor
                editor._caret = editor._anchor = len(editor.text)
                editor._reveal_after_paint = False
                rectangle, clip = host.rect, host.clip
                armed = mutated = False

                def arm(bounds, *args, **kwargs):
                    nonlocal armed
                    if bounds == editor.bounds:
                        armed = True
                    return rectangle(bounds, *args, **kwargs)

                def mutate(bounds):
                    nonlocal mutated
                    if armed and not mutated:
                        mutated = True
                        if changed == "helper":
                            object.__setattr__(editor, "_text_left", lambda: 30)
                        else:
                            editor._caret = editor._anchor = 0
                    return clip(bounds)

                host.rect, host.clip = arm, mutate
                with preparation_calls() as calls:
                    frame()
                self.assertTrue(mutated)
                self.assertEqual(calls[editor], 1)
                assert_direct()

    def test_copied_context_does_not_retain_control_after_drawing(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                editor = TextBox(text="Detached source", width=180, height=48)
                host = PatchRecordingHost()
                arrange(editor, host)
                copied = None
                rectangle = host.rect

                def capture(*args, **kwargs):
                    nonlocal copied
                    copied = copy_context()
                    if failed:
                        raise RuntimeError("failed pass")
                    return rectangle(*args, **kwargs)

                host.rect = capture
                if failed:
                    with self.assertRaisesRegex(RuntimeError, "failed pass"):
                        paint_tree(editor, host)
                else:
                    paint_tree(editor, host)
                self.assertIsNotNone(copied)
                reference = ref(editor)
                editor.destroy()
                del editor
                gc.collect()
                self.assertIsNone(reference())
