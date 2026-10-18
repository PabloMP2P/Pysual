"""Native metric caching can be checked without a compiled host process."""

import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock

import pytest

from _ui_testcase import UIOwnerTestCase
from pysual import Rect, TextBox, dark
from pysual.backends.native import NativeHost
from pysual.painting import Painter
from tools.native import ROOT, compiler_environment


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


def test_metric_cache_keeps_new_entries_reachable_after_collisions(tmp_path):
    try:
        compiler, environment = compiler_environment()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        pytest.skip(f"C compiler unavailable: {error}")
    renderer = (ROOT / "native/sdl_renderer.c").read_text(encoding="utf-8")
    # Compile the production helpers themselves; no separate cache model or
    # renderer instrumentation is needed to check admission and key isolation.
    start = renderer.index("#define TEXT_METRIC_SLOTS")
    end = renderer.index("static int measure(PXRenderer", start)
    source = tmp_path / "text_metrics.c"
    source.write_text("#include <stdint.h>\n#include <stdio.h>\n#include <string.h>\n"
                      + renderer[start:end] + r'''
static int check(const char *text, int pixels, int mono, int width, double height) {
    int actual_width = -1;
    double actual_height = -1;
    if (!measure_cached(text, pixels, mono, &actual_width, &actual_height) ||
        actual_width != width || actual_height != height) {
        fprintf(stderr, "Unreachable/wrong metric: %s (%d, %d)\n", text, pixels, mono);
        return 0;
    }
    return 1;
}
int main(void) {
    char text[64], longest[TEXT_METRIC_KEEP + 2];
    unsigned i, found = 0;
    int width;
    double height;
    /* Force a wrapped, exhausted probe window before testing general churn. */
    for (i = 0; found < 40 && i < 1000000; ++i) {
        snprintf(text, sizeof(text), "collision-%u", i);
        if ((text_metric_hash(text, strlen(text), 16, 0) &
             (TEXT_METRIC_SLOTS - 1)) != TEXT_METRIC_SLOTS - 1)
            continue;
        measure_store(text, 16, 0, i, found + .5);
        if (!check(text, 16, 0, i, found + .5)) return 1;
        ++found;
    }
    if (found != 40) return 2;
    for (i = 0; i < TEXT_METRIC_SLOTS * 4; ++i) {
        snprintf(text, sizeof(text), "churn-%u", i);
        measure_store(text, 17, 1, i, i * 2.0);
        if (!check(text, 17, 1, i, i * 2.0)) return 3;
    }
    memset(text_metrics, 0, sizeof(text_metrics));
    measure_store("same", 16, 0, 10, 2384002384.0);
    measure_store("same", 16, 1, 20, 30);
    measure_store("same", 17, 0, 40, 50);
    if (!check("same", 16, 0, 10, 2384002384.0) ||
        !check("same", 16, 1, 20, 30) || !check("same", 17, 0, 40, 50)) return 4;
    memset(longest, 'x', sizeof(longest) - 1);
    longest[sizeof(longest) - 1] = 0;
    measure_store(longest, 16, 0, 1, 2);
    if (measure_cached(longest, 16, 0, &width, &height)) return 5;
    return 0;
}
''', encoding="utf-8")
    executable = tmp_path / ("text_metrics.exe" if os.name == "nt" else "text_metrics")
    if Path(compiler).stem.lower() == "cl":
        command = [compiler, "/nologo", "/std:c11", "/O2", str(source), f"/Fe:{executable}"]
    else:
        command = [compiler, "-std=c11", "-O2", str(source), "-o", str(executable)]
    subprocess.run(command, cwd=tmp_path, env=environment, check=True, capture_output=True)
    subprocess.run([str(executable)], check=True, capture_output=True, timeout=15)
