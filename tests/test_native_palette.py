"""Exact ANSI contracts for native terminal palette conversion and reuse."""
import os
from pathlib import Path
import sys
import unittest

from pysual.backends._native_client import NativeClient
from pysual.backends.term_text import _sgr

ROOT = Path(__file__).resolve().parents[1]
EXE = Path(os.environ.get('PYSUAL_HOST', ROOT / 'src/pysual/bin' /
                        ('pysual-host.exe' if sys.platform == 'win32' else 'pysual-host')))


@unittest.skipUnless(EXE.is_file(), 'Build the standalone native C host first')
class NativePaletteTests(unittest.TestCase):
    def setUp(self):
        self.client = NativeClient(executable=EXE)
        self.addCleanup(self.client.close)
        self.client.request('open', backend='terminal', title='Palette regression',
                            width=8, height=16, hidden=True, headless=True)

    def assert_ansi(self, mode, colors, styles=None, backgrounds=None):
        self.client.request('configure', columns=len(colors), rows=1, color=mode)
        backgrounds = backgrounds or ['#000000'] * len(colors)
        commands = [['begin', '#000000']]
        for x, (color, background) in enumerate(zip(colors, backgrounds)):
            commands.append(['rect', [x * 8, 0, 8, 16], background, 0, '', 0])
            commands.append(['text', 'X', x * 8, 0, color, 16, False])
        self.client.request('frame', commands=commands)
        self.client.request('present')
        expected, previous = '\x1b[?2026h\x1b[1;1H', None
        for index, (color, background) in enumerate(zip(colors, backgrounds)):
            if (color, background) != previous:
                rgb = tuple(int(color[start:start + 2], 16) for start in (1, 3, 5))
                bg = tuple(int(background[start:start + 2], 16) for start in (1, 3, 5))
                expected += styles[index] if styles else _sgr(mode, rgb, bg).decode('ascii')
            expected += 'X'
            previous = (color, background)
        expected += '\x1b[0m\x1b[?2026l'
        self.assertEqual(self.client.request('ansi', diff=False), expected)
        self.assertEqual(self.client.request('ansi', diff=True), '')

    def test_repeated_rgb_colors_keep_exact_output_when_palette_changes(self):
        colors = ['#5f0073', '#ffffff', '#102030', '#808040', '#010203'] * 8
        # The same RGB keys have different results in the two palettes.
        for mode in ('16', '256', '16', '256', 'truecolor', 'none'):
            with self.subTest(mode=mode):
                for _ in range(3):
                    self.assert_ansi(mode, colors, backgrounds=colors[::-1])

    def test_colliding_rgb_colors_never_reuse_another_colors_result(self):
        # 0x000000 and 0x000262 collide in the 1,024-slot multiplicative cache;
        # their nearest colors differ in both palettes. Revisit after eviction.
        for mode in ('16', '256'):
            with self.subTest(mode=mode):
                self.assert_ansi(mode, ['#000000', '#000262'] * 16,
                                 backgrounds=['#000262', '#000000'] * 16)

    def test_palette_distance_ties_keep_the_first_palette_entry(self):
        cases = [('16', '#400000', '\x1b[30;40m'),
                 ('16', '#808040', '\x1b[33;40m'),
                 ('256', '#000000', '\x1b[38;5;0;48;5;0m'),
                 ('256', '#ffffff', '\x1b[38;5;15;48;5;0m'),
                 ('256', '#5f0073', '\x1b[38;5;53;48;5;0m')]
        for mode, color, style in cases:
            with self.subTest(mode=mode, color=color):
                self.assert_ansi(mode, [color, color], [style, style])


if __name__ == '__main__':
    unittest.main()
