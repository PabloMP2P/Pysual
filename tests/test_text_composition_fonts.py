"""Composition previews and resolved-font text viewport regressions."""

from _ui_testcase import UIOwnerTestCase
from pysual import App, Style, TextBox, get_theme, light
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.runtime import Runtime
from test_library import RecordingHost


class TextHost(RecordingHost):
    def __init__(self):
        super().__init__()
        self.draws = []
        self.carets = []
        self.largest_measure = 0

    def begin(self, color):
        super().begin(color)
        self.draws.clear()
        self.carets.clear()

    def measure(self, text, size, mono=False):
        self.largest_measure = max(self.largest_measure, len(text))
        return len(text) * (11 if mono else 7), size * 1.2

    def text(self, text, x, y, color, size, mono=False):
        super().text(text)
        self.draws.append((text, x, y, mono))

    def caret(self, x, y, height, color):
        self.carets.append((x, y))
        return True


class WholeRows(TextBox):
    def _initialize(self):
        super()._initialize()
        self._painted_rows = []

    def paint_line(self, painter, text, x, y, row, style):
        self._painted_rows.append((row, text))
        super().paint_line(painter, text, x, y, row, style)


class FragmentRows(WholeRows):
    def paint_line_fragment(self, painter, text, x, y, row, style, start_column):
        self._painted_rows.append((row, start_column, text))
        TextBox.paint_line(self, painter, text, x, y, row, style)


class TextCompositionFontTests(UIOwnerTestCase):
    def entry(self, *, cls=TextBox, theme=None, **kwargs):
        app, host = App(theme=theme or light(), reduce_motion=True), TextHost()
        self.addCleanup(app.destroy)
        entry = cls(parent=app, width=500, height=140, **kwargs)
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        runtime.router.focus = entry
        arrange(app, host)
        return app, entry, host

    def test_read_only_ignores_new_preedit_without_changing_document(self):
        app, entry, host = self.entry(text="AB", read_only=True)
        entry.select(1, 1)
        entry.handle_input(Input("composition", text="xy"))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["AB"])
        self.assertEqual(entry.text, "AB")
        self.assertEqual(entry.selection_range, (1, 1))
        self.assertEqual(entry._composition, "")

    def test_read_only_transition_without_preedit_keeps_pending_caret_reveal(self):
        app, entry, host = self.entry(text="x" * 1000, font_family="mono")
        entry.select(len(entry.text), len(entry.text))
        entry.read_only = True
        paint_tree(app, host)
        self.assertGreater(entry.capture_view_state()[3], 0)
        self.assertGreaterEqual(host.carets[0][0], entry.bounds.x + entry._text_left())
        self.assertLess(host.carets[0][0], entry.bounds.right)

    def test_document_load_and_read_only_transition_cancel_preedit_and_scroll(self):
        for action in ("read_only", "load_new", "load_same"):
            with self.subTest(action=action):
                app, entry, host = self.entry(text="AB", multiline=True, font_family="mono")
                entry.select(1, 1)
                entry.handle_input(Input("composition", text="\n".join(["x" * 1000] * 30)))
                paint_tree(app, host)
                self.assertGreater(entry.capture_view_state()[2], 0)
                self.assertGreater(entry.capture_view_state()[3], 0)
                if action == "read_only":
                    entry.read_only = True
                else:
                    entry.load_text("NEW" if action == "load_new" else "AB")
                self.assertEqual(entry._composition, "")
                self.assertFalse(entry._reveal_after_paint)
                paint_tree(app, host)
                self.assertEqual(host.texts, [entry.text])
                self.assertEqual(entry.text, "NEW" if action == "load_new" else "AB")
                self.assertEqual(entry.capture_view_state()[2:], (0, 0.0))

    def test_preedit_inserts_before_suffix_without_editing_document_or_history(self):
        app, entry, host = self.entry(text="AB")
        entry.replace_selection("C")
        entry.undo()
        self.assertTrue(entry.can_redo)
        entry.select(1, 1)
        before = (entry.text, entry.selection_range, entry._undo[:], entry._redo[:])
        entry.handle_input(Input("composition", text="secret"))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["AsecretB"])
        self.assertEqual(host.carets[0][0], entry.bounds.x + entry._text_left() + 7 * 7)
        self.assertEqual(
            (entry.text, entry.selection_range, entry._undo, entry._redo), before
        )
        entry.handle_input(Input("composition", text=""))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["AB"])
        self.assertEqual(entry.selection_range, (1, 1))
        self.assertFalse(entry.can_undo)
        self.assertTrue(entry.can_redo)

    def test_single_line_preedit_and_commit_normalize_line_breaks_identically(self):
        app, entry, host = self.entry(text="AB")
        entry.select(1, 1)
        entry.handle_input(Input("composition", text="x\r\ny"))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["Ax yB"])
        self.assertEqual(entry.text, "AB")
        entry.handle_input(Input("text", text="x\r\ny"))
        self.assertEqual(entry.text, "Ax yB")

    def test_preedit_replaces_either_selection_direction_and_commits_once(self):
        for selection in ((1, 4), (4, 1)):
            with self.subTest(selection=selection):
                app, entry, host = self.entry(text="ABCDE")
                entry.select(*selection)
                entry.handle_input(Input("composition", text="xy"))
                paint_tree(app, host)
                self.assertEqual(host.texts, ["AxyE"])
                self.assertEqual(entry.text, "ABCDE")
                self.assertEqual(entry.selection_range, (1, 4))
                self.assertFalse(entry.can_undo)
                entry.handle_input(Input("text", text="xy"))
                self.assertEqual(entry.text, "AxyE")
                self.assertEqual(entry.selection_range, (3, 3))
                self.assertEqual(entry._composition, "")
                entry.undo()
                self.assertEqual(entry.text, "ABCDE")
                self.assertEqual(entry.selection_range, (1, 4))
                self.assertFalse(entry.can_undo)

    def test_password_preedit_uses_grapheme_masks(self):
        app, entry, host = self.entry(text="AB", password=True)
        entry.select(1, 1)
        preedit = "\U0001f469\u200d\U0001f4bb"
        entry.handle_input(Input("composition", text=preedit))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["•" * 3])
        self.assertEqual(host.carets[0][0], entry.bounds.x + entry._text_left() + 14)
        self.assertEqual(entry.text, "AB")
        self.assertFalse(entry.can_undo)
        entry.handle_input(Input("blur"))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["••"])
        self.assertEqual(entry._composition, "")

    def test_password_preedit_mark_can_join_the_preceding_grapheme(self):
        app, entry, host = self.entry(text="eB", password=True)
        entry.select(1, 1)
        entry.handle_input(Input("composition", text="\u0301"))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["••"])
        self.assertEqual(host.carets[0][0], entry.bounds.x + entry._text_left() + 7)
        entry.handle_input(Input("composition", text=""))
        self.assertEqual(entry.text, "eB")
        self.assertFalse(entry.can_undo)

    def test_multiline_preedit_replaces_selected_rows_without_stale_decoration(self):
        app, entry, host = self.entry(
            cls=WholeRows, text="AB\nCD\nEF\nGH", multiline=True
        )
        entry.select(1, 7)
        entry.handle_input(Input("composition", text="x\ny"))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["Ax", "yF", "GH"])
        self.assertEqual(entry._painted_rows, [(3, "GH")])
        self.assertEqual(entry.text, "AB\nCD\nEF\nGH")
        self.assertEqual(entry.line_count, 4)
        self.assertEqual(entry.selection_range, (1, 7))
        self.assertFalse(entry.can_undo)
        entry.handle_input(Input("composition", text=""))
        entry._painted_rows.clear()
        paint_tree(app, host)
        self.assertEqual(entry._painted_rows, list(enumerate(("AB", "CD", "EF", "GH"))))
        entry.handle_input(Input("composition", text="x\ny"))
        entry.handle_input(Input("text", text="x\ny"))
        self.assertEqual(entry.text, "Ax\nyF\nGH")
        entry.undo()
        self.assertEqual(entry.text, "AB\nCD\nEF\nGH")

    def test_composition_caret_remains_visible_then_cancellation_restores_bounds(self):
        app, entry, host = self.entry(text="AB", font_family="mono")
        entry.select(1, 1)
        entry.handle_input(Input("composition", text="x" * 1000))
        paint_tree(app, host)
        self.assertLess(sum(map(len, host.texts)), 100)
        self.assertGreaterEqual(host.carets[0][0], entry.bounds.x + entry._text_left())
        self.assertLess(host.carets[0][0], entry.bounds.right)
        caret, offset = host.carets[0], entry.capture_view_state()[3]
        entry.invalidate()
        paint_tree(app, host)
        self.assertEqual(host.carets[0], caret)
        self.assertEqual(entry.capture_view_state()[3], offset)
        entry.handle_input(Input("composition", text=""))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["AB"])
        self.assertEqual(entry.capture_view_state()[3], 0)

    def test_multiline_preedit_offsets_survive_repaint_and_cancel(self):
        app, entry, host = self.entry(text="AB", multiline=True, font_family="mono")
        entry.select(1, 1)
        entry.handle_input(Input("composition", text="\n".join(["x"] * 30)))
        paint_tree(app, host)
        caret, scroll = host.carets[0], entry.capture_view_state()[2]
        self.assertGreater(scroll, 0)
        self.assertGreaterEqual(caret[1], entry.bounds.y)
        self.assertLess(caret[1], entry.bounds.bottom)
        entry.invalidate()
        paint_tree(app, host)
        self.assertEqual(host.carets[0], caret)
        self.assertEqual(entry.capture_view_state()[2], scroll)
        entry.handle_input(Input("composition", text=""))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["AB"])
        self.assertEqual(entry.capture_view_state()[2], 0)

    def test_composition_reveals_changes_but_preserves_manual_scroll_on_both_axes(self):
        original = "\n".join(["a" * 100] * 30)
        app, entry, host = self.entry(text=original, multiline=True, font_family="mono")
        entry.select(1, 1)
        entry.handle_input(Input("composition", text="preedit"))
        paint_tree(app, host)
        entry.handle_input(Input("wheel", delta=2))
        entry.handle_input(Input("wheel", delta=10, shift=True))
        scroll = entry.capture_view_state()[2:]
        self.assertGreater(scroll[0], 0)
        self.assertGreater(scroll[1], 0)
        paint_tree(app, host)
        self.assertEqual(entry.capture_view_state()[2:], scroll)
        entry.invalidate()
        paint_tree(app, host)
        self.assertEqual(entry.capture_view_state()[2:], scroll)
        self.assertEqual(entry._composition, "preedit")
        entry.handle_input(Input("composition", text="updated"))
        paint_tree(app, host)
        self.assertEqual(entry.capture_view_state()[2], 0)
        self.assertLess(entry.capture_view_state()[3], scroll[1])
        self.assertGreaterEqual(host.carets[0][0], entry.bounds.x + entry._text_left())
        self.assertLess(host.carets[0][0], entry.bounds.right)
        self.assertEqual(entry.text, original)
        self.assertFalse(entry.can_undo)

    def test_all_effective_mono_sources_bound_extent_hit_caret_and_paint_work(self):
        configurations = (
            ({"monospace": True}, None),
            ({"font_family": "mono"}, None),
            ({}, get_theme("terminal")),
            ({}, light().styled(TextBox, Style(font_family="mono"))),
        )
        for properties, theme in configurations:
            with self.subTest(properties=properties, theme=theme):
                app, entry, host = self.entry(
                    text="a" * 100_000, multiline=True, theme=theme, **properties
                )
                paint_tree(app, host)
                self.assertLess(host.largest_measure, 100)
                self.assertLess(sum(map(len, host.texts)), 100)
                self.assertTrue(all(mono for _, _, _, mono in host.draws))
                self.assertEqual(entry._document_width(host), 1_100_000)
                self.assertGreaterEqual(host.carets[0][0], entry.bounds.x + entry._text_left())
                self.assertLess(host.carets[0][0], entry.bounds.right)
                entry.restore_view_state((0, 0, 0, 0.0))
                entry.handle_input(Input(
                    "pointer_down", x=entry.bounds.x + entry._text_left() + 27,
                    y=entry.bounds.y + 10,
                ))
                self.assertEqual(entry.selection_range, (2, 2))
                self.assertLess(host.largest_measure, 100)

    def test_font_switch_recomputes_extent_caret_and_fast_path(self):
        for inherited in (False, True):
            with self.subTest(inherited=inherited):
                properties = (
                    {"theme": light().derive(font_family="mono")}
                    if inherited else {"font_family": "mono"}
                )
                app, entry, host = self.entry(text="a" * 1000, multiline=True, **properties)
                paint_tree(app, host)
                self.assertEqual(entry._document_width(host), 11_000)
                self.assertLess(sum(map(len, host.texts)), 100)
                if inherited:
                    app.theme = light()
                else:
                    entry.font_family = "ui"
                paint_tree(app, host)
                self.assertEqual(entry._document_width(host), 7_000)
                self.assertEqual(host.texts, ["a" * 1000])
                self.assertFalse(host.draws[0][3])
                self.assertLess(host.carets[0][0], entry.bounds.right)
                if inherited:
                    app.theme = light().derive(font_family="mono")
                else:
                    entry.font_family = "mono"
                host.largest_measure = 0
                paint_tree(app, host)
                self.assertEqual(entry._document_width(host), 11_000)
                self.assertLess(host.largest_measure, 100)
                self.assertLess(sum(map(len, host.texts)), 100)

    def test_non_ascii_passwords_and_whole_row_extensions_keep_complete_rows(self):
        for properties, expected in (
            ({"text": "a" * 300 + "é"}, "a" * 300 + "é"),
            ({"text": "a" * 300, "password": True}, "•" * 300),
        ):
            with self.subTest(properties=properties):
                app, entry, host = self.entry(font_family="mono", **properties)
                paint_tree(app, host)
                self.assertEqual(host.texts, [expected])
        app, entry, host = self.entry(cls=WholeRows, text="a" * 1000, font_family="mono")
        paint_tree(app, host)
        self.assertEqual(entry._painted_rows, [(0, "a" * 1000)])
        app, entry, host = self.entry(cls=FragmentRows, text="a" * 1000, font_family="mono")
        paint_tree(app, host)
        self.assertEqual(len(entry._painted_rows), 1)
        row, start, fragment = entry._painted_rows[0]
        self.assertEqual(row, 0)
        self.assertGreater(start, 900)
        self.assertLess(len(fragment), 100)
