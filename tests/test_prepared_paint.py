"""Preparation belongs to one paint operation and survives no input changes."""

from collections import Counter
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import replace
import gc
import sys
from unittest.mock import patch
from weakref import ref

from _ui_testcase import UIOwnerTestCase
from test_render_cache import svg_drawing
from test_retained_paint import PatchRecordingHost
from test_web_svg import RecordingRenderer

from pysual import App, DataGrid, ListView, TextBox, TreeView
from pysual import _paint_state
from pysual._paint_state import _current_checks, paint_inputs, painting_scope
from pysual.layout import arrange
from pysual.painting import Painter, RetainedPaintTree, paint_tree
from pysual.render_cache import RenderCache
from pysual.runtime import Runtime


@contextmanager
def preparation_calls():
    # Observing calls must not replace a method: replacement correctly opts a
    # builtin out of the trusted preparation contract being tested here.
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


@contextmanager
def input_calls(kind):
    codes = {
        kind._prepare_paint.__code__: "prepare",
        _paint_state._snapshot.__code__: "snapshot",
    }
    calls = Counter()
    previous = sys.getprofile()

    def observe(frame, event, argument):
        if event == "call" and frame.f_code in codes:
            calls[codes[frame.f_code]] += 1

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

    def test_single_line_paint_skips_scrollbar_preparation_audit(self):
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
                with patch("pysual.text.peek_prepared_paint",
                           side_effect=AssertionError("Single-line scrollbars need no audit")):
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
                self.assertEqual(calls[editor], 2)
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
                self.assertEqual(calls[editor], 2)
                assert_direct()

    def test_normalization_only_hooks_take_no_extra_preparation_snapshots(self):
        for kind in (ListView, DataGrid, TreeView):
            with self.subTest(control=kind.__name__):
                control = kind(width=180, height=100)
                self.addCleanup(control.destroy)
                host = PatchRecordingHost()
                arrange(control, host)
                snapshots = []
                previous = sys.getprofile()

                def observe(frame, event, argument):
                    if event == "call" and frame.f_code is _paint_state._snapshot.__code__:
                        snapshots.append(frame.f_locals["control"])

                with painting_scope():
                    sys.setprofile(observe)
                    try:
                        paint_inputs(control, host, None, control._fast_theme())
                    finally:
                        sys.setprofile(previous)
                    self.assertEqual(snapshots, [control])
                    self.assertFalse(_current_checks().prepared)

    def test_completed_unchanged_inputs_skip_preparation_with_one_snapshot(self):
        for kind in (TextBox, ListView, DataGrid, TreeView):
            with self.subTest(control=kind.__name__):
                control = kind(width=180, height=100)
                self.addCleanup(control.destroy)
                host = PatchRecordingHost()
                arrange(control, host)
                with painting_scope():
                    previous = paint_inputs(control, host, None, None)
                control.invalidate()
                with painting_scope(), input_calls(kind) as calls:
                    current = paint_inputs(control, host, None, None, previous=previous)
                    self.assertIs(current, previous)
                    self.assertFalse(_current_checks().prepared)
                self.assertEqual(calls, {"snapshot": 1})

    def test_changed_normalizers_prepare_without_an_extra_preflight_snapshot(self):
        for kind in (ListView, DataGrid, TreeView):
            for changed in ("model", "private"):
                with self.subTest(control=kind.__name__, changed=changed):
                    control = kind(width=180, height=100)
                    self.addCleanup(control.destroy)
                    host = PatchRecordingHost()
                    arrange(control, host)
                    with painting_scope():
                        previous = paint_inputs(control, host, None, None)
                    if changed == "model":
                        control.foreground = "#123456"
                    else:
                        control._scroll = 1
                    with painting_scope(), input_calls(kind) as calls:
                        paint_inputs(control, host, None, None, previous=previous)
                    self.assertEqual(calls, {"prepare": 1, "snapshot": 1})

    def test_completed_editor_reuse_does_not_prepare_undrawn_body(self):
        for mode in ("retained", "surface"):
            with self.subTest(mode=mode):
                app, editor, host, runtime, frame, assert_direct = self.fixture(mode)
                for _ in range(3):
                    frame()
                editor.invalidate()
                with preparation_calls() as calls:
                    frame()
                self.assertEqual(calls[editor], 0)
                assert_direct()

    def test_changed_environment_focus_and_private_inputs_require_preparation(self):
        for changed in ("metrics", "scale", "resources", "focus", "caret", "reuse"):
            with self.subTest(changed=changed):
                app, editor, host, runtime, frame, assert_direct = self.fixture()
                with painting_scope():
                    previous = paint_inputs(editor, host, None, None)
                if changed == "metrics":
                    host.text_row_height = 24
                elif changed == "scale":
                    host.render_scale = 2
                elif changed == "resources":
                    host.resource_revision += 1
                elif changed == "focus":
                    runtime.router.focus = editor
                elif changed == "caret":
                    editor._caret = editor._anchor = 0
                with painting_scope(), input_calls(TextBox) as calls:
                    current = paint_inputs(editor, host, None, None,
                                           previous=previous, reuse=changed != "reuse")
                self.assertEqual(calls["prepare"], 1)
                self.assertIsNot(current, previous)

    def test_preflight_rechecks_snapshot_callback_mutations_before_reuse(self):
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
                with painting_scope():
                    previous = paint_inputs(editor, host, None, None)

                def mutate():
                    if changed == "text":
                        editor.text = "Changed during preflight"
                    elif changed == "caret":
                        editor._caret = editor._anchor = 0
                    else:
                        def obsolete_inputs():
                            raise AssertionError("A replaced input projection must not be called")
                        object.__setattr__(editor, "_paint_inputs", obsolete_inputs)

                host.callback = mutate
                with painting_scope(), input_calls(TextBox) as calls:
                    current = paint_inputs(editor, host, None, None, previous=previous)
                self.assertIsNone(host.callback)
                self.assertIsNot(current, previous)
                if changed == "helper":
                    self.assertEqual(calls["prepare"], 0)
                    self.assertIsInstance(current.state, int)
                else:
                    self.assertEqual(calls["prepare"], 1)
                    self.assertEqual(current.state, (editor._visual_epoch, editor._paint_inputs()))

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
                with input_calls(TextBox) as calls:
                    frame()
                self.assertEqual(calls["prepare"], 1)
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

    def test_measurement_mutation_rejects_result_computed_from_earlier_text(self):
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
                self.assertEqual(calls[editor], 2)
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
                self.assertEqual(calls[editor], 2)
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
        self.assertEqual(calls[editor], 2)
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

    def test_actual_style_and_viewport_must_match_prepared_view(self):
        app, editor, host, runtime, frame, assert_direct = self.fixture()
        for changed in ("style", "viewport"):
            with self.subTest(changed=changed), painting_scope(), preparation_calls() as calls:
                paint_inputs(editor, host, None, app.effective_theme)
                painter = Painter(host, editor.bounds, app.effective_theme, editor)
                style = painter.style()
                viewport = editor._text_viewport()
                if changed == "style":
                    style = replace(style, font_size=style.font_size + 2)
                else:
                    viewport = viewport.inset(2)
                editor._paint_text(painter, style, viewport)
                self.assertEqual(calls[editor], 2)

    def test_private_input_mutation_during_preparation_is_not_mistaken_for_normalization(self):
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
            return measure(text, size, mono)

        host.measure = mutate
        with preparation_calls() as calls:
            frame()
        self.assertTrue(changed)
        self.assertEqual(calls[editor], 2)
        assert_direct()

    def test_snapshot_callback_rechecks_hooks_and_private_inputs_before_consuming(self):
        class CallbackHost(PatchRecordingHost):
            callback = None

            @property
            def resource_revision(self):
                callback, self.callback = self.callback, None
                if callback is not None:
                    callback()
                return 0

        for changed in ("helper", "caret"):
            with self.subTest(changed=changed):
                app, editor, host, runtime, frame, assert_direct = self.fixture(host_type=CallbackHost)
                runtime.router.focus = editor
                editor._caret = editor._anchor = len(editor.text)
                editor._reveal_after_paint = False
                rectangle = host.rect
                armed = False

                def mutate():
                    if changed == "helper":
                        object.__setattr__(editor, "_text_left", lambda: 30)
                    else:
                        editor._caret = editor._anchor = 0

                def arm(bounds, *args, **kwargs):
                    nonlocal armed
                    if not armed and bounds == editor.bounds:
                        armed = True
                        host.callback = mutate
                    return rectangle(bounds, *args, **kwargs)

                host.rect = arm
                with preparation_calls() as calls:
                    frame()
                self.assertTrue(armed)
                self.assertIsNone(host.callback)
                self.assertEqual(calls[editor], 2)
                assert_direct()

    def test_copied_context_cannot_retain_or_reopen_completed_preparation(self):
        for failed in (False, True):
            with self.subTest(failed=failed):
                editor = TextBox(text="Detached source", width=180, height=48)
                host = PatchRecordingHost()
                arrange(editor, host)
                copied = None
                try:
                    with painting_scope():
                        paint_inputs(editor, host, None, editor._fast_theme())
                        self.assertIn(editor, _current_checks().prepared)
                        copied = copy_context()
                        if failed:
                            raise RuntimeError("failed pass")
                except RuntimeError:
                    self.assertTrue(failed)
                self.assertIsNone(copied.run(_current_checks))
                copied.run(paint_inputs, editor, host, None, editor._fast_theme())
                self.assertIsNone(copied.run(_current_checks))
                reference = ref(editor)
                editor.destroy()
                del editor
                gc.collect()
                self.assertIsNone(reference())
