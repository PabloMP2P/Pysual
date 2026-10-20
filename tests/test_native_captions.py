"""Deferred native captions preserve ordinary drawing and batch actual demand."""

from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
from types import MethodType
from unittest.mock import Mock, patch

from _ui_testcase import UIOwnerTestCase

from pysual import Button, Container, Control, Label, Rect, dark
from pysual._caption import Caption
from pysual.backends.native import NativeHost, NativeTermTextHost
from pysual.layout import arrange
from pysual.painting import Painter, RetainedPaintTree, paint_tree


def request_metrics(operation, **fields):
    if operation == "measure_many":
        return [[len(text) * 8, 16 * (text.count("\n") + 1)] for text in fields["texts"]]
    if operation == "measure":
        text = fields["text"]
        return [len(text) * 8, 16 * (text.count("\n") + 1)]
    return None


def comparable(value):
    # Moving an icon's local origin changes floating-point addition order,
    # while preserving geometry many orders below a device pixel.
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, list):
        return [comparable(item) for item in value]
    if isinstance(value, dict):
        return {key: comparable(item) for key, item in value.items()}
    return value


def original_label(control, painter):
    Control.paint(control, painter)
    style = painter.style()
    text, height = control.text, style.font_size * 1.2
    if "\n" in text:
        height = painter.measure(text, size=style.font_size, font_family=style.font_family)[1]
    else:
        text = painter.elide(text, painter.width, size=style.font_size, font_family=style.font_family)
    painter.text(text, 0, max(0, (painter.height - height) / 2),
                 color=style.foreground, size=style.font_size)


def original_button(control, painter):
    style = painter.body()
    content = painter.content_rect()
    icon_size = min(20, content.width, painter.height) if control.icon else 0
    icon_width = icon_size + (8 if control.text else 0) if control.icon else 0
    text = painter.elide(control.text, max(0, content.width - icon_width), size=style.font_size)
    width, height = painter.measure(text, size=style.font_size)
    if not text:
        icon_width = icon_size
    left = max(content.x, (painter.width - width - icon_width) / 2)
    if control.icon and icon_size > 0:
        painter.icon(control.icon, left, max(0, (painter.height - icon_size) / 2),
                     size=icon_size, color=style.foreground)
    painter.text(text, left + icon_width, max(0, (painter.height - height) / 2),
                 color=style.foreground, size=style.font_size)


class NativeCaptionTests(UIOwnerTestCase):
    def host(self, kind=NativeHost):
        host = kind()
        host._request = Mock(side_effect=request_metrics)
        self.addCleanup(host.close)
        return host

    def test_label_and_button_keep_their_original_command_output(self):
        for host_kind in (NativeHost, NativeTermTextHost):
            for kind, original in ((Label, original_label), (Button, original_button)):
                for text in ("", "Fits", "Long caption needing elision", "one\ntwo", "e\u0301🙂\0xyz"):
                    for width, height in ((160, 40), (35, 40), (0, 5), (15, 8)):
                        for family in ("ui", "mono"):
                            with self.subTest(host=host_kind, kind=kind, text=text, width=width, family=family):
                                control = kind(text=text, font_family=family,
                                               **({"icon": "plus"} if kind is Button else {}))
                                try:
                                    bounds = Rect(11, 23, width, height)
                                    control._rect = control._clip = control._paint_clip = bounds
                                    commands = []
                                    for direct in (True, False):
                                        host = self.host(host_kind)
                                        host.begin("#000000")
                                        painter = Painter(host, bounds, dark(), control)
                                        (original(control, painter) if direct else control.paint(painter))
                                        host.rect(Rect(1, 2, 3, 4), "#123456")
                                        host.present()
                                        commands.append(deepcopy(host._commands))
                                    self.assertEqual(comparable(commands[0]), comparable(commands[1]))
                                finally:
                                    control.destroy()

    def test_fifty_changed_labels_and_buttons_share_one_measurement_request(self):
        for kind in (Label, Button):
            host = self.host()
            root = Container(layout="grid", columns=5, width=1000, height=600, padding=0, spacing=0)
            controls = [kind(parent=root, text=f"Caption {index}", width=190, height=50)
                        for index in range(50)]
            retained = RetainedPaintTree(host)
            try:
                for frame in range(2):
                    if frame:
                        for index, control in enumerate(controls):
                            control.text = f"Changed {index}"
                        retained.invalidate_all()
                    arrange(root, host)
                    host._request.reset_mock()
                    paint_tree(root, host, retained=retained)
                    counts = Counter(call.args[0] for call in host._request.call_args_list)
                    self.assertEqual(counts, {"measure_many": 1, "patch": 1})
                    texts = host._request.call_args_list[0].kwargs["texts"]
                    self.assertEqual(Counter(texts), Counter(control.text for control in controls))
            finally:
                retained.clear()
                root.destroy()

    def test_measurement_failure_preserves_pending_commands_for_retry(self):
        host = self.host()
        painter = Painter(host, Rect(0, 0, 100, 40), dark())
        host.begin("#000000")
        painter.caption("First")
        host.rect(Rect(1, 2, 3, 4), "#123456")
        painter.caption("Second")
        before = list(host._commands)
        host._request.side_effect = RuntimeError("temporarily unavailable")
        with self.assertRaisesRegex(RuntimeError, "temporarily unavailable"):
            host.present()
        self.assertEqual(host._commands, before)
        self.assertTrue(any(isinstance(command, Caption) for command in before))
        host._request.side_effect = request_metrics
        host.present()
        self.assertEqual([command[0] for command in host._commands], ["begin", "text", "rect", "text"])

    def test_restarting_or_closing_a_frame_discards_unsubmitted_captions(self):
        host = self.host()
        painter = Painter(host, Rect(0, 0, 100, 40), dark())
        host.begin("#000000")
        painter.caption("Discarded")
        host.begin("#123456")
        painter.caption("Kept")
        host.present()
        batch = host._request.call_args_list[0].kwargs["texts"]
        self.assertEqual(batch, ["Kept"])
        host.begin("#000000")
        painter.caption("Closed")
        host.close()
        self.assertEqual(host._commands, [])

    def test_custom_painter_measurements_and_host_subclasses_remain_eager(self):
        original = Painter.measure

        def doubled(self, text, **kwargs):
            width, height = original(self, text, **kwargs)
            return width * 2, height

        class CustomHost(NativeHost):
            pass

        class CustomPainter(Painter):
            measure = doubled

        for mode in ("subclass", "instance", "class", "host"):
            host = self.host(CustomHost if mode == "host" else NativeHost)
            painter = (CustomPainter if mode == "subclass" else Painter)(host, Rect(0, 0, 40, 40), dark())
            if mode == "instance":
                painter.measure = MethodType(doubled, painter)
            with patch.object(Painter, "measure", doubled) if mode == "class" else nullcontext():
                painter.caption("abcdefgh", line_height=20)
            self.assertFalse(any(isinstance(command, Caption) for command in host._commands))
            self.assertTrue(host._request.call_count)

    def test_scoped_icon_stroke_overrides_are_applied_before_presentation(self):
        for method in ("lines", "line"):
            for scope in ("instance", "class"):
                with self.subTest(method=method, scope=scope):
                    host = self.host()
                    control = Button(text="Go", icon="plus")
                    self.addCleanup(control.destroy)
                    bounds = Rect(11, 23, 100, 40)
                    control._rect = control._clip = control._paint_clip = bounds
                    calls = []
                    original_lines, original_line = NativeHost.lines, NativeHost.line

                    def red_lines(owner, points, color, width=1):
                        calls.append("lines")
                        original_lines(owner, points, "#ff0000", width)

                    def red_line(owner, x1, y1, x2, y2, color, width=1):
                        calls.append("line")
                        original_line(owner, x1, y1, x2, y2, "#ff0000", width)

                    owner = host if scope == "instance" else NativeHost
                    replacement = red_lines if method == "lines" else red_line
                    if scope == "instance":
                        replacement = MethodType(replacement, host)
                    fallback = (patch.object(owner, "lines", None)
                                if method == "line" else nullcontext())
                    host.begin("#000000")
                    with patch.object(owner, method, replacement), fallback:
                        control.paint(Painter(host, bounds, dark(), control))
                    # The drawing customization has ended before submission.
                    self.assertEqual(calls, [method, method])
                    host.present()
                    self.assertEqual(calls, [method, method])
                    strokes = [command for command in host._commands
                               if command[0] in ("line", "lines")]
                    self.assertEqual([command[0] for command in strokes], [method, method])
                    self.assertEqual([command[-2] for command in strokes],
                                     ["#ff0000", "#ff0000"])

    def test_changed_metric_environment_discards_inflight_batch_results(self):
        host = self.host()
        unit = 8

        def changed(operation, **fields):
            nonlocal unit
            if operation == "measure_many" and unit == 8:
                old = request_metrics(operation, **fields)
                host.metrics_revision += 1
                unit = 16
                return old
            if operation == "measure_many":
                return [[len(text) * unit, 16] for text in fields["texts"]]
            if operation == "measure":
                return [len(fields["text"]) * unit, 16]
            return None

        host._request.side_effect = changed
        host.begin("#000000")
        Painter(host, Rect(0, 0, 30, 40), dark()).caption("abc", line_height=20)
        host.present()
        self.assertEqual(host._commands[-1][1], "…")
        self.assertEqual(host.measure("abc", 15), (48, 16))

    def test_environment_change_during_elision_restarts_all_caption_commands(self):
        host = self.host()
        unit = 8

        def changed(operation, **fields):
            nonlocal unit
            if operation == "measure_many":
                texts = fields["texts"]
                old = [[len(text) * unit, 16] for text in texts]
                if unit == 8 and "…" in texts:
                    unit = 16
                    host.metrics_revision += 1
                return old
            if operation == "measure":
                return [len(fields["text"]) * unit, 16]
            return None

        host._request.side_effect = changed
        host.begin("#000000")
        painter = Painter(host, Rect(0, 0, 30, 40), dark())
        painter.caption("abcdefgh", line_height=20)
        painter.caption("abc", line_height=20)
        host.present()
        self.assertEqual([command[1] for command in host._commands if command[0] == "text"],
                         ["…", "…"])

    def test_services_replaced_after_recording_are_used_eagerly_at_submission(self):
        host = self.host()
        host.begin("#000000")
        Painter(host, Rect(0, 0, 30, 40), dark()).caption("abc", line_height=20)
        host.measure = lambda text, size, mono=False: (len(text) * 16, 16)
        host.measure_many = lambda texts, size, mono=False: [(len(text) * 16, 16) for text in texts]
        host.present()
        self.assertEqual(host._commands[-1][1], "…")
        self.assertEqual([call.args[0] for call in host._request.call_args_list], ["frame"])

    def test_services_replaced_during_measurement_do_not_publish_old_results(self):
        host = self.host()

        def changed(operation, **fields):
            if operation == "measure_many":
                host.measure = lambda text, size, mono=False: (len(text) * 16, 16)
                host.measure_many = lambda texts, size, mono=False: [(len(text) * 16, 16) for text in texts]
            return request_metrics(operation, **fields)

        host._request.side_effect = changed
        host.begin("#000000")
        Painter(host, Rect(0, 0, 30, 40), dark()).caption("abc", line_height=20)
        host.present()
        self.assertEqual(host._commands[-1][1], "…")

    def test_image_only_reload_preserves_raw_metrics(self):
        host = self.host()
        info = {"width": 640, "height": 400, "scale": 1, "resource_revision": 5}
        host._accept_viewport(info)
        self.assertEqual(host.measure("Caption", 15), (56, 16))
        host._request.reset_mock()
        host._accept_viewport({**info, "resource_revision": 6}, image_only=True)
        self.assertEqual(host.measure("Caption", 15), (56, 16))
        host._request.assert_not_called()
        host._accept_viewport({**info, "resource_revision": 7, "scale": 2}, image_only=True)
        host.measure("Caption", 15)
        host._request.assert_called_once()
