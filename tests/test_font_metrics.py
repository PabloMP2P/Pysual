"""Stable SVG layout metrics from the packaged DejaVu fonts."""

import unittest

from pysual.backends import _font


class FontMetricsTests(unittest.TestCase):
    def test_packaged_fonts_keep_kerning_unicode_and_multiline_measurements(self):
        # Packaged-font advances at 16 px, with canonical accents composed as SVG renders them.
        cases = (
            ("", 0, 0, 1),
            ("AVATAR", 60.140625, 57.796875, 1),
            ("café λ", 48.6328125, 57.796875, 1),
            ("A\n\nV", 10.9453125, 9.6328125, 3),
            ("e\u0301", 9.84375, 9.6328125, 1),
            ("漢字", 19.203125, 19.265625, 1),
            ("😀", 16.6796875, 9.6328125, 1),
        )
        for mono in (False, True):
            for size, row_height in ((8, 10), (12, 14), (16, 19), (24, 28), (48, 56)):
                for text, proportional, monospace, lines in cases:
                    with self.subTest(mono=mono, size=size, text=text):
                        width = (monospace if mono else proportional) * size / 16
                        self.assertEqual(_font.measure(text, size, mono),
                                         (width, row_height * lines))
                        self.assertEqual(_font.ascent(size, mono), 14.8515625 * size / 16)

    def test_long_text_measurement_matches_uncached_layout(self):
        for mono in (False, True):
            # Long values bypass the small-string cache, retaining exact metrics.
            width, height = _font.measure("x", 16, mono)
            self.assertEqual(_font.measure("x" * 5000, 16, mono), (width * 5000, height))

    def test_canonical_accents_match_composed_short_and_long_text(self):
        pairs = (("e\u0301", "é"), ("A\u030a n\u0303 u\u0308", "Å ñ ü"),
                 ("Cafe\u0301\nre\u0301sume\u0301", "Café\nrésumé"))
        for mono in (False, True):
            for decomposed, composed in pairs:
                for repeats in (1, 10, 5000):
                    with self.subTest(mono=mono, text=decomposed, repeats=repeats):
                        original = decomposed * repeats
                        self.assertEqual(_font.measure(original, 16, mono),
                                         _font.measure(composed * repeats, 16, mono))


if __name__ == "__main__":
    unittest.main()
