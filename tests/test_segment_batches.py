"""Independent strokes preserve coordinates, order and fallback semantics."""
import unittest
from unittest.mock import Mock

from _ui_testcase import UIOwnerTestCase
from pysual import App, ChartSlice, Rect, Theme
from pysual.backends.native import NativeHost
from pysual.layout import arrange
from pysual.painting import Painter, RetainedPaintTree, paint_tree
from pysual.runtime import Runtime


class SegmentBatchTests(unittest.TestCase):
    def test_unchanged_scene_skips_ipc_but_explicit_present_completes(self):
        host = NativeHost()
        host._request = Mock(return_value={})
        host.begin_scene("#000000")
        host.end_scene()
        host._request.reset_mock()
        host.begin_scene("#000000")
        host.end_scene()
        host._request.assert_not_called()
        host._request.reset_mock()
        host.begin_scene("#000000")
        host.present()
        host._request.assert_called_once_with("present", scheduled=False)

    def test_expose_and_resume_present_an_unchanged_scene_once(self):
        for kind in ("repaint", "resume"):
            with self.subTest(kind=kind):
                host = NativeHost()
                host._request = Mock(return_value={})
                host.begin_scene("#000000")
                host.end_scene()
                host._request.reset_mock()
                host._on_event({"kind": kind})
                self.assertEqual(host.poll()[0].kind, kind)
                host.begin_scene("#000000")
                host.end_scene()
                host._request.assert_called_once_with("present", scheduled=True)
                host._request.reset_mock()
                host.begin_scene("#000000")
                host.end_scene()
                host._request.assert_not_called()

    def test_changed_scene_satisfies_pending_expose_without_extra_present(self):
        host = NativeHost()
        host._request = Mock(return_value={})
        host._on_event({"kind": "repaint"})
        host.poll()
        host.begin_scene("#123456")
        host.end_scene()
        self.assertEqual(host._request.call_args.args, ("patch",))
        host._request.reset_mock()
        host.begin_scene("#123456")
        host.end_scene()
        host._request.assert_not_called()

    def test_painter_offsets_batched_and_fallback_strokes_identically(self):
        segments = ((1, 2, 3, 4), (9, 8, 7, 6))
        expected = ((11, 22, 13, 24), (19, 28, 17, 26))
        class Host:
            line = Mock()
        fallback = Host()
        painter = Painter(fallback, Rect(10, 20, 100, 80), Theme())
        painter.segments(iter(segments), "#12345680", 2)
        self.assertEqual([call.args for call in fallback.line.call_args_list],
                         [(*stroke, "#12345680", 2) for stroke in expected])
        native = NativeHost()
        painter = Painter(native, Rect(10, 20, 100, 80), Theme())
        painter.segments(iter(segments), "#12345680", 2)
        self.assertEqual(native._commands, [["segments",
            [[11, 22], [13, 24], [19, 28], [17, 26]], "#12345680", 2]])
        painter.segments((), "#123456", 2)
        self.assertEqual(len(native._commands), 1)

    def test_noncallable_batch_hook_uses_line_fallback(self):
        class Host:
            def __init__(self):
                self.line = Mock()
                self.lines = object()

        host = Host()
        painter = Painter(host, Rect(0, 0, 10, 10), Theme())
        painter.lines(((0, 0), (1, 1)), "#fff", 1)
        self.assertEqual(
            [call.args for call in host.line.call_args_list],
            [(0, 0, 1, 1, "#fff", 1)],
        )


class RetainedSegmentBatchTests(UIOwnerTestCase):
    def test_donut_strokes_reach_native_retained_commands(self):
        class PaintedApp(App):
            def paint(self, painter):
                self._paint_calls = getattr(self, "_paint_calls", 0) + 1
                super().paint(painter)

        app, host = PaintedApp(width=640, height=400), NativeHost()
        host._request = Mock(return_value={})
        host.measure = lambda text, size, mono=False: (len(text) * size * .6, size * 1.2)
        chart = app.donut_chart(width=400, height=300, slices=(
            ChartSlice("n", "North", 32), ChartSlice("s", "South", 31),
            ChartSlice("w", "West", 37),
        ))
        runtime = Runtime(app, host)
        app._runtime = runtime
        retained = runtime._retained_paint = RetainedPaintTree(host)
        try:
            arrange(app, host)
            paint_tree(app, host, retained=retained, layout_changed=True)
            payload = host._request.call_args.kwargs
            identity = retained._records[chart].identity
            commands = next(segment["commands"] for segment in payload["upsert"]
                            if segment["id"] == identity)
            batches = [command for command in commands if command[0] == "segments"]
            self.assertEqual(len(batches), 3)
            self.assertEqual(sum(len(command[1]) // 2 for command in batches), 513)
            host._request.reset_mock()
            chart.center_text = "Updated"
            before = app._paint_calls
            paint_tree(app, host, retained=retained)
            self.assertEqual(app._paint_calls, before + 1)
            updated = host._request.call_args.kwargs["upsert"]
            # Ordinary ancestor painting still runs and reaches the same patch;
            # the native renderer handles equal segments within that update.
            self.assertEqual({segment["id"] for segment in updated},
                             {identity, retained._records[app].identity})
            updated_commands = next(segment["commands"] for segment in updated
                                    if segment["id"] == identity)
            self.assertEqual([command for command in updated_commands if command[0] == "segments"],
                             batches)
            self.assertTrue(any(command[0] == "text" and command[1] == "Updated"
                                for command in updated_commands))
        finally:
            app._runtime = None
            app.destroy()
