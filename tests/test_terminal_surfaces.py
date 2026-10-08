"""Cached terminal paint must produce the same complete cells as direct paint."""

import unittest

from _terminal import MemoryTerminal
from _ui_testcase import UIOwnerTestCase

from pysual import App, Button, Container, Label, Rect, Style, TextBox
from pysual._png import encode_png
from pysual.backends._term_cells import CellRenderer
from pysual.backends.term_text import TermTextHost
from pysual.backends.terminal import TerminalHost
from pysual.image_resources import image_source
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.render_cache import RenderCache


class CellSurfaceTests(unittest.TestCase):
    def renderer(self):
        renderer = CellRenderer(40, 12)
        self.addCleanup(renderer.close)
        return renderer

    @staticmethod
    def backdrop(renderer):
        renderer.begin("#112233")
        renderer.rect(Rect(16, 16, 280, 144), "#884400")
        renderer.text("underlying text 好", 32, 64, "#eeddaa", 16)

    def assert_cached_paint(self, draw, bounds, backdrop=None):
        direct, cached = self.renderer(), self.renderer()
        backdrop = backdrop or self.backdrop
        backdrop(direct)
        direct.clip(bounds)
        draw(direct)
        backdrop(cached)
        surface = cached.surface_create(bounds)
        self.assertFalse(cached.surface_matches(surface))
        cached.surface_begin(surface)
        cached.clip(bounds)
        draw(cached)
        cached.surface_end()
        cached.clip(None)
        self.assertTrue(cached.surface_matches(surface))
        cached.surface_blit(surface)
        self.assertEqual(cached.cells, direct.cells)
        self.assertEqual(cached._ink, direct._ink)
        # Reusing a surface in a new frame must also preserve its backdrop.
        backdrop(cached)
        self.assertTrue(cached.surface_matches(surface))
        cached.surface_blit(surface)
        self.assertEqual(cached.cells, direct.cells)
        self.assertEqual(cached._ink, direct._ink)
        return cached, surface

    def test_all_primitives_keep_global_coordinates_inside_an_offset_surface(self):
        source = image_source(encode_png(8, 8, bytes(
            channel for y in range(8) for x in range(8)
            for channel in (x * 31, y * 31, 160, 180)
        )))
        style = Style(fill="#3377aa88", fill_end="#aa7733cc", border="#eeeeee",
                      border_width=1, foreground="#ffffff", gradient_axis="horizontal")
        area = Rect(48, 48, 128, 80)
        draws = {
            "text": lambda r: r.text("Hello 好\ne\u0301\tmore", 53, 59, "#eee9", 16),
            "caret": lambda r: r.caret(56, 64, 16, "#eee"),
            "line": lambda r: r.line(16, 16, 280, 150, "#eeffaa", 1),
            "horizontal line": lambda r: r.line(24, 96, 240, 96, "#eee", 1),
            "rect": lambda r: r.rect(area, "#aa773388", 4, "#eee", 1),
            "thin rect": lambda r: r.rect(Rect(48, 96, 128, 2), "#eee"),
            "styled rect": lambda r: r.styled_rect(area, style),
            "thin track": lambda r: r.styled_rect(Rect(48, 98, 128, 3), style),
            "square marker": lambda r: r.marker(Rect(64, 96, 24, 24), style, checked=True),
            "circle marker": lambda r: r.marker(Rect(96, 96, 24, 24), style, shape="circle"),
            "icon": lambda r: r.icon("✓", 64, 96, 24, "#fff"),
            "focus": lambda r: r.focus_ring(area, "#eee", 4, monochrome=True),
            "gradient": lambda r: r.gradient_rect(area, "#8008", "#0088", "horizontal", 0),
            "image": lambda r: r.image(source, area, tint="#ffccffbb"),
            "contain image": lambda r: r.image(source, area, fit="contain"),
            "cover image": lambda r: r.image(source, area, fit="cover"),
            "nine-slice": lambda r: r.image_nine(source, area, (2, 2, 2, 2)),
            "placeholder": lambda r: r.image_placeholder(area),
        }
        for name, draw in draws.items():
            with self.subTest(primitive=name):
                self.assert_cached_paint(draw, Rect(45, 41, 142, 105))

    def test_wide_glyph_halves_outside_the_clip_are_erased_or_tinted_together(self):
        def backdrop(renderer):
            renderer.begin("#884400")
            renderer.text("好", 32, 32, "#fff", 16)
            renderer.text("界", 64, 32, "#fff", 16)

        bounds = Rect(40, 32, 32, 16)
        for color in ("#336699", "#33669988"):
            with self.subTest(color=color):
                self.assert_cached_paint(lambda r: r.rect(bounds, color), bounds, backdrop)
        for column in (5, 8):
            with self.subTest(replace_column=column):
                self.assert_cached_paint(
                    lambda r: r.text("x", column * 8, 32, "#fff", 16), bounds, backdrop,
                )

    def test_surface_padding_never_expands_a_reset_clip(self):
        renderer = self.renderer()
        renderer.begin("#123456")
        surface = renderer.surface_create(Rect(40, 32, 32, 16))
        renderer.surface_begin(surface)
        renderer.clip(None)
        renderer.rect(Rect(0, 0, 320, 192), "#ffffff")
        renderer.surface_end()
        renderer.surface_blit(surface)
        self.assertEqual(renderer.cells[2 * 40 + 4].background, (18, 52, 86))
        self.assertEqual(renderer.cells[2 * 40 + 5].background, (255, 255, 255))
        self.assertEqual(renderer.cells[2 * 40 + 9].background, (18, 52, 86))

    def test_backdrop_validation_checks_text_colors_and_ink_and_resize(self):
        renderer = self.renderer()
        bounds = Rect(40, 32, 80, 32)
        renderer.begin("#123456")
        renderer.text("─", 48, 32, "#fff", 16)
        surface = renderer.surface_create(bounds)
        renderer.surface_begin(surface)
        renderer.rect(bounds, "#aabbcc88")
        renderer.surface_end()
        self.assertTrue(renderer.surface_matches(surface))
        renderer._ink[2 * 40 + 6] = 0  # Same visible cell, different stroke ownership.
        self.assertFalse(renderer.surface_matches(surface))
        renderer._ink[2 * 40 + 6] = 1
        renderer.text("x", 48, 32, "#fff", 16)
        self.assertFalse(renderer.surface_matches(surface))
        renderer.begin("#884400")
        self.assertFalse(renderer.surface_matches(surface))
        renderer.resize(160, 96)
        self.assertFalse(renderer.surface_matches(surface))
        with self.assertRaisesRegex(ValueError, "earlier framebuffer"):
            renderer.surface_begin(surface)
        renderer.surface_release(surface)
        self.assertEqual(surface.backdrop, [])
        self.assertEqual(surface.cells, [])


class TerminalRenderCacheTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=320, height=192, layout="absolute", reduce_motion=True)
        self.addCleanup(self.app.destroy)
        self.host = TermTextHost(io=MemoryTerminal(40, 12), probe_timeout=0)
        self.host.open("cached cells", 320, 192, False, 1)
        self.addCleanup(self.host.close)
        self.cache = RenderCache(self.host)
        self.addCleanup(self.cache.clear)

    def assert_matches_direct(self):
        paint_tree(self.app, self.host, cache=self.cache)
        cells, ink = self.host._renderer.cells.copy(), self.host._renderer._ink.copy()
        paint_tree(self.app, self.host)
        self.assertEqual(cells, self.host._renderer.cells)
        self.assertEqual(ink, self.host._renderer._ink)

    def test_stable_editor_and_label_survive_unrelated_and_backdrop_changes(self):
        panel = Container(parent=self.app, width=320, height=192, background="#884400")
        TextBox(parent=panel, text="Visible editor text", left=40, top=48, width=240, height=48)
        Label(parent=panel, text="Stable label", left=8, top=0, width=160, height=32)
        other = Label(parent=self.app, text="Ready", left=200, top=112, width=100, height=32)
        arrange(self.app, self.host)
        for _ in range(4):
            self.assert_matches_direct()
        self.assertGreater(self.cache.stats.hits, 0)
        other.text = "Changed"
        self.assert_matches_direct()
        self.assertIn("Visible editor text", "\n".join(self.host._renderer.snapshot_rows()))
        panel.background = "#225588"
        for _ in range(3):
            self.assert_matches_direct()
        self.assertLessEqual(self.cache.stats.retained_bytes, self.cache.budget)

    def test_cached_button_tracks_body_clip_when_shadow_bounds_stay_unchanged(self):
        panel = Container(parent=self.app, width=200, height=100, layout="absolute")
        button = Button(parent=panel, width=160, height=48, text="Press the button")
        arrange(self.app, self.host)
        for _ in range(3):
            self.assert_matches_direct()
        self.assertGreater(self.cache.stats.hits, 0)
        surface = next(iter(self.cache.entries.values()))[1]
        allocations = self.cache.stats.allocations
        for width in (100, 200):
            panel.width = width
            arrange(self.app, self.host)
            self.assertEqual(button.bounds.width, 160)
            self.assertEqual(button._clip.width, min(width, 160))
            for _ in range(3):
                self.assert_matches_direct()
            self.assertIs(next(iter(self.cache.entries.values()))[1], surface)
            self.assertEqual(self.cache.stats.allocations, allocations)
            hits = self.cache.stats.hits
            self.assert_matches_direct()
            self.assertGreater(self.cache.stats.hits, hits)

    def test_cache_charges_backdrop_and_padding_and_releases_storage(self):
        Label(parent=self.app, text="Stable", left=40, top=32, width=80, height=32)
        arrange(self.app, self.host)
        self.cache.configure(1024)  # Too small even for the label's full cell storage.
        for _ in range(3):
            self.assert_matches_direct()
        self.assertEqual(self.cache.stats.allocations, 0)
        self.cache.configure(32 * 1024 * 1024)
        for _ in range(3):
            self.assert_matches_direct()
        surfaces = [entry[1] for entry in self.cache.entries.values()]
        self.assertTrue(surfaces)
        self.assertEqual(self.cache.stats.retained_bytes, sum(
            self.host.surface_byte_size(entry[0][2]) for entry in self.cache.entries.values()
        ))
        self.cache.configure(0)
        self.assertEqual(self.cache.stats.retained_bytes, 0)
        self.assertTrue(all(s.released and not s.cells and not s.backdrop for s in surfaces))

    def test_subcell_clips_cache_their_empty_result(self):
        paints = []

        class CountingLabel(Label):
            def paint(self, painter):
                paints.append(1)
                super().paint(painter)

        CountingLabel(parent=self.app, text="Below a cell", left=40, top=32,
                      width=80, height=1, cache_paint=True)
        arrange(self.app, self.host)
        for _ in range(5):
            paint_tree(self.app, self.host, cache=self.cache)
        self.assertEqual(len(paints), 2)
        self.assertGreater(self.cache.stats.hits, 0)

    def test_run_resolves_the_selected_renderer_and_revalidates_changed_backdrops(self):
        app = App(width=320, height=192, layout="absolute", reduce_motion=True)
        self.addCleanup(app.destroy)
        panel = Container(parent=app, width=320, height=192, background="#884400")
        Label(parent=panel, text="Stable label", left=8, top=0, width=160, height=32)
        host = TerminalHost(renderer="python", hidden=True)
        # Runtime creates RenderCache while this wrapper has no selected host.
        app.run(backend=host)
        cache = app._runtime.render_cache
        for _ in range(3):
            paint_tree(app, host, cache=cache)
        self.assertGreater(cache.stats.hits, 0)
        panel.background = "#225588"
        for _ in range(3):
            paint_tree(app, host, cache=cache)
            cached = host.snapshot()
            paint_tree(app, host)
            self.assertEqual(cached, host.snapshot())


if __name__ == "__main__":
    unittest.main()
