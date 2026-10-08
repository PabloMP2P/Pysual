"""Proportional hit testing reuses exact host prefixes within bounded storage."""
from _ui_testcase import UIOwnerTestCase
from pysual import TextBox
from pysual.backends import _font


class Metrics:
    resource_revision = 0

    def __init__(self):
        self.calls = []

    def measure(self, text, size, mono=False):
        self.calls.append((text, size, mono))
        return _font.measure(text, size, mono)


class TextHitCacheTests(UIOwnerTestCase):
    def test_repeated_long_line_hit_reuses_host_results_and_keeps_kerning(self):
        entry, host = TextBox(), Metrics()
        line = 'AV To read proportional text. ' * 700
        x = host.measure(line, 16)[0] * .75
        first = entry._pointer_columns(None, line, x, host, 16, False)
        host.calls.clear()
        self.assertEqual(entry._pointer_columns(None, line, x, host, 16, False), first)
        self.assertEqual(host.calls, [])
        # Summing independent A and V widths would give index1 here.
        self.assertEqual(entry._pointer_columns(None, 'AV', 16.162109375, host, 16, False)[0], 2)

    def test_host_resource_size_and_text_contexts_invalidate_results(self):
        entry, host = TextBox(), Metrics()
        def hit(line='AV To', size=16, mono=False):
            entry._pointer_columns(None, line, 15, host, size, mono)
        hit()
        for action in (lambda: setattr(host, 'resource_revision', 1),
                       lambda: setattr(entry, 'password', True),
                       lambda: setattr(entry, 'tab_size', 8),
                       lambda: setattr(entry, 'text', 'new text')):
            action()
            host.calls.clear()
            hit()
            self.assertTrue(host.calls)
        for line, size, mono in (('AV To', 20, False), ('AV To', 20, True), ('new row', 20, False)):
            host.calls.clear()
            hit(line, size, mono)
            self.assertTrue(host.calls)
        host = Metrics()
        hit('new row', 20)
        self.assertTrue(host.calls)

    def test_tab_and_password_display_are_measured_as_whole_prefixes(self):
        entry, host = TextBox(), Metrics()
        line = 'AV\tTo'
        for password in (False, True):
            entry.password = password
            entry._pointer_columns(None, line, 30, host, 16, False)
            self.assertTrue(all('\t' not in text for text, _, _ in host.calls))
            if password:
                self.assertTrue(all(set(text) <= {'•'} for text, _, _ in host.calls))
            host.calls.clear()
            entry._pointer_columns(None, line, 30, host, 16, False)
            self.assertEqual(host.calls, [])

    def test_row_and_entry_counts_are_bounded(self):
        entry = TextBox()
        host = type('FastMetrics', (), {'measure': lambda self, text, size, mono=False: (len(text), size)})()
        line = 'a' * 20000
        for x in range(2200):
            entry._pointer_columns(None, line, x, host, 16, False)
        self.assertLessEqual(len(entry._hit_widths), 2048)
        context = entry._hit_context
        entry._pointer_columns(None, 'b' * 65537, 100, host, 16, False)
        self.assertIs(entry._hit_context, context)
        entry.text = 'replacement'
        self.assertEqual(len(entry._hit_widths), 0)
        self.assertIsNone(entry._hit_context)
