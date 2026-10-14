"""Render surface admission, accounting and failure cleanup stay consistent."""

from dataclasses import FrozenInstanceError

from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import Button
from pysual.layout import arrange
from pysual.render_cache import RenderCache


class SurfaceHost(RecordingHost):
    capabilities = frozenset({"render_surfaces"})
    render_scale = 1
    resource_revision = 0

    def __init__(self):
        super().__init__()
        self.size = (10, 10)
        self.created = []
        self.released = []
        self.active = None
        self.blits = 0

    def surface_create(self, rect):
        surface = object()
        self.created.append(surface)
        return surface

    def surface_begin(self, surface):
        assert self.active is None
        self.active = surface

    def surface_end(self):
        assert self.active is not None
        self.active = None

    def surface_blit(self, surface):
        assert surface not in self.released
        self.blits += 1

    def surface_release(self, surface):
        assert self.active is None
        assert surface not in self.released
        self.released.append(surface)


class RenderCacheTests(UIOwnerTestCase):
    def setUp(self):
        self.host = SurfaceHost()
        self.control = Button(cache_paint=True, width=10, height=10)
        self.addCleanup(self.control.destroy)
        arrange(self.control, self.host)
        self.cache = RenderCache(self.host)
        self.addCleanup(self.cache.clear)
        self.draws = 0

    def draw(self):
        self.draws += 1

    def frame(self, draw=None):
        self.cache.start_frame()
        self.cache.paint(self.control, None, self.draw if draw is None else draw)
        self.cache.finish_frame()

    def test_snapshots_preserve_types_and_do_not_change_with_live_counters(self):
        self.frame()
        before = self.cache.stats
        self.assertEqual((before.misses, before.allocations), (1, 0))
        self.frame()
        self.frame()
        built = self.cache.stats
        self.assertEqual((built.misses, built.hits, built.rebuilds), (2, 1, 1))
        self.assertEqual((built.allocations, built.entries, built.retained_bytes), (1, 1, 400))
        self.assertEqual(self.draws, 2)
        self.assertIs(type(built.misses), int)
        self.assertIs(type(built.build_seconds), float)
        with self.assertRaises(FrozenInstanceError):
            built.hits = 30
        self.control.invalidate()
        self.frame()
        self.frame()
        self.assertEqual((self.cache.stats.allocations, self.cache.stats.rebuilds), (1, 2))
        self.cache.configure(0)
        self.frame()
        final = self.cache.stats
        self.assertEqual((final.evictions, final.releases, final.bypasses), (1, 1, 1))
        self.assertEqual((final.retained_bytes, final.entries), (0, 0))
        self.assertEqual((before.allocations, built.rebuilds), (0, 1))
        self.cache.clear()
        self.assertEqual(self.cache.stats, final)

    def test_failed_build_releases_new_or_reused_storage(self):
        def fail():
            raise RuntimeError("drawing failed")

        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                self.cache.clear()
                if reuse:
                    self.frame()
                    self.frame()
                    self.control.invalidate()
                self.frame()
                before = self.cache.stats
                with self.assertRaisesRegex(RuntimeError, "drawing failed"):
                    self.frame(fail)
                after = self.cache.stats
                self.assertEqual(after.allocations - before.allocations, int(not reuse))
                self.assertEqual(after.releases - before.releases, 1)
                self.assertEqual(after.rebuilds, before.rebuilds)
                self.assertEqual((after.retained_bytes, after.entries), (0, 0))
                self.assertFalse(self.cache.pending)
                self.assertIsNone(self.host.active)

    def test_reuse_requires_an_admitted_paint_key(self):
        self.frame()
        self.frame()
        self.control.cache_paint = False
        key = self.cache.paint_key(self.control, None, self.control.effective_theme)
        self.assertIsNone(key)
        self.assertEqual(self.cache.stats.entries, 1)
        before_blits = self.host.blits
        self.cache.start_frame()
        self.assertFalse(self.cache.reuse(self.control, key))
        self.assertEqual(self.host.blits, before_blits)
        self.assertEqual(self.cache.stats.hits, 0)
        self.cache.finish_frame()
        self.assertEqual((self.cache.stats.entries, self.cache.stats.releases), (0, 1))

    def test_noncallable_optional_hooks_use_default_surface_behavior(self):
        self.host.surface_byte_size = object()
        self.host.surface_matches = object()
        for _ in range(3):
            self.frame()
        self.assertEqual(self.cache.stats.retained_bytes, 400)
        self.assertEqual(self.cache.stats.hits, 1)

    def test_invalid_byte_estimates_cannot_allocate_or_corrupt_accounting(self):
        for value in (True, False, 1.5, float("nan"), "400", None, -1):
            with self.subTest(value=value):
                self.host.surface_byte_size = lambda rect: value
                error = ValueError if value == -1 else TypeError
                with self.assertRaisesRegex(error, "surface_byte_size"):
                    self.frame()
                self.assertEqual(self.cache.stats.retained_bytes, 0)
                self.assertEqual(self.cache.stats.allocations, 0)
                self.assertFalse(self.cache.pending)
        self.assertFalse(self.host.created)

    def test_zero_estimate_bypasses_and_hook_errors_propagate(self):
        self.host.surface_byte_size = lambda rect: 0
        self.frame()
        self.assertEqual((self.draws, self.cache.stats.bypasses), (1, 1))

        def fail(rect):
            raise RuntimeError("measurement failed")

        self.host.surface_byte_size = fail
        with self.assertRaisesRegex(RuntimeError, "measurement failed"):
            self.frame()
        self.assertFalse(self.host.created)
