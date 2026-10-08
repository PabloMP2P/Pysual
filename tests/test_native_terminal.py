"""Headless real C terminal comparisons against the retained Python cell oracle.

These launch the native executable but never attach or modify a user's console.
They compare complete cells, not only printable rows or FPS.
"""
from dataclasses import asdict
import os
from pathlib import Path
import random
import subprocess
import sys
import time
import unittest
from uuid import uuid4

from pysual import Rect, Style
from pysual._png import encode_png
from pysual.image_resources import image_source
from pysual.backends._native_client import NativeClient, NativeHostError
from pysual.backends._term_cells import Cell, CellRenderer, safe_text, text_cells
from pysual.backends._term_input import TerminalInput
from pysual.backends.term_text import _ICON_GLYPHS

ROOT = Path(__file__).resolve().parents[1]
EXE = Path(os.environ.get('PYSUAL_HOST', ROOT / 'src/pysual/bin' / ('pysual-host.exe' if sys.platform == 'win32' else 'pysual-host')))


def replay(renderer, commands):
    for name, *args in commands:
        if name in {'rect', 'styled_rect', 'marker', 'focus_ring', 'gradient_rect'}:
            args[0] = Rect(*args[0])
        elif name == 'clip' and args[0] is not None:
            args[0] = Rect(*args[0])
        if name in {'styled_rect', 'marker'}:
            args[1] = Style(**args[1])
        if name == 'styled_rect':
            renderer.styled_rect(*args[:2])
        elif name == 'marker':
            renderer.marker(*args[:2], shape=args[2], checked=args[3])
        elif name == 'focus_ring':
            renderer.focus_ring(*args, monochrome=renderer.monochrome)
        elif name == 'icon':
            renderer.icon(_ICON_GLYPHS[args[0]], *args[1:])
        elif name in {'lines', 'segments'}:
            step = 2 if name == 'segments' else 1
            for first, last in zip(args[0][::step], args[0][1::step]):
                renderer.line(*first, *last, *args[1:])
        elif name == 'image':
            renderer.image(args[0], Rect(*args[1]), tint=args[2], fit=args[3])
        elif name == 'image_nine':
            renderer.image_nine(args[0], Rect(*args[1]), args[2], tint=args[3])
        else:
            getattr(renderer, name)(*args)


@unittest.skipUnless(EXE.is_file(), 'Build the standalone native C host first')
class NativeTerminalTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.client = NativeClient(self.events.append, executable=EXE, timeout=5)
        self.addCleanup(self.client.close)
        self.info = self.client.request('open', backend='terminal', title='Headless cell oracle',
            width=320, height=192, hidden=True, headless=True, color='truecolor', scale=1)
        self.cols, self.rows = 40, 12

    def test_capture_preserves_unicode_directory_and_filename(self):
        directory = ROOT / 'work' / 'terminal-captures' / ('résumé-' + uuid4().hex)
        directory.mkdir(parents=True)
        output = directory / 'capturé.ansi'
        self.addCleanup(directory.rmdir)
        self.addCleanup(output.unlink, missing_ok=True)
        self.client.request('frame', commands=[['begin', '#123456'],
            ['text', 'Captured café', 0, 0, '#fff', 16, False]])
        expected = self.client.request('ansi', diff=False)
        self.client.request('capture', path=str(output))
        self.assertTrue(output.is_file())
        self.assertEqual(output.read_text(encoding='utf-8'), expected)

    def test_disconnected_stroke_batches_match_terminal_line_semantics(self):
        points = [[8, 16], [240, 160], [240, 16], [8, 160]]
        for color in ('#fedcba', '#fedcba80'):
            with self.subTest(color=color):
                self.compare([['begin', '#123456'], ['clip', [24, 16, 160, 128]],
                              ['segments', points, color, 3]])
        for points in ([[1, 2]], [[1, 2], [3]]):
            with self.assertRaises(NativeHostError):
                self.client.request('frame', commands=[['begin', '#fff'],
                    ['segments', points, '#abc', 1]])

    def compare(self, commands, *, mono=False):
        self.client.request('frame', commands=commands)
        return self.compare_snapshot(commands, mono=mono)

    def compare_snapshot(self, commands, *, mono=False):
        reference = CellRenderer(self.cols, self.rows, monochrome=mono)
        self.addCleanup(reference.close)
        replay(reference, commands)
        snapshot = self.client.request('snapshot')
        actual = [Cell(c['text'], tuple(c['foreground']), tuple(c['background']), c['width'], c['underline']) for c in snapshot['cells']]
        self.assertEqual((snapshot['columns'], snapshot['rows']), (self.cols, self.rows))
        for i, (found, expected) in enumerate(zip(actual, reference.cells)):
            self.assertEqual(found, expected, f'cell x={i % self.cols}, y={i // self.cols}')
        self.assertEqual(len(actual), len(reference.cells))
        self.assertEqual(snapshot['rows_text'], list(reference.snapshot_rows()))
        return snapshot

    def compare_segments(self, segments, background='#123456', *, mono=False):
        commands = [['begin', background]]
        for segment in segments:
            commands += [['clip', None], *segment['commands']]
        return self.compare_snapshot(commands, mono=mono)

    @staticmethod
    def segment(identifier, commands, bounds=(0, 0, 320, 192)):
        return dict(id=identifier, bounds=list(bounds), commands=commands)

    def test_retained_patches_match_full_cells_through_edits_order_and_removal(self):
        style = asdict(Style(fill='#234567a0', fill_end='#fbaa8860', border='#aaffdd', radius=8))
        image = image_source(encode_png(2, 1, bytes([255, 0, 0, 160, 0, 120, 255, 255])))
        surface = self.segment('surface', [['styled_rect', [8, 0, 150, 80], style, None],
            ['text', '中 wide é', 0, 16, '#fff', 16, False], ['caret', 16, 16, 16, '#0f0']])
        clipped = self.segment('clipped', [['clip', [40, 0, 72, 64]],
            ['gradient_rect', [0, 0, 144, 64], '#f005', '#09f8', 'horizontal', 4, 0]])
        # Caller bounds are hints; even incorrect narrow bounds cannot clip ink.
        overlay = self.segment('overlay', [['text', 'Visible beyond the prior clip', 0, 96, '#fed', 16, False],
            ['image', image, [136, 16, 48, 48], '#fffa', 'stretch'],
            ['marker', [184, 16, 24, 24], style, 'circle', True, None]], bounds=(0, 0, 1, 1))
        if not self.info.get('images', True):
            overlay['commands'].pop(1)
        segments = [surface, clipped, overlay]
        self.client.request('patch', upsert=segments, order=[s['id'] for s in segments], background='#123456')
        self.compare_segments(segments)
        self.assertTrue(self.client.request('info')['scene_patches'])
        self.client.request('stats', reset=True)
        surface['commands'][1][1] = 'Changed 中文'
        self.client.request('patch', upsert=[surface])
        self.compare_segments(segments)
        stats = self.client.request('stats')
        self.assertEqual(stats['cell_frames_composed'], 1)
        self.assertEqual(stats['segments_updated'], 1)
        self.assertEqual(stats['commands_touched'], len(surface['commands']))
        self.assertEqual(stats['commands_replayed'], sum(len(s['commands']) for s in segments))
        segments = [overlay, surface, clipped]
        self.client.request('patch', order=[s['id'] for s in segments])
        self.compare_segments(segments)
        segments = [overlay, surface]
        self.client.request('patch', remove=['clipped'], order=[s['id'] for s in segments], background='#445566')
        self.compare_segments(segments, '#445566')
        self.client.request('patch', remove=['overlay', 'surface'], order=[])
        self.compare_segments([], '#445566')
        self.assertEqual(self.client.request('info')['retained_scene_bytes'], 0)

    def test_patch_rejection_is_atomic_and_full_frame_replaces_segments(self):
        kept = self.segment('kept', [['text', 'Kept 中文', 0, 0, '#fff', 16, False]])
        added = self.segment('added', [['rect', [0, 0, 80, 48], '#f00', 0, '', 0]])
        self.client.request('patch', upsert=[kept], order=['kept'], background='#123456')
        before = self.compare_segments([kept])
        invalid = [dict(upsert=[added, self.segment('bad', [['bogus']])]),
            dict(upsert=[added], order=['kept', 'missing']), dict(upsert=[added, added]),
            dict(remove=['kept'], upsert=[kept]), dict(order=['kept', 'kept']),
            dict(upsert=[self.segment('reset', [['begin', '#f00']])]),
            dict(upsert=[self.segment('', [])]), dict(upsert=[self.segment('negative', [], (0, 0, -1, 5))]),
            dict(background='#invalid'), dict(upsert=[self.segment('late-bad', [['text', 'Paint then fail', 0, 0, '#fff', 16, False], ['rect', [0, 0, 10, 10], '#badcolor', 0, '', 0]])])]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(NativeHostError):
                self.client.request('patch', **change)
            self.assertEqual(self.client.request('snapshot'), before)
            self.assertEqual(self.client.request('info')['segment_count'], 1)
        self.client.request('patch', upsert=[added])
        self.compare_segments([kept, added])
        self.compare([['begin', '#000'], ['text', 'Full frame', 0, 0, '#fff', 16, False]])
        self.assertEqual(self.client.request('info')['segment_count'], 0)
        self.client.request('patch', upsert=[added])
        self.compare_segments([added], '#000000')

    def test_cached_cells_avoid_replay_and_encoding_until_scene_changes(self):
        commands = [['begin', '#123456'], ['text', 'One composition 中', 0, 0, '#fff', 16, False]]
        self.compare(commands)
        stats = self.client.request('stats')
        self.assertEqual(stats['cell_frames_composed'], 1)  # commit + immediate draw
        self.assertEqual(stats['commands_replayed'], len(commands))
        self.client.request('stats', reset=True)
        for _ in range(5):
            self.client.request('present')
        self.client.request('frame', commands=commands)  # identical submissions are cached too
        stats = self.client.request('stats')
        self.assertEqual(stats['cell_frames_composed'], 0)
        self.assertEqual(stats['commands_replayed'], 0)
        self.assertEqual(stats['ansi_frames_encoded'], 0)
        self.assertEqual(stats['ansi_bytes_encoded'], 0)
        self.assertEqual(stats['terminal_bytes_written'], 0)  # headless
        self.assertGreaterEqual(stats['cell_cache_hits'], 6)
        commands[1][1] = 'Changed cells 中'
        self.compare(commands)
        stats = self.client.request('stats')
        self.assertEqual(stats['cell_frames_composed'], 1)
        self.assertEqual(stats['ansi_frames_encoded'], 1)
        self.assertGreater(stats['ansi_bytes_encoded'], 0)
        # Diagnostics and capture encoding do not masquerade as terminal output.
        self.assertTrue(self.client.request('ansi', diff=False))
        self.assertEqual(self.client.request('stats')['ansi_frames_encoded'], 1)

    def test_retained_command_budget_rejects_large_patch_without_losing_scene(self):
        kept = self.segment('kept', [['text', 'Retained', 0, 0, '#fff', 16, False]])
        self.client.request('patch', upsert=[kept], background='#123456')
        before = self.compare_segments([kept])
        info = self.client.request('info')
        # The compact wire payload is below 16 MiB, while the parsed command
        # records exceed the 64 MiB retained-command budget (11 nodes/rect).
        commands = [['rect', [0, 0, 8, 16], '#fff', 0, '', 0]] * 96_000
        with self.assertRaisesRegex(NativeHostError, '64 MiB'):
            self.client.request('patch', upsert=[self.segment('too-large', commands)])
        self.assertEqual(self.client.request('snapshot'), before)
        self.assertEqual(self.client.request('info')['retained_scene_bytes'], info['retained_scene_bytes'])
        kept['commands'][0][1] = 'Still usable'
        self.client.request('patch', upsert=[kept])
        self.compare_segments([kept])

    def test_segment_cache_rebuilds_for_resize_and_monochrome_without_resubmission(self):
        segment = self.segment('outside', [['text', 'Outside 中文', 320, 0, '#fed', 16, False],
            ['focus_ring', [0, 16, 80, 48], '#0ff', 4]], bounds=(0, 0, 400, 64))
        self.client.request('patch', upsert=[segment], background='#123456')
        self.client.request('stats', reset=True)
        self.client.request('patch', upsert=[segment])
        identical = self.client.request('stats')
        self.assertEqual(identical['unchanged_segments'], 1)
        self.assertEqual(identical['cell_frames_composed'], 0)
        self.assertEqual(identical['ansi_frames_encoded'], 0)
        self.client.request('configure', columns=60, rows=14)
        self.cols, self.rows = 60, 14
        self.client.request('present')
        self.compare_segments([segment])
        self.assertIn('Outside 中文', self.client.request('snapshot')['rows_text'][0])
        self.client.request('configure', color='none')
        self.client.request('present')
        self.compare_segments([segment], mono=True)
        self.client.request('configure', color='truecolor')
        self.client.request('present')
        self.compare_segments([segment])
        self.assertEqual(self.client.request('stats')['cell_frames_composed'], 3)

    def test_long_polyline_matches_cells_and_rejects_invalid_late_point(self):
        # cJSON arrays are linked lists: traversal must not repeatedly index
        # every earlier point. This sizeable path also checks merged junctions.
        points = [[(i * 13) % 320, (i * 7) % 192] for i in range(1200)]
        commands = [['begin', '#123456'], ['lines', points, '#7898', 1]]
        before = self.compare(commands)
        points.append([1, 'invalid'])
        with self.assertRaises(NativeHostError):
            self.client.request('frame', commands=commands)
        self.assertEqual(self.client.request('snapshot'), before)

    def test_fixed_cell_viewport_and_safe_unicode_metrics(self):
        self.assertEqual((self.info['width'], self.info['height'], self.info['scale']), (320, 192, 1))
        reference = CellRenderer(40, 12)
        for text in ('', 'Hello', 'a\tb\ncafé λ', '中😀x', '👨‍👩‍👧‍👦🇪🇸1️⃣', '\u0301\u0308', 'a\x1b[31m\x07b\u202ec', '\u0915\u094d\u0937', 'a' + '\u0301' * 100):
            with self.subTest(text=text):
                self.assertEqual(self.client.request('measure', text=text, size=77, mono=False), list(reference.measure(text, 77)))
        self.compare([['begin', '#123456'], ['text', '中é 😀 👨‍👩‍👧‍👦 🇪🇸\n\u0301\tTab', 0, 0, '#abcdef80', 16, False]])

    def test_width_changes_between_unicode_versions_keep_native_python_cell_parity(self):
        symbols = '\u2630\U0001fae9\U0001fa89\U0001fa8f\U0001f680'
        for symbol, width in zip(symbols, (8, 8, 8, 8, 16)):
            with self.subTest(code=hex(ord(symbol))):
                self.assertEqual(self.client.request('measure', text=symbol, size=16), [width, 16])
        self.compare([['begin', '#123456'], ['text', symbols + ' follows', 0, 0, '#fff', 16, False]])

    def test_large_portable_png_source_is_accepted_in_frames_and_patches(self):
        if not self.info.get('images', True):
            self.skipTest('This host was built without PNG decoding')
        # Incompressible RGBA pixels exceed the old 8 MiB text/URI limit while
        # staying below the public 8 MiB PNG and 32 MiB decoded-image limits.
        from pysual.image_resources import MAX_IMAGE_BYTES
        png = encode_png(1600, 1100, random.Random(7).randbytes(1600 * 1100 * 4))
        self.assertLessEqual(len(png), MAX_IMAGE_BYTES)
        source = image_source(png)
        self.assertGreater(len(source), 8 * 1024 * 1024)
        for name, extra in (('image', ['#fff', 'contain']), ('image_nine', [[1, 1, 1, 1], '#fff'])):
            command = [name, source, [0, 0, 80, 64], *extra]
            with self.subTest(command=name, scene='frame'):
                self.client.request('frame', commands=[['begin', '#123456'], command])
                before = self.client.request('snapshot')
                self.assertIn('▀', ''.join(before['rows_text']))
                cached = self.client.request('info')
                self.assertEqual(cached['image_count'], 1)
                self.assertGreaterEqual(cached['image_bytes'], 1600 * 1100 * 4 + len(source))
                self.assertLessEqual(cached['image_bytes'], 32 * 1024 * 1024)
            with self.subTest(command=name, scene='patch'):
                self.client.request('patch', upsert=[self.segment('image', [command])],
                                    order=['image'], background='#123456')
                self.assertEqual(self.client.request('snapshot'), before)
                reused = self.client.request('info')
                self.assertEqual((reused['image_count'], reused['image_bytes']),
                                 (cached['image_count'], cached['image_bytes']))
        self.assertFalse(any(event.get('kind') == 'resource_error' for event in self.events))

    def test_valid_image_larger_than_cache_budget_is_drawn_without_retaining(self):
        if not self.info.get('images', True):
            self.skipTest('This host was built without PNG decoding')
        # The decoded pixels alone exactly fill the cache; source/entry storage
        # puts this valid image over its admission budget.
        source = image_source(encode_png(4096, 2048, b'\xff\0\0\xff' * (4096 * 2048)))
        command = ['image', source, [0, 0, 16, 16], '#fff', 'stretch']
        for background in ('#123456', '#654321'):
            self.client.request('frame', commands=[['begin', background], command])
            snapshot = self.client.request('snapshot')
            self.assertEqual(snapshot['cells'][0]['foreground'], [255, 0, 0])
            info = self.client.request('info')
            self.assertEqual((info['image_bytes'], info['image_count']), (0, 0))

    def test_image_source_allowance_does_not_widen_text_or_encoded_png_budget(self):
        from base64 import b64encode
        from pysual.image_resources import MAX_IMAGE_BYTES
        self.client.request('frame', commands=[['begin', '#123456'],
                            ['text', 'Kept', 0, 0, '#fff', 16, False]])
        before = self.client.request('snapshot')
        oversized = 'data:image/png;base64,' + b64encode(bytes(MAX_IMAGE_BYTES + 1)).decode('ascii')
        commands = [
            ['text', 'x' * (8 * 1024 * 1024 + 1), 0, 0, '#fff', 16, False],
            ['image', oversized, [0, 0, 80, 64], '#fff', 'contain'],
            ['image_nine', oversized + 'AAAA', [0, 0, 80, 64], [1, 1, 1, 1], '#fff'],
        ]
        for command in commands:
            for operation, arguments in (
                ('frame', {'commands': [['begin', '#000'], command]}),
                ('patch', {'upsert': [self.segment('bad', [command])], 'order': ['bad']}),
            ):
                with self.subTest(command=command[0], operation=operation):
                    with self.assertRaises(NativeHostError):
                        self.client.request(operation, **arguments)
                    self.assertEqual(self.client.request('snapshot'), before)

    def test_unicode_grapheme_fixture_cells(self):
        path = ROOT / 'tests/fixtures/unicode/GraphemeBreakTest-17.0.0.txt'
        fixtures = []
        for line in path.read_text(encoding='utf8').splitlines():
            body = line.split('#')[0].strip()
            if body:
                fixtures.append(''.join(chr(int(x, 16)) for x in body.split() if x not in ('÷', '×')))
        # All official cases pass through sanitizer and the native cluster iterator.
        # Pack one case per row; positioning makes cluster boundaries observable.
        for start in range(0, len(fixtures), self.rows):
            commands = [['begin', '#000']]
            for row, text in enumerate(fixtures[start:start+self.rows]):
                text = safe_text(text).replace('\n', '').replace('\t', '')
                commands.append(['text', text, 0, row * 16, '#fff', 16, False])
            with self.subTest(fixture=start):
                self.compare(commands)

    def test_clipping_wide_erasure_tint_and_decorations(self):
        commands = [['begin', '#112233'], ['text', '中文A label', 0, 0, '#fff', 16, False],
            ['rect', [0, 0, 40, 16], '#f008', 0, '', 0],
            ['clip', [8, 0, 56, 48]], ['rect', [8, 0, 8, 16], '#080', 0, '', 0],
            ['text', '中', 0, 16, '#fff', 16, False], ['clip', None],
            ['line', -200, 40, 400, 40, '#abc', 1], ['line', 52, 0, 52, 100, '#abc', 1],
            ['lines', [[0, 90], [70, 160], [300, 50]], '#69c8', 2],
            ['text', 'Borders preserve text', 8, 64, '#fed', 16, True],
            ['rect', [0, 64, 240, 16], '', 0, '#fff', 1], ['caret', 16, 64, 16, '#0f0']]
        self.compare(commands)

    def test_semantic_surfaces_markers_icons_and_focus(self):
        style = asdict(Style(fill='#12345680', fill_end='#badaff', gradient_axis='horizontal',
            border='#ff8800', border_end='#00ffff', border_width=2, radius=9,
            glow='#ffffff', glow_width=8, shadow='#00000088', shadow_blur=6))
        commands = [['begin', '#191a20']]
        for row, height in enumerate((5, 16, 32, 48)):
            commands += [['styled_rect', [8, row*40, 110, height], style, None],
                ['marker', [130, row*40, 20, 20], style, 'square', bool(row % 2), None],
                ['marker', [165, row*40, 20, 20], style, 'circle', bool(row % 2), None]]
        commands += [['focus_ring', [8, 40, 110, 32], '#0ff', 4]]
        for i, name in enumerate(_ICON_GLYPHS):
            commands.append(['icon', name, (i % 10)*30, 164+(i//10)*4, 24, '#fff'])
        self.compare(commands)
        self.client.request('configure', color='none')
        self.compare(commands, mono=True)

    def test_fractional_geometry_and_gradient_oracles(self):
        randomizer = random.Random(4312)
        for case in range(30):
            commands = [['begin', '#123456']]
            for _ in range(10):
                rect = [randomizer.uniform(-50, 350), randomizer.uniform(-50, 210), randomizer.uniform(0, 250), randomizer.uniform(0, 160)]
                commands += [['gradient_rect', rect, '#f258', '#29c7', randomizer.choice(['horizontal', 'vertical']), randomizer.randrange(12), randomizer.randrange(3)],
                    ['rect', rect, '', randomizer.randrange(8), '#fff8', 1]]
            with self.subTest(case=case):
                self.compare(commands)

    def test_png_tint_fit_alpha_nineslice_and_monochrome(self):
        if not self.info.get('images', True):
            self.skipTest('This terminal-only helper was built without image decoding')
        pixels = bytes([255,0,0,255, 0,255,0,128, 0,0,255,255, 255,255,0,0] * 4)
        source = image_source(encode_png(4, 4, pixels))
        commands = [['begin', '#14283c'], ['text', '中文 image', 0, 0, '#fff', 16, False]]
        for i, fit in enumerate(('stretch', 'contain', 'cover')):
            commands.append(['image', source, [i*80, 0, 63, 47], '#aaffcc90', fit])
        commands += [['image_nine', source, [8, 65, 90, 70], [1,1,1,1], '#fff'],
            ['rect', [8, 65, 60, 40], '#1239', 0, '', 0], ['clip', [136, 64, 80, 48]],
            ['image', source, [129, 57, 100, 72], '#fff', 'stretch']]
        self.compare(commands)
        self.client.request('configure', color='none')
        self.compare(commands, mono=True)

    def test_invalid_scene_is_atomic_and_recoverable(self):
        valid = [['begin', '#123'], ['text', 'Kept', 0, 0, '#fff', 16, False]]
        before = self.compare(valid)
        for invalid in ([['begin', '#f00'], ['bogus']], [['rect', [0,0,4,4], '#xyz', 0, '', 0]], [['marker', [0,0,8,16], {}, 'triangle', True]]):
            with self.subTest(invalid=invalid), self.assertRaises(NativeHostError):
                self.client.request('frame', commands=invalid)
            self.assertEqual(self.client.request('snapshot'), before)
        self.compare(valid)

    def test_ansi_unchanged_frames_modes_and_control_sanitization(self):
        commands = [['begin', '#123456'], ['text', 'A\x1b[99mB\x07中', 0, 0, '#abcdef', 16, False], ['caret', 0, 0, 16, '#fff']]
        self.compare(commands)
        self.assertEqual(self.client.request('ansi', diff=True), '')
        for mode, expected in [('truecolor', '\x1b[38;2;'), ('256', '\x1b[38;5;'), ('16', '\x1b[3'), ('none', '\x1b[4m')]:
            self.client.request('configure', color=mode)
            self.client.request('frame', commands=commands)
            encoded = self.client.request('ansi', diff=False)
            self.assertIn(expected, encoded)
            self.assertNotIn('\x1b[99m', encoded)
            self.assertNotIn('\x07', encoded)
            self.assertEqual(self.client.request('ansi', diff=True), '')

    def test_resize_invalidates_viewport_and_repaints_retained_scene(self):
        self.client.request('configure', columns=17, rows=4)
        self.cols, self.rows = 17, 4
        self.compare([['begin', '#124'], ['text', 'Hello 中', 0, 0, '#fff', 16, False]])
        self.assertEqual(self.client.request('info')['width'], 136)
        self.assertEqual(self.client.request('set_size', width=900, height=900)['width'], 136)
        with self.assertRaises(NativeHostError):
            self.client.request('configure', columns=0)
        self.assertEqual(self.client.request('info')['width'], 136)

    def test_fragmented_utf8_keys_mouse_focus_and_bracketed_paste(self):
        oracle = TerminalInput()
        pieces = [b'A\t', 'é'.encode()[:1], 'é'.encode()[1:], b'\x1b[1;5A', b'\x1b[Z',
            b'\x1b[<0;3;2M', b'\x1b[<32;4;3M', b'\x1b[<0;4;3m', b'\x1b[<65;2;2M',
            b'\x1b[I\x1b[O', 'ÀΩİ'.encode(), b'\x1b[223;2:1u', b'\x1b[97;2:1u\x1b[97;2:3u',
            b'\x1b[200~one\r', b'\ntwo', ' 中'.encode(), b'\x1b[201~', b'\x1b[200~bad \xff\xe2\x82x \x00 end\x1b[201~', b'\x11']
        expected = []
        for part in pieces:
            expected.extend(oracle.feed(part))
            self.client.request('feed_input', hex=part.hex())
        deadline = time.monotonic() + 2
        while len(self.events) < len(expected) and time.monotonic() < deadline:
            time.sleep(.005)
        # Match the public Input defaults as well as payloads.
        from pysual.host import Input
        actual = [Input(**e) for e in self.events]
        self.assertEqual(actual, expected)
        self.assertTrue(self.client.is_alive)  # Ctrl-Q is a vetoable request.

    def test_input_overflow_reports_error_instead_of_silent_event_loss(self):
        self.client.request('feed_input', data='x' * 2000)
        deadline = time.monotonic() + 2
        while not self.events and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0]['kind'], 'error')
        self.assertIn('4096', self.events[0]['text'])

    def test_continuous_c_replay_does_not_require_python_frame_requests(self):
        self.compare([['begin', '#123'], ['text', 'Native cells', 0, 0, '#fff', 16, False]])
        self.client.request('configure', continuous=True, fps_limit=120)
        first = self.client.request('stats')
        sent = self.client.diagnostics['requests_sent']
        time.sleep(.06)
        self.assertEqual(self.client.diagnostics['requests_sent'], sent)
        last = self.client.request('stats')
        self.assertGreater(last['frames'], first['frames'])
        self.assertEqual(last['scene_updates'], first['scene_updates'])


@unittest.skipUnless(sys.platform == 'win32' and EXE.is_file() and os.environ.get('PYSUAL_TEST_TERMINAL_IO') == '1',
                     'Opt in to an isolated hidden Windows test console')
class NativeTerminalSessionTests(unittest.TestCase):
    def test_live_console_io_and_mode_restoration(self):
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--isolated-console-probe'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf8', timeout=12,
            creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn('isolated console passed', result.stdout)


def isolated_console_probe():
    # This process owns a newly-created, hidden console. The user's interactive
    # terminal is neither attached nor altered by this test.
    import ctypes as c
    from ctypes import wintypes as w
    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE)
    kernel.CreateFileW.restype = w.HANDLE
    kernel.GetConsoleMode.argtypes = (w.HANDLE, c.POINTER(w.DWORD))
    kernel.WriteConsoleInputW.argtypes = (w.HANDLE, w.LPVOID, w.DWORD, c.POINTER(w.DWORD))
    kernel.CloseHandle.argtypes = (w.HANDLE,)
    handles = [kernel.CreateFileW(name, 0xc0000000, 3, None, 3, 0, None) for name in ('CONIN$', 'CONOUT$')]
    def modes():
        values = []
        for handle in handles:
            mode = w.DWORD()
            if not kernel.GetConsoleMode(handle, c.byref(mode)):
                raise c.WinError(c.get_last_error())
            values.append(mode.value)
        return (*values, kernel.GetConsoleOutputCP())
    before = modes()
    events = []
    client = NativeClient(events.append, executable=EXE, timeout=4)
    try:
        opened = client.request('open', backend='terminal', title='Isolated terminal test', hidden=False,
                                headless=False, color='truecolor', width=320, height=192)
        assert opened['scene_patches'] is True
        status = dict(id='status', bounds=[0, 0, 320, 16],
                      commands=[['text', 'Hello native console', 0, 0, '#fff', 16, False]])
        neighbor = dict(id='neighbor', bounds=[0, 16, 320, 16],
                        commands=[['text', 'Retained neighbor', 0, 16, '#fff', 16, False]])
        client.request('patch', background='#123456', upsert=[status, neighbor],
                       order=['status', 'neighbor'])
        # Output must continue draining even with no automatic presentations.
        client.request('configure', continuous=False)
        class Coord(c.Structure):
            _fields_ = [('x', w.SHORT), ('y', w.SHORT)]
        kernel.ReadConsoleOutputCharacterW.argtypes = (w.HANDLE, w.LPWSTR, w.DWORD, Coord, c.POINTER(w.DWORD))

        def console_row(row):
            # Reopen CONOUT$ to read the native host's active alternate buffer.
            output = kernel.CreateFileW('CONOUT$', 0xc0000000, 3, None, 3, 0, None)
            try:
                columns = int(opened['columns'])
                chars, read = c.create_unicode_buffer(columns + 1), w.DWORD()
                if not kernel.ReadConsoleOutputCharacterW(output, chars, columns, Coord(0, row), c.byref(read)):
                    raise c.WinError(c.get_last_error())
                return chars.value.rstrip()
            finally:
                kernel.CloseHandle(output)

        def wait_for_rows(first, second):
            deadline = time.monotonic() + 2
            while True:
                visible = (console_row(0), console_row(1))
                stats = client.request('stats')
                if visible == (first, second) and stats['terminal_output_pending_bytes'] == 0:
                    return stats
                if time.monotonic() >= deadline:
                    raise AssertionError((visible, first, second, stats))
                time.sleep(.01)

        wait_for_rows('Hello native console', 'Retained neighbor')
        # Do not wait for console output between edits: only the latest scene
        # needs to follow a pending ANSI frame, while the other segment persists.
        for revision in range(32):
            status['commands'][0][1] = f'Patch revision {revision:02d}'
            client.request('patch', upsert=[status])
        stats = wait_for_rows('Patch revision 31', 'Retained neighbor')
        assert stats['terminal_bytes_written'] > 0
        assert stats['ansi_frames_encoded'] > 0
        assert stats['ansi_bytes_encoded'] > 0
        assert client.request('snapshot')['rows_text'][0].rstrip() == 'Patch revision 31'
        client.request('patch', remove=['status'], order=['neighbor'])
        wait_for_rows('', 'Retained neighbor')
        class Key(c.Structure):
            _fields_ = [('down', w.BOOL), ('repeat', w.WORD), ('virtual', w.WORD),
                        ('scan', w.WORD), ('char', w.WCHAR), ('state', w.DWORD)]
        class Payload(c.Union):
            _fields_ = [('key', Key), ('padding', c.c_byte * 16)]
        class Record(c.Structure):
            _fields_ = [('type', w.WORD), ('data', Payload)]
        records = (Record * 2)()
        for i in range(2):
            records[i].type = 1
            records[i].data.key = Key(i == 0, 1, 65, 30, 'A', 0)
        count = w.DWORD()
        if not kernel.WriteConsoleInputW(handles[0], c.byref(records), 2, c.byref(count)):
            raise c.WinError(c.get_last_error())
        deadline = time.monotonic() + 2
        while not any(e.get('kind') == 'text' and e.get('text') == 'A' for e in events) and time.monotonic() < deadline:
            time.sleep(.01)
        assert any(e.get('kind') == 'text' and e.get('text') == 'A' for e in events), events
        assert client.request('snapshot')['rows_text'][0].strip() == ''
        assert client.request('snapshot')['rows_text'][1].rstrip() == 'Retained neighbor'
        assert client.request('stats')['frames'] >= 1
    finally:
        client.close()
    assert modes() == before, (before, modes())
    assert client.process.poll() is not None
    for handle in handles:
        kernel.CloseHandle(handle)
    print('isolated console passed')


if __name__ == '__main__':
    if '--isolated-console-probe' in sys.argv:
        isolated_console_probe()
    else:
        unittest.main()
