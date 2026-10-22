"""Soft wrapping preserves document offsets through editing and interaction."""

import copy
import pickle
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase
from pysual import App, Style, TextBox, light
from pysual._text_wrap import wrap_line
from pysual.graphemes import boundaries
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree, resolve_style
from pysual.runtime import Runtime
from test_text_tabs_clicks import GeometryHost, SourceFragments
from test_text_visual_navigation import FontHost


class WrapHost(GeometryHost):
    def measure(self, text, size, mono=False):
        self.largest_measure = max(self.largest_measure, len(text))
        return len(text) * size, size * 1.2


class TextWordWrapTests(UIOwnerTestCase):
    def entry(self, text, *, cls=TextBox, host=None, **kwargs):
        app = App(width=600, height=600, reduce_motion=True,
                  theme=light().styled(TextBox, Style(padding=0)))
        self.addCleanup(app.destroy)
        host = host or WrapHost()
        entry = cls(parent=app, left=20, top=20, text=text, **{
            "width": 100, "height": 400, "font_size": 10,
            "multiline": True, "monospace": True, "word_wrap": True, **kwargs,
        })
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        runtime.router.focus = entry
        arrange(app, host)
        entry.select(0, 0)
        paint_tree(app, host)
        return app, entry, host, runtime

    def key(self, runtime, key, *, shift=False, ctrl=False):
        runtime.router.process(Input("key_down", key=key, shift=shift, ctrl=ctrl))

    def click(self, runtime, entry, column, row, *, now=10):
        size = resolve_style(entry).font_size
        x = entry.bounds.x + entry._text_left() + column * size
        y = (entry.bounds.y + resolve_style(entry).padding
             + row * entry._line_height() + 1)
        with patch("pysual.text.monotonic", return_value=now):
            runtime.router.process(Input("pointer_down", x=x, y=y))
            runtime.router.process(Input("pointer_up", x=x, y=y))

    def assert_caret_row(self, entry, host, row):
        self.assertTrue(host.carets)
        self.assertAlmostEqual(
            host.carets[-1][1], entry.bounds.y + resolve_style(entry).padding
            + (row - entry.capture_view_state()[2]) * entry._line_height(),
        )

    def test_default_and_single_line_fields_retain_horizontal_scrolling(self):
        plain = TextBox()
        self.addCleanup(plain.destroy)
        self.assertFalse(plain.word_wrap)
        for options in ({"word_wrap": False}, {"multiline": False}):
            with self.subTest(options=options):
                app, entry, host, _ = self.entry("a" * 100, **options)
                entry.select(len(entry.text), len(entry.text))
                paint_tree(app, host)
                self.assertEqual(entry.line_count, 1)
                self.assertEqual(entry._display_lines()[0], [entry.text])
                self.assertGreater(entry.capture_view_state()[3], 0)

    def test_paint_wraps_words_and_preserves_explicit_breaks_and_whitespace(self):
        source = "alpha beta gamma\n\n  last words  "
        _, entry, host, _ = self.entry(source)
        lines, starts = entry._display_lines()
        self.assertGreater(len(lines), 3)
        self.assertIn("", lines)
        self.assertEqual("".join(host.texts), source.replace("\n", ""))
        self.assertEqual(entry.text, source)
        self.assertEqual(entry.line_count, 3)
        self.assertEqual(entry.capture_view_state()[3], 0)
        for line, start in zip(lines, starts):
            self.assertEqual(line, source[start:start + len(line)])
        # The first word plus its separator fits, while two words do not.
        self.assertEqual(lines[0], "alpha ")

    def test_selection_across_soft_breaks_replaces_and_undoes_source_text(self):
        source = "alpha beta gamma delta"
        app, entry, host, _ = self.entry(source)
        lines, starts = entry._display_lines()
        start, end = 2, starts[2] + 2
        entry.select(start, end)
        paint_tree(app, host)
        self.assertEqual(entry.selection_text, source[start:end])
        selections = [rect for rect, color in host.rects
                      if color == entry.effective_theme.tokens.selection]
        self.assertEqual(len(selections), 3)
        entry.replace_selection("Q")
        self.assertEqual(entry.text, source[:start] + "Q" + source[end:])
        entry.undo()
        self.assertEqual(entry.text, source)
        self.assertEqual(entry.selection_range, (start, end))
        entry.redo()
        self.assertEqual(entry.text, source[:start] + "Q" + source[end:])

    def test_vertical_home_end_and_page_navigation_use_visual_rows(self):
        app, entry, host, runtime = self.entry("abcdefghijklmnopqrstuvwxyz" * 4, height=80)
        lines, starts = entry._display_lines()
        entry.select(1, 1)
        self.key(runtime, "ArrowDown", shift=True)
        self.assertEqual(entry.selection_range, (1, starts[1] + 1))
        self.key(runtime, "ArrowUp", shift=True)
        self.assertEqual(entry.selection_range, (1, 1))
        self.key(runtime, "ArrowDown")
        self.key(runtime, "Home")
        self.assertEqual(entry.selection_range, (starts[1], starts[1]))
        self.key(runtime, "End")
        end = starts[1] + len(lines[1])
        self.assertEqual(entry.selection_range, (end, end))
        paint_tree(app, host)
        self.assert_caret_row(entry, host, 1)
        self.key(runtime, "Home")
        self.assertEqual(entry.selection_range, (starts[1], starts[1]))
        entry.select(1, 1)
        page = entry._page_rows()
        self.key(runtime, "PageDown")
        self.assertEqual(entry.selection_range, (starts[page] + 1,) * 2)
        self.key(runtime, "Home", ctrl=True)
        self.assertEqual(entry.selection_range, (0, 0))
        self.key(runtime, "End", ctrl=True)
        self.assertEqual(entry.selection_range, (len(entry.text),) * 2)

    def test_pointer_at_soft_break_retains_clicked_visual_row(self):
        app, entry, host, runtime = self.entry("abcdefghijklmnopqrstuvwxyz")
        lines, starts = entry._display_lines()
        self.click(runtime, entry, 1.1, 1)
        self.assertEqual(entry.selection_range, (starts[1] + 1,) * 2)
        paint_tree(app, host)
        self.assert_caret_row(entry, host, 1)
        self.click(runtime, entry, len(lines[0]), 0, now=11)
        self.assertEqual(entry.selection_range, (len(lines[0]),) * 2)
        paint_tree(app, host)
        self.assert_caret_row(entry, host, 0)
        self.key(runtime, "Home")
        self.assertEqual(entry.selection_range, (0, 0))

    def test_double_click_selects_entire_word_across_forced_wraps(self):
        source = "abcdefghijklmnopqrstuvwxyz"
        _, entry, _, runtime = self.entry(source)
        self.click(runtime, entry, 1.1, 1)
        self.click(runtime, entry, 1.1, 1, now=10.2)
        self.assertEqual(entry.selection_text, source)

    def test_line_numbers_and_fragment_hooks_keep_logical_source_coordinates(self):
        source = "alpha beta gamma delta\nsecond\n"
        _, entry, host, _ = self.entry(source, cls=SourceFragments, line_numbers=True,
                                      width=140)
        numbers = [text for text, x, _, _ in host.draws
                   if x == entry.bounds.x + 5]
        self.assertEqual(numbers, ["1", "2", "3"])
        self.assertEqual(entry.line_count, 3)
        logical = source.split("\n")
        fragments = entry._fragments
        self.assertGreater(len(fragments), 3)
        for row, column, text in fragments:
            self.assertEqual(text, logical[row][column:column + len(text)])
        self.assertTrue(any(row == 0 and column > 0 for row, column, _ in fragments))

    def test_ime_preview_wraps_without_mutating_document_or_history(self):
        app, entry, host, runtime = self.entry("AB")
        entry.select(1, 1)
        before = entry.text, entry.selection_range, entry._undo[:], entry._redo[:]
        preview = "alpha beta gamma"
        runtime.router.process(Input("composition", text=preview))
        paint_tree(app, host)
        self.assertEqual("".join(host.texts), "A" + preview + "B")
        self.assertGreater(len(host.texts), 1)
        self.assertEqual((entry.text, entry.selection_range, entry._undo, entry._redo), before)
        self.assertEqual(entry.capture_view_state()[3], 0)
        runtime.router.process(Input("composition", text=""))
        paint_tree(app, host)
        self.assertEqual(host.texts, ["AB"])
        runtime.router.process(Input("composition", text=preview))
        runtime.router.process(Input("text", text=preview))
        self.assertEqual(entry.text, "A" + preview + "B")
        entry.undo()
        self.assertEqual(entry.text, "AB")

    def test_resize_font_and_wrap_toggle_reflow_without_editing(self):
        source = "alpha beta gamma delta epsilon zeta"
        app, entry, host, _ = self.entry(source)
        original_count = len(entry._display_lines()[0])
        entry.width = 200
        arrange(app, host)
        paint_tree(app, host)
        wide_count = len(entry._display_lines()[0])
        self.assertLess(wide_count, original_count)
        entry.font_size = 20
        arrange(app, host)
        paint_tree(app, host)
        self.assertGreater(len(entry._display_lines()[0]), wide_count)
        entry.word_wrap = False
        entry.select(len(source), len(source))
        paint_tree(app, host)
        self.assertEqual(entry._display_lines()[0], [source])
        self.assertGreater(entry.capture_view_state()[3], 0)
        entry.word_wrap = True
        paint_tree(app, host)
        self.assertGreater(len(entry._display_lines()[0]), 1)
        self.assertEqual(entry.capture_view_state()[3], 0)
        self.assertEqual(entry.text, source)
        self.assertFalse(entry.can_undo)

    def test_graphemes_and_password_masks_use_legal_source_boundaries(self):
        source = ("e\u0301👩🏽‍💻" * 4) + "tail"
        for password in (False, True):
            with self.subTest(password=password):
                _, entry, host, _ = self.entry(source, password=password, height=550)
                lines, starts = entry._display_lines()
                self.assertGreater(len(lines), 1)
                legal = boundaries(source)
                for line, start in zip(lines, starts):
                    self.assertIn(start, legal)
                    self.assertIn(start + len(line), legal)
                self.assertEqual("".join(host.texts),
                                 "•" * (len(legal) - 1) if password else source)
                self.assertEqual(entry.text, source)

    def test_proportional_metrics_drive_wrapping_navigation_and_resource_reflow(self):
        source = "WWWWWWiiiiiiiiiiiiiiiiWWWWWWiiiiiiiiiiiiiiii"
        app, entry, host, runtime = self.entry(source, monospace=False)
        scale = 1

        def measure(text, size, mono=False):
            return sum(18 if char == "W" else 3 for char in text) * scale, size * 1.2

        host.measure = measure
        host.resource_revision = 1
        entry.invalidate()
        paint_tree(app, host)
        lines, starts = entry._display_lines()
        available = entry._text_viewport().right - entry._text_left() - 14
        for line in lines:
            self.assertLessEqual(measure(line, 10)[0], available)
        entry.select(2, 2)
        x = measure(lines[0][:2], 10)[0]
        expected = min(boundaries(lines[1]),
                       key=lambda column: (abs(measure(lines[1][:column], 10)[0] - x), -column))
        self.key(runtime, "ArrowDown")
        self.assertEqual(entry.selection_range, (starts[1] + expected,) * 2)
        old_count = len(lines)
        scale = 2
        host.resource_revision += 1
        entry.invalidate()
        paint_tree(app, host)
        self.assertGreater(len(entry._display_lines()[0]), old_count)
        self.assertEqual(entry.text, source)

    def test_resize_reveals_caret_without_inventing_a_new_source_position(self):
        app, entry, host, runtime = self.entry("abcdefghijklmnopqrstuvwxyz" * 5,
                                              width=180, height=70)
        entry.select(40, 40)
        self.key(runtime, "End")
        before = entry.selection_range
        entry.width = 80
        arrange(app, host)
        paint_tree(app, host)
        self.assertEqual(entry.selection_range, before)
        self.assertGreaterEqual(host.carets[-1][0], entry.bounds.x + entry._text_left())
        self.assertLess(host.carets[-1][0], entry.bounds.right)
        self.assertGreaterEqual(host.carets[-1][1], entry.bounds.y)
        self.assertLess(host.carets[-1][1], entry.bounds.bottom)

    def test_tabs_preserve_logical_grid_across_wraps_and_pointer_positions(self):
        source = "abcde\tfghij\tklmno\tp"
        app, entry, host, runtime = self.entry(source, width=110)
        self.assertGreater(len(entry._display_lines()[0]), 1)
        self.assertEqual("".join(host.texts), source.expandtabs(entry.tab_size))
        lines, starts = entry._display_lines()
        for row, (line, start) in enumerate(zip(lines, starts)):
            # A source column is measured on its original logical tab grid.
            prefix_width = len(source[:start].expandtabs(entry.tab_size))
            for column in range(len(line) + 1):
                x = len(source[:start + column].expandtabs(entry.tab_size)) - prefix_width
                self.click(runtime, entry, x, row, now=100 + row * 20 + column)
                self.assertEqual(entry.selection_range, (start + column,) * 2)
        entry.select(0, 0)
        entry.tab_size = 8
        paint_tree(app, host)
        self.assertEqual("".join(host.texts), source.expandtabs(8))

    def test_masked_whitespace_occupies_visible_space_on_each_wrapped_row(self):
        _, entry, host, _ = self.entry("alpha          beta", password=True)
        available = entry._text_viewport().right - entry._text_left() - 14
        self.assertEqual("".join(host.texts), "•" * len(entry.text))
        for line in host.texts:
            self.assertLessEqual(host.measure(line, entry.font_size)[0], available)

    def test_bundled_font_wrap_and_caret_use_shaped_fragment_metrics(self):
        app, entry, host, _ = self.entry(
            "To AV wide words with cafe\u0301 and more text", host=FontHost(),
            monospace=False, font_size=18, width=145)
        lines, starts = entry._display_lines()
        available = entry._text_viewport().right - entry._text_left() - 14
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(host.measure(line.rstrip(), 18, False)[0], available)
        column = min(2, len(lines[1]))
        entry.select(starts[1] + column, starts[1] + column)
        paint_tree(app, host)
        self.assert_caret_row(entry, host, 1)
        self.assertAlmostEqual(host.carets[-1][0], entry.bounds.x + entry._text_left()
                               + host.measure(lines[1][:column], 18, False)[0])

    def test_exact_fit_words_keep_separators_and_the_end_caret_visible(self):
        app, entry, host, runtime = self.entry("hello world", width=66)
        self.assertEqual(entry._display_lines()[0], ["hello ", "world"])
        self.key(runtime, "End")
        paint_tree(app, host)
        self.assertEqual(entry.selection_range, (6, 6))
        self.assert_caret_row(entry, host, 0)
        self.assertLess(host.carets[-1][0], entry.bounds.right)

    def test_navigation_reuses_wrap_metrics_and_serialization_drops_layout_caches(self):
        app, entry, host, runtime = self.entry("alpha beta gamma " * 30, height=70)
        with patch("pysual.text.wrap_line", wraps=wrap_line) as wrap:
            for _ in range(3):
                self.key(runtime, "ArrowRight")
                paint_tree(app, host)
            self.assertEqual(wrap.call_count, 0)
        self.assertTrue(entry._wrap_cache)
        # A detached editor can be restored on a different host.
        editor = TextBox(text=entry.text, width=100, multiline=True, word_wrap=True)
        self.addCleanup(editor.destroy)
        arrange(editor, host)
        editor._wrapped_layout(host)
        self.assertTrue(editor._wrap_cache)
        for restore in (copy.deepcopy, lambda control: pickle.loads(pickle.dumps(control))):
            clone = restore(editor)
            self.addCleanup(clone.destroy)
            self.assertTrue(clone.word_wrap)
            self.assertEqual(clone.text, editor.text)
            self.assertEqual(clone.selection_range, editor.selection_range)
            self.assertEqual(clone._wrap_cache, {})
            self.assertIsNone(clone._measure_host)

    def test_vertical_scrollbar_uses_visual_rows_and_horizontal_wheel_is_disabled(self):
        _, entry, _, runtime = self.entry("alpha beta gamma delta " * 20, height=70)
        self.assertEqual(entry.line_count, 1)
        self.assertEqual(entry._scroll_axes, (False, True))
        runtime.router.process(Input("wheel", delta=3,
                                     x=entry.bounds.x + 10, y=entry.bounds.y + 10))
        self.assertGreater(entry.capture_view_state()[2], 0)
        runtime.router.process(Input("wheel", delta=10, shift=True,
                                     x=entry.bounds.x + 10, y=entry.bounds.y + 10))
        self.assertEqual(entry.capture_view_state()[3], 0)
        entry.restore_view_state((0, 0, 10000, 10000.0))
        self.assertEqual(entry.capture_view_state()[2],
                         len(entry._display_lines()[0]) - entry._page_rows())
        self.assertEqual(entry.capture_view_state()[3], 0)
