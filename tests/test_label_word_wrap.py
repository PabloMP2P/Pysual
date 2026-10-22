"""Word wrapping keeps source text intact and follows allocated label widths."""

import unittest

from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import Container, Label
from pysual._text_wrap import wrap_line
from pysual.graphemes import boundaries
from pysual.layout import arrange
from pysual.painting import paint_tree


class WrapHost(RecordingHost):
    def measure(self, text, size, mono=False):
        lines = text.split("\n")
        return (max((len(boundaries(line)) - 1) * 10 for line in lines),
                len(lines) * 20)


class WordWrapTests(unittest.TestCase):
    def test_word_boundaries_and_all_source_whitespace_are_preserved(self):
        text = "first second  third"
        rows = wrap_line(text, 8, lambda start, end: len(text[start:end]))
        self.assertEqual([text[start:end] for start, end in rows],
                         ["first ", "second  ", "third"])
        self.assertEqual("".join(text[start:end] for start, end in rows), text)

    def test_exact_fit_words_keep_overhanging_separators_on_the_previous_row(self):
        for text, width, expected in (
            ("hello world", 5, ["hello ", "world"]),
            ("one two three", 7, ["one two ", "three"]),
            ("hello  \tworld", 5, ["hello  \t", "world"]),
            ("hello  \tworld", 6, ["hello  \t", "world"]),
            ("  hello world", 7, ["  hello ", "world"]),
        ):
            with self.subTest(text=text, width=width):
                rows = wrap_line(text, width,
                                 lambda start, end: len(text[start:end].expandtabs(4)))
                self.assertEqual([text[start:end] for start, end in rows], expected)
                self.assertEqual("".join(text[start:end] for start, end in rows), text)

    def test_leading_whitespace_and_long_trailing_runs_make_bounded_progress(self):
        text = " " * 20 + "hello" + " \t" * 10_000 + "world"
        measured = 0
        def measure(start, end):
            nonlocal measured
            measured += end - start
            return len(text[start:end].expandtabs(4))
        rows = wrap_line(text, 5, measure)
        self.assertEqual([text[start:end] for start, end in rows],
                         [" " * 5] * 4 + ["hello" + " \t" * 10_000, "world"])
        self.assertLess(measured, len(text))

    def test_empty_exact_fit_and_narrow_rows(self):
        self.assertEqual(wrap_line("", 0, lambda start, end: 0), [(0, 0)])
        self.assertEqual(wrap_line("abc", 3, lambda start, end: end - start), [(0, 3)])
        self.assertEqual(wrap_line("abc", 0, lambda start, end: end - start),
                         [(0, 1), (1, 2), (2, 3)])
        with self.assertRaises(ValueError):
            wrap_line("abc", float("inf"), lambda start, end: end - start)

    def test_long_words_split_only_at_grapheme_boundaries(self):
        clusters = ["a\u0301", "👩‍💻", "🇪🇸", "क्‍ष"]
        text = "".join(clusters)
        rows = wrap_line(text, 1, lambda start, end: len(boundaries(text[start:end])) - 1)
        self.assertEqual([text[start:end] for start, end in rows], clusters)

    def test_metrics_can_expand_tabs_or_use_proportional_glyphs(self):
        text = "Wii\tiiW"
        def measure(start, end):
            return sum(3 if char == "W" else 1
                       for char in text[start:end].expandtabs(4))
        rows = wrap_line(text, 6, measure)
        self.assertEqual([text[start:end] for start, end in rows], ["Wii\t", "iiW"])
        self.assertTrue(all(measure(start, end) <= 6 for start, end in rows))

    def test_long_narrow_lines_bound_total_measurement_work(self):
        text = "x" * 10_000
        measured = 0
        def measure(start, end):
            nonlocal measured
            measured += end - start
            return end - start
        rows = wrap_line(text, 10, measure)
        self.assertEqual(len(rows), 1000)
        self.assertLess(measured, len(text) * 10)


class LabelWordWrapTests(UIOwnerTestCase):
    def label(self, **kwargs):
        label = Label(**kwargs)
        self.addCleanup(label.destroy)
        return label

    def test_default_keeps_natural_size_and_ellipsis(self):
        label = self.label(text="alpha beta", width=60, height=20)
        host = WrapHost()
        host.size = (60, 20)
        self.assertFalse(label.word_wrap)
        self.assertEqual(label.measure_available(host, width=60), (100, 20))
        arrange(label, host)
        paint_tree(label, host)
        self.assertEqual(host.texts, ["alpha…"])
        self.assertEqual(label.text, "alpha beta")

    def test_declared_available_and_maximum_width_measure_wrapped_height(self):
        host = WrapHost()
        explicit = self.label(text="alpha beta", word_wrap=True, width=60)
        self.assertEqual(explicit.measure(host), (60, 40))
        self.assertEqual(explicit.measure_available(host, width=200), (60, 40))
        flexible = self.label(text="alpha beta", word_wrap=True)
        self.assertEqual(flexible.measure(host), (100, 20))
        self.assertEqual(flexible.measure_available(host, width=60), (60, 40))
        flexible.max_width = 60
        self.assertEqual(flexible.measure(host), (60, 40))

    def test_overhanging_whitespace_and_graphemes_do_not_enlarge_wrapped_measure(self):
        host = WrapHost()
        explicit = self.label(text="hello world", word_wrap=True, width=50)
        self.assertEqual(explicit.measure(host), (50, 40))
        self.assertEqual(explicit.measure_available(host, width=100), (50, 40))
        flexible = self.label(text="hello world", word_wrap=True)
        self.assertEqual(flexible.measure_available(host, width=50), (50, 40))
        narrow = self.label(text="👩‍💻", word_wrap=True, width=5)
        self.assertEqual(narrow.measure(host), (5, 20))

    def test_explicit_blank_and_trailing_lines_survive_painting(self):
        text = "a b\n\nc\n"
        label = self.label(text=text, word_wrap=True, width=20)
        host = WrapHost()
        host.size = (20, 100)
        arrange(label, host)
        paint_tree(label, host)
        self.assertEqual(host.texts, ["a \nb\n\nc\n"])
        self.assertEqual(label.bounds.height, 100)
        self.assertEqual(label.text, text)

    def test_parent_width_and_resize_update_height_and_following_control(self):
        parent = Container(layout="stack", direction="vertical", width=60,
                           height=100, padding=0, spacing=0)
        self.addCleanup(parent.destroy)
        label = Label(parent=parent, text="alpha beta", word_wrap=True)
        following = Label(parent=parent, text="next")
        host = WrapHost()
        host.size = (60, 100)
        arrange(parent, host)
        self.assertEqual((label.bounds.width, label.bounds.height), (60, 40))
        self.assertEqual(following.bounds.y, 40)
        parent.width = 120
        host.size = (120, 100)
        arrange(parent, host)
        self.assertEqual((label.bounds.width, label.bounds.height), (120, 20))
        self.assertEqual(following.bounds.y, 20)
        self.assertEqual(label.text, "alpha beta")

    def test_grid_auto_row_uses_wrapped_height_and_toggle_invalidates_it(self):
        parent = Container(layout="grid", width=60, height=100, padding=0, spacing=0,
                           column_tracks=(60,), row_tracks=("auto", "auto"))
        self.addCleanup(parent.destroy)
        label = Label(parent=parent, text="alpha beta", word_wrap=True)
        following = Label(parent=parent, text="next")
        host = WrapHost()
        arrange(parent, host)
        self.assertEqual((label.bounds.height, following.bounds.y), (40, 40))
        label.word_wrap = False
        arrange(parent, host)
        self.assertEqual((label.bounds.height, following.bounds.y), (20, 20))

    def test_unbreakable_grapheme_is_preserved_in_a_narrow_label(self):
        label = self.label(text="👩‍💻a\u0301", word_wrap=True, width=5)
        host = WrapHost()
        host.size = (5, 40)
        arrange(label, host)
        paint_tree(label, host)
        self.assertEqual(host.texts, ["👩‍💻\na\u0301"])
        self.assertEqual(label.bounds.width, 5)
        self.assertEqual(label._clip.width, 5)


if __name__ == "__main__":
    unittest.main()
