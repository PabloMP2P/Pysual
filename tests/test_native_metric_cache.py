"""Native metric caching can be checked without a compiled host process."""

import unittest
from unittest.mock import Mock

from _ui_testcase import UIOwnerTestCase
from pysual import Rect, TextBox, dark
from pysual.backends.native import NativeHost
from pysual.painting import Painter


class NativeMetricCacheTests(unittest.TestCase):
    def setUp(self):
        self.host = NativeHost()

        def request(operation, **kwargs):
            texts = kwargs["texts"] if operation == "measure_many" else [kwargs["text"]]
            result = [[len(text) * 8, 16] for text in texts]
            return result if operation == "measure_many" else result[0]

        self.host._request = Mock(side_effect=request)

    def assert_accounted(self):
        self.assertEqual(
            self.host._metric_bytes,
            sum(cost for _, cost in self.host._metrics.values()),
        )

    def test_cold_duplicates_are_measured_once_and_return_in_input_order(self):
        values = self.host.measure_many(["Pending", "Done", "Pending", "Done"], 15)
        self.assertEqual(values, [(56, 16), (32, 16), (56, 16), (32, 16)])
        self.host._request.assert_called_once_with(
            "measure_many", texts=["Pending", "Done"], size=15, mono=False,
        )
        self.assertEqual(len(self.host._metrics), 2)
        self.assert_accounted()
        self.host._request.reset_mock()
        self.assertEqual(self.host.measure_many(["Done", "Pending"], 15), [(32, 16), (56, 16)])
        self.host._request.assert_not_called()

    def test_mixed_hits_and_duplicate_misses_keep_positions_and_font_keys(self):
        self.host.measure("Cached", 15)
        self.host._request.reset_mock()
        self.assertEqual(
            self.host.measure_many(["New", "Cached", "New", "", "Cached"], 15),
            [(24, 16), (48, 16), (24, 16), (0, 16), (48, 16)],
        )
        self.host._request.assert_called_once_with(
            "measure_many", texts=["New", ""], size=15, mono=False,
        )
        self.host.measure_many(["New", "New"], 16, True)
        self.assertIn(("New", 15, False), self.host._metrics)
        self.assertIn(("New", 16, True), self.host._metrics)
        self.assert_accounted()

    def test_replacement_updates_result_and_lru_without_charging_twice(self):
        self.host.measure_many(["first", "second"], 15)
        before = self.host._metric_bytes
        self.host._store_metric("first", 15, False, (99, 20))
        self.assertEqual(self.host.measure("first", 15), (99, 20))
        self.assertEqual(self.host._metric_bytes, before)
        self.assertEqual(list(self.host._metrics), [("second", 15, False), ("first", 15, False)])
        self.assert_accounted()

    def test_replacement_at_entry_limit_does_not_evict_an_unrelated_entry(self):
        for index in range(16384):
            self.host._store_metric(str(index), 15, False, (10, 16))
        self.host._store_metric("100", 15, False, (20, 16))
        self.assertEqual(len(self.host._metrics), 16384)
        self.assertIn(("0", 15, False), self.host._metrics)
        self.host._store_metric("new", 15, False, (30, 16))
        self.assertEqual(len(self.host._metrics), 16384)
        self.assertNotIn(("0", 15, False), self.host._metrics)
        self.assertIn(("100", 15, False), self.host._metrics)
        self.assert_accounted()

    def test_cold_caption_accounts_for_repeated_ellipsis_once(self):
        painter = Painter(self.host, Rect(0, 0, 100, 30), dark())
        self.assertEqual(painter.elide("A longer title", 64), "A longe…")
        batches = [call.kwargs["texts"] for call in self.host._request.call_args_list
                   if call.args[0] == "measure_many"]
        self.assertEqual(sum(texts.count("…") for texts in batches), 1)
        self.assert_accounted()


class TextDocumentMetricTests(UIOwnerTestCase):
    def setUp(self):
        self.host = NativeHost()

        def request(operation, **kwargs):
            texts = kwargs["texts"] if operation == "measure_many" else [kwargs["text"]]
            widths = [[len(text) * 8, 16] for text in texts]
            return widths if operation == "measure_many" else widths[0]

        self.host._request = Mock(side_effect=request)

    def test_document_batches_unique_lines_and_only_measures_new_content(self):
        lines = [f"Line {index:04d}" for index in range(5000)]
        entry = TextBox(multiline=True, text="\n".join(lines + lines[:20]))
        self.assertEqual(entry._document_width(self.host), 72)
        calls = self.host._request.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["measure", "measure_many", "measure_many"])
        self.assertEqual([len(c.kwargs["texts"]) for c in calls[1:]], [4096, 904])
        self.assertEqual({t for c in calls[1:] for t in c.kwargs["texts"]}, set(lines))
        self.host._request.reset_mock()
        self.assertEqual(entry._document_width(self.host), 72)
        self.host._request.assert_not_called()
        entry.text += "\nA newly appended longer line"
        self.assertEqual(entry._document_width(self.host), len("A newly appended longer line") * 8)
        self.assertEqual(self.host._request.call_count, 1)
        self.assertEqual(self.host._request.call_args.kwargs["texts"], ["A newly appended longer line"])

    def test_fixed_pitch_tabs_passwords_and_non_batch_hosts_keep_widths(self):
        from types import SimpleNamespace

        plain = SimpleNamespace(measure=lambda text, size, mono=False: (len(text) * 8, 16))
        for mono, password in ((False, False), (True, False), (False, True)):
            with self.subTest(mono=mono, password=password):
                entry = TextBox(multiline=True, text="AV\tTo\ncafé\ne\u0301", monospace=mono, password=password)
                expected = entry._document_width(plain)
                self.assertEqual(entry._document_width(self.host), expected)
        self.host._request.reset_mock()
        fixed = TextBox(multiline=True, monospace=True, text="A\tB\nlonger")
        self.assertEqual(fixed._document_width(self.host), 48)
        # The fixed-pitch path needs at most its single character advance.
        self.assertFalse(any(c.args[0] == "measure_many" for c in self.host._request.call_args_list))

    def test_long_document_lines_bound_packets_and_use_uncached_results(self):
        # NativeHost intentionally does not cache lines longer than 8,192 chars.
        lines = [str(index) + "x" * 10000 for index in range(110)]
        entry = TextBox(multiline=True, text="\n".join(lines))
        self.assertEqual(entry._document_width(self.host), max(map(len, lines)) * 8)
        calls = self.host._request.call_args_list
        self.assertEqual(sum(c.args[0] == "measure" for c in calls), 1)
        batches = [c.kwargs["texts"] for c in calls if c.args[0] == "measure_many"]
        self.assertEqual(len(batches), 2)
        self.assertTrue(all(sum(map(len, texts)) <= 1024 * 1024 for texts in batches))
