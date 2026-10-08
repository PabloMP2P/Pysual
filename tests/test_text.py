"""Unicode conformance, document normalization and long-line interaction."""

from pathlib import Path
import unittest
from pysual import App, Button, Style, TextBox, light
from pysual.graphemes import boundaries, is_boundary, next_boundary, previous_boundary
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.runtime import Runtime
from test_library import RecordingHost


class TextTests(unittest.TestCase):
    def test_auto_indent_replacement_uses_selection_start_in_both_directions(self):
        for text, start, expected in (
            ("    alpha\nbeta", 4, "    \n    "),
            ("    if ready:\nbeta", 13, "    if ready:\n        "),
            ("\talpha\nbeta", 1, "\t\n\t"),
        ):
            for anchor, caret in ((start, len(text)), (len(text), start)):
                with self.subTest(text=text, caret=caret):
                    entry = TextBox(text=text, multiline=True, auto_indent=True)
                    self.addCleanup(entry.destroy)
                    entry.select(anchor, caret)
                    entry.handle_input(Input("key_down", key="Enter"))
                    self.assertEqual(entry.text, expected)
                    entry.undo()
                    self.assertEqual(entry.text, text)

    def test_part_padding_changes_measurement_and_text_hit_geometry(self):
        app, host = (
            App(
                theme=light()
                .styled(Button, Style(padding=24))
                .styled(TextBox, Style(padding=20))
            ),
            RecordingHost(),
        )
        self.addCleanup(app.destroy)
        button = Button(parent=app, text="A long button title")
        entry = TextBox(parent=app, text="abcdef", top=100, width=300, height=80)
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        arrange(app, host)
        width, height = button.measure(host)
        text_width, text_height = host.measure(button.text, 15)
        self.assertGreaterEqual(width, text_width + 48)
        self.assertGreaterEqual(height, text_height + 48)
        entry.handle_input(
            Input("pointer_down", x=entry.bounds.x + 23, y=entry.bounds.y + 22)
        )
        self.assertEqual(entry.selection_range, (0, 0))
        self.assertIs(runtime.app, app)

    def test_view_state_validation_is_atomic(self):
        entry = TextBox(text="abc\ndef", multiline=True)
        entry.restore_view_state((2, 1, 100, 20.5))
        old = entry.capture_view_state()
        self.assertEqual(old[:2], (2, 1))
        for state in ((0, 100, 0, 0.0), (0, 0, -1, 0.0), (0, 0, 0, float("inf"))):
            with self.assertRaises(ValueError):
                entry.restore_view_state(state)
            self.assertEqual(entry.capture_view_state(), old)

    def test_unicode_17_extended_grapheme_conformance(self):
        fixture = (
            Path(__file__).parent / "fixtures/unicode/GraphemeBreakTest-17.0.0.txt"
        )
        count = 0
        for raw in fixture.read_text(encoding="utf-8").splitlines():
            value = raw.split("#")[0].strip()
            if not value:
                continue
            text, expected = "", []
            for item in value.split():
                if item == "÷":
                    expected.append(len(text))
                elif item != "×":
                    text += chr(int(item, 16))
            with self.subTest(case=count):
                self.assertEqual(boundaries(text), expected)
                self.assertEqual(
                    [i for i in range(len(text) + 1) if is_boundary(text, i)], expected
                )
                self.assertEqual(
                    [next_boundary(text, i) for i in expected[:-1]], expected[1:]
                )
                self.assertEqual(
                    [previous_boundary(text, i) for i in expected[1:]], expected[:-1]
                )
            count += 1
        self.assertEqual(count, 766)

    def test_all_document_entry_points_normalize_newlines(self):
        text = TextBox(text="one\r\ntwo\rthree\nfour")
        self.assertEqual(text.text, "one two three four")
        text.text = "a\nb"
        self.assertEqual(text.text, "a b")
        text.load_text("a\r\nb")
        self.assertEqual(text.text, "a b")
        text.multiline = True
        text.text = "a\rb\r\nc"
        self.assertEqual(text.text, "a\nb\nc")
        text.select_all()
        text.replace_selection("d\re")
        self.assertEqual(text.text, "d\ne")
        text.multiline = False
        text.undo()
        self.assertEqual(text.text, "d e")
        with self.assertRaises(TypeError):
            text.text = None

    def test_cluster_deletion_and_zwj_nonemoji_boundary(self):
        for cluster in ("क्ष", "각", "👩🏽‍💻", "🇪🇸", "\u0600a", "e\u0301"):
            text = TextBox(text="x" + cluster)
            text.handle_input(Input("key_down", key="Backspace"))
            self.assertEqual(text.text, "x")
        text = TextBox(text="a\u200db")
        text.handle_input(Input("key_down", key="Backspace"))
        self.assertEqual(text.text, "a\u200d")

    def test_long_monospace_line_paints_only_visible_fragment(self):
        class MeasuredHost(RecordingHost):
            def measure(self, text, size, mono=False):
                self.largest_measure = max(
                    getattr(self, "largest_measure", 0), len(text)
                )
                return super().measure(text, size, mono)

        app, host = App(), MeasuredHost()
        text = app.text_box(text="a" * 1_000_000, monospace=True, width=500, height=80)
        self.addCleanup(app.destroy)
        runtime = Runtime(app, host)
        runtime.router.focus = text
        arrange(app, host)
        paint_tree(app, host)
        self.assertLess(host.largest_measure, 100)
        self.assertLess(sum(map(len, host.texts)), 100)
        text.handle_input(Input("key_down", key="ArrowLeft"))
        self.assertEqual(text.selection_range, (999999, 999999))


if __name__ == "__main__":
    unittest.main()
