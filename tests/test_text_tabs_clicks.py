"""Tab geometry keeps source offsets; ordinary double clicks select whole words."""

import importlib.util
import os
import unittest
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase, AsyncUIOwnerTestCase
from pysual import App, ComboBox, TextBox
from pysual.graphemes import is_boundary
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree, resolve_style
from pysual.runtime import Runtime
from test_text_composition_fonts import TextHost, WholeRows


class GeometryHost(TextHost):
    def __init__(self):
        super().__init__()
        self.rects = []

    def begin(self, color):
        super().begin(color)
        self.rects.clear()

    def rect(self, rect, fill, *args):
        self.rects.append((rect, fill))


class SourceFragments(TextBox):
    def _initialize(self):
        super()._initialize()
        self._fragments = []

    def paint_line_fragment(self, painter, text, x, y, row, style, start_column):
        self._fragments.append((row, start_column, text))
        super().paint_line_fragment(painter, text, x, y, row, style, start_column)


class TextTabsAndClicksTests(UIOwnerTestCase):
    def entry(self, text, *, cls=TextBox, **kwargs):
        app, host = App(reduce_motion=True), GeometryHost()
        self.addCleanup(app.destroy)
        properties = {"multiline": True, "monospace": True, "width": 400, "height": 180}
        properties.update(kwargs)
        entry = cls(parent=app, text=text, **properties)
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        runtime.router.focus = entry
        arrange(app, host)
        return app, entry, host, runtime

    def point(self, entry, column, row=0):
        return (entry.bounds.x + entry._text_left() + column * 11 + 0.1,
                entry.bounds.y + resolve_style(entry).padding + row * entry._line_height() + 1)

    def click(self, runtime, entry, column, *, now=10, row=0, **kwargs):
        x, y = self.point(entry, column, row)
        with patch("pysual.text.monotonic", return_value=now):
            runtime.router.process(Input("pointer_down", x=x, y=y, **kwargs))
            runtime.router.process(Input("pointer_up", x=x, y=y, **kwargs))

    def test_tabs_share_width_caret_selection_and_hit_geometry(self):
        source = " a\tb\tZ"
        app, entry, host, runtime = self.entry(source)
        entry.select(3, 5)
        paint_tree(app, host)
        self.assertEqual(host.texts, [source.expandtabs(4)])
        self.assertEqual(entry._document_width(host), 9 * 11)
        self.assertEqual(host.carets[0][0], entry.bounds.x + entry._text_left() + 8 * 11)
        selected = [rect for rect, fill in host.rects if fill == entry.effective_theme.tokens.selection]
        self.assertEqual([(r.x - entry.bounds.x - entry._text_left(), r.width) for r in selected], [(44, 44)])
        for column, expected in ((0, 0), (2, 2), (3, 3), (4, 3), (5, 4), (7, 5), (8, 5), (9, 6)):
            self.click(runtime, entry, column, now=10 + column)
            self.assertEqual(entry.selection_range, (expected, expected))
        entry.tab_size = 8
        paint_tree(app, host)
        self.assertEqual(entry._document_width(host), 17 * 11)
        self.assertEqual(host.texts, [source.expandtabs(8)])
        self.assertEqual(entry.text, source)

    def test_single_click_and_drag_choose_nearest_insertion_boundary(self):
        for cls in (TextBox, ComboBox):
            with self.subTest(control=cls.__name__):
                _, entry, _, runtime = self.entry("hello world", cls=cls, multiline=False)
                for index, (column, expected) in enumerate(((0.2, 0), (0.8, 1), (4.8, 5), (20, 11))):
                    self.click(runtime, entry, column, now=10 + index)
                    self.assertEqual(entry.selection_range, (expected, expected))
                x, y = self.point(entry, 0.8)
                runtime.router.process(Input("pointer_down", x=x, y=y))
                x, y = self.point(entry, 2.8)
                runtime.router.process(Input("pointer_move", x=x, y=y))
                runtime.router.process(Input("pointer_up", x=x, y=y))
                self.assertEqual(entry.selection_text, "el")
                self.click(runtime, entry, 3.8, shift=True, now=20)
                self.assertEqual(entry.selection_text, "ell")

    def test_proportional_clicks_use_actual_glyph_and_tab_midpoints(self):
        _, entry, host, runtime = self.entry("W\ti", monospace=False)
        host.measure = lambda text, size, mono=False: (
            sum({"W": 19, "i": 5}.get(char, 7) for char in text), size * 1.2
        )
        # 'W' spans 0..19, the expanded tab 19..40, and 'i' 40..45.
        for index, (offset, expected) in enumerate(((3.8, 0), (15.2, 1), (23.2, 1), (35.8, 2), (41, 2), (44, 3))):
            self.click(runtime, entry, offset / 11, now=10 + index)
            self.assertEqual(entry.selection_range, (expected, expected))

    def test_clicks_choose_whole_graphemes_in_plain_and_masked_text(self):
        for cluster in ("e\u0301", "👩🏽‍💻"):
            for password in (False, True):
                with self.subTest(cluster=cluster, password=password):
                    _, entry, _, runtime = self.entry(cluster + "x", password=password)
                    for index, (fraction, expected) in enumerate(((0.2, 0), (0.8, len(cluster)))):
                        self.click(runtime, entry, (1 if password else len(cluster)) * fraction,
                                   now=10 + index)
                        self.assertEqual(entry.selection_range, (expected, expected))
                        self.assertTrue(is_boundary(entry.text, expected))

    def test_double_click_uses_word_under_pointer_not_nearest_caret(self):
        for source, column, word, caret in (("hello world", 4.8, "hello", 5),
                                          ("cafe\u0301 noir", 4.6, "cafe\u0301", 5)):
            with self.subTest(source=source):
                _, entry, _, runtime = self.entry(source)
                self.click(runtime, entry, column)
                self.assertEqual(entry.selection_range, (caret, caret))
                self.click(runtime, entry, column, now=10.2)
                self.assertEqual(entry.selection_text, word)

    def test_tab_copy_replace_undo_and_password_keep_original_characters(self):
        source = "\talpha\n  beta\tend"
        app, entry, host, _ = self.entry(source)
        entry.select_all()
        self.assertEqual(entry.selection_text, source)
        entry.replace_selection("replacement")
        entry.undo()
        self.assertEqual(entry.text, source)
        self.assertEqual(entry.selection_text, source)
        entry.redo()
        self.assertEqual(entry.text, "replacement")
        entry.load_text(source)
        entry.password = True
        paint_tree(app, host)
        self.assertEqual(host.texts, ["•" * len(line) for line in source.split("\n")])
        self.assertEqual(entry.selection_text, "")

    def test_tab_fragments_and_whole_row_hooks_keep_original_offsets(self):
        source = "ab\tcdef\t" + "x" * 100
        app, entry, host, _ = self.entry(source, cls=SourceFragments)
        entry.restore_view_state((0, 0, 0, 4 * 11))
        paint_tree(app, host)
        row, start, fragment = entry._fragments[-1]
        self.assertEqual(row, 0)
        self.assertEqual(fragment, source[start:start + len(fragment)])
        self.assertEqual(start, 2)  # The viewport starts inside the first tab.
        self.assertNotIn("\t", "".join(host.texts))
        self.assertEqual(host.draws[0][1], entry.bounds.x + entry._text_left() - 2 * 11)
        app, entry, host, _ = self.entry(source, cls=WholeRows)
        paint_tree(app, host)
        self.assertEqual(entry._painted_rows, [(0, source)])
        self.assertEqual(host.texts, [source.expandtabs(4)])

    def test_long_tabbed_line_keeps_host_work_to_visible_columns(self):
        app, entry, host, runtime = self.entry("a\t" + "x" * 1_000_000)
        paint_tree(app, host)
        self.assertLess(host.largest_measure, 100)
        self.assertLess(sum(map(len, host.texts)), 100)
        self.assertGreater(entry._scroll_x, 1_000_000)
        self.assertEqual(entry._document_width(host), 1_000_004 * 11)
        entry.restore_view_state((0, 0, 0, 2 * 11))
        self.click(runtime, entry, 1.8)
        self.assertEqual(entry.selection_range, (2, 2))
        self.assertLess(host.largest_measure, 100)

    def test_tab_preedit_is_visual_only_and_keeps_caret_geometry(self):
        app, entry, host, _ = self.entry("a\tZ")
        entry.select(2, 2)
        before = entry.text, entry.selection_range, entry.can_undo
        entry.handle_input(Input("composition", text="b\t"))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["a\tb\tZ".expandtabs(4)])
        self.assertEqual(host.carets[0][0], entry.bounds.x + entry._text_left() + 8 * 11)
        self.assertEqual((entry.text, entry.selection_range, entry.can_undo), before)
        entry.handle_input(Input("pointer_cancel"))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["a\tZ".expandtabs(4)])

    def test_vertical_navigation_preserves_display_column_across_tabs_and_spaces(self):
        _, entry, _, _ = self.entry("\talpha\n    alpha\nx\n\talpha")
        entry.select(4, 4)  # Display column seven, just before 'h'.
        for expected in (14, 18, 23):
            entry.handle_input(Input("key_down", key="ArrowDown"))
            self.assertEqual(entry.selection_range, (expected, expected))
        entry.handle_input(Input("key_down", key="ArrowUp"))
        entry.handle_input(Input("key_down", key="ArrowUp"))
        self.assertEqual(entry.selection_range, (14, 14))

    def test_tab_size_change_remeasures_scrolled_text_and_reveals_caret(self):
        app, entry, host, _ = self.entry("a\t" * 100)
        entry.select(len(entry.text), len(entry.text))
        paint_tree(app, host)
        old_scroll = entry._scroll_x
        old_width = entry._document_width(host)
        entry.tab_size = 8
        paint_tree(app, host)
        self.assertEqual(entry._document_width(host), old_width * 2)
        self.assertGreater(entry._scroll_x, old_scroll)
        self.assertLessEqual(host.carets[0][0], entry.bounds.right)
        entry.tab_size = 2
        paint_tree(app, host)
        self.assertEqual(entry._document_width(host), old_width / 2)
        self.assertLess(entry._scroll_x, old_scroll)
        self.assertLessEqual(entry._scroll_x, entry._horizontal_limit(host))
        self.assertEqual(entry.selection_range, (len(entry.text), len(entry.text)))

    def test_double_click_selects_words_underscores_and_grapheme_runs(self):
        for source, column, expected in (
            ("hello world", 2, "hello"),
            ("some_name next", 5, "some_name"),
            ("cafe\u0301 noir", 4, "cafe\u0301"),
            ("one 👩🏽‍💻!? two", 6, "👩🏽‍💻!?"),
            ("one   two", 4, "   "),
            ("\talpha beta", 6, "alpha"),
        ):
            with self.subTest(source=source):
                _, entry, _, runtime = self.entry(source)
                self.click(runtime, entry, column)
                self.click(runtime, entry, column, now=10.2)
                self.assertEqual(entry.selection_text, expected)
                self.assertTrue(all(is_boundary(source, offset) for offset in entry.selection_range))
                self.assertFalse(entry.can_undo)

    def test_double_click_in_read_only_and_small_motion_keeps_whole_word(self):
        _, entry, _, runtime = self.entry("hello world", read_only=True)
        self.click(runtime, entry, 2)
        x, y = self.point(entry, 2)
        with patch("pysual.text.monotonic", return_value=10.2):
            runtime.router.process(Input("pointer_down", x=x, y=y))
            runtime.router.process(Input("pointer_move", x=x + 1, y=y))
            self.assertEqual(entry.selection_text, "hello")
            x, y = self.point(entry, 8)
            runtime.router.process(Input("pointer_move", x=x, y=y))
            runtime.router.process(Input("pointer_up", x=x, y=y))
        self.assertEqual(entry.selection_text, "hello world")
        entry.replace_selection("changed")
        self.assertEqual(entry.text, "hello world")

    def test_slow_or_shift_click_and_document_or_focus_reset_do_not_select_word(self):
        _, entry, _, runtime = self.entry("hello world")
        self.click(runtime, entry, 2)
        self.click(runtime, entry, 2, now=11)
        self.assertEqual(entry.selection_range, (2, 2))
        self.click(runtime, entry, 3, now=11.2, shift=True)
        self.assertEqual(entry.selection_text, "l")
        for reset in (
            lambda: entry.load_text("hello world"),
            lambda: entry.handle_input(Input("blur")),
            lambda: entry.handle_input(Input("pointer_cancel")),
            lambda: entry.select(0, 0),
        ):
            self.click(runtime, entry, 2, now=20)
            reset()
            self.click(runtime, entry, 2, now=20.2)
            self.assertEqual(entry.selection_range, (2, 2))

    def test_normal_drag_and_distant_click_remain_character_selection(self):
        _, entry, _, runtime = self.entry("hello world")
        self.click(runtime, entry, 2)
        self.click(runtime, entry, 8, now=10.2)
        self.assertEqual(entry.selection_range, (8, 8))
        x, y = self.point(entry, 0)
        runtime.router.process(Input("pointer_down", x=x, y=y))
        x, y = self.point(entry, 3)
        runtime.router.process(Input("pointer_move", x=x, y=y))
        runtime.router.process(Input("pointer_up", x=x, y=y))
        self.assertEqual(entry.selection_text, "hel")
