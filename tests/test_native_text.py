"""Bounded text surfaces preserve whole-line shaping at native pixel scales."""

from pathlib import Path
import unittest
from uuid import uuid4

from pysual import App, Label, Rect
from pysual.backends._native_client import native_executable
from pysual.backends.native import NativeHost
from test_native_window import bmp_pixels

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(native_executable().is_file(), "Build the native helper")
class NativeTextFragmentTests(unittest.TestCase):
    def test_long_zero_width_label_opens_and_closes_normally(self):
        for length in (9000, 16000):
            with self.subTest(length=length):
                app = App(width=75, height=100, reduce_motion=True)
                Label(parent=app, text="\u200b" * length + "abcde",
                      font_size=15, width=35, height=30)
                host = NativeHost(hidden=True, vsync=False)
                opened = False
                try:
                    app.run(backend=host)
                    opened = True
                    self.assertTrue(app.is_open)
                    self.assertGreaterEqual(app.frame_count, 1)
                    app.close()
                    self.assertEqual(app.wait(2).reason, "closed")
                finally:
                    app.destroy()
                    if opened:
                        app.wait(2)

    def test_visible_pixels_match_whole_line_at_left_middle_and_end(self):
        directory = ROOT / "work" / "text-fragments" / uuid4().hex
        directory.mkdir(parents=True)
        output = directory / "frame.bmp"
        self.addCleanup(directory.rmdir)
        self.addCleanup(output.unlink, missing_ok=True)
        for scale in (1, 1.5):
            host = NativeHost(hidden=True, vsync=False)
            try:
                # The reference has room for one complete texture. The first
                # render clips to an editor-sized viewport and uses a crop.
                host.open("Text crop pixels", 2800, 90, False, scale)
                for mono, pattern in ((False, "AV office Wi "),
                                      (False, "café e\u0301\tWi "),
                                      (True, "é e\u0301\tWi ")):
                    for fraction in (0, .5, 1):
                        with self.subTest(mono=mono, scale=scale, scroll=fraction):
                            # A unique suffix avoids warming the whole-line
                            # texture before testing the next crop position.
                            source = (pattern * 20 + str(fraction)).expandtabs(4)
                            width = host.measure(source, 15, mono)[0]
                            offset = max(0, width - 430) * fraction
                            images = []
                            for clip in (Rect(0, 17, 480, 40), Rect(0, 0, 480, 90), None):
                                host.begin("#123456")
                                host.clip(clip)
                                host.text(source, 12 - offset, 12, "#ffffff", 15, mono)
                                host.present()
                                host.capture(output)
                                images.append([row[:round(480 * scale)] for row in bmp_pixels(output)])
                            self.assertTrue(images[1] == images[2],
                                            "Clipped glyph pixels differ from full-line shaping")
                            top, bottom = round(17 * scale), round(57 * scale)
                            self.assertTrue(images[0][top:bottom] == images[2][top:bottom],
                                            "Vertical text crop differs from full-line shaping")
            finally:
                host.close()
