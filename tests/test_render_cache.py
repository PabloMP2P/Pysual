"""Render surface admission, accounting and failure cleanup stay consistent."""

import xml.etree.ElementTree as ET
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from _terminal import MemoryTerminal
from _ui_testcase import UIOwnerTestCase
from test_web_svg import RecordingRenderer
from test_library import RecordingHost

from pysual import App, Button, Dropdown, Label, ListView, TextBox
from pysual.backends.term_text import TermTextHost
from pysual.layout import arrange
from pysual.render_cache import RenderCache
from pysual.painting import paint_tree
from pysual.runtime import Runtime


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


def svg_drawing(svg):
    """Compare drawing attributes and effective clips, independent of groups."""
    root = ET.fromstring(svg)
    definitions = {node.get("id"): node for node in root.iter() if node.get("id")}

    def attributes(node):
        attributes = []
        for name, value in sorted(node.attrib.items()):
            if name == "id" or name.startswith("data-pysual-"):
                continue
            identifier = value[5:-1] if value.startswith("url(#") else None
            if name == "href" and value.startswith("#"):
                identifier = value[1:]
            attributes.append((name, definition(definitions[identifier]) if identifier else value))
        return tuple(attributes)

    def definition(node):
        return (node.tag.rsplit("}", 1)[-1], attributes(node), node.text or "",
                tuple(definition(child) for child in node))

    def visit(node, clip=None, groups=()):
        tag = node.tag.rsplit("}", 1)[-1]
        if tag == "defs":
            return ()
        attrs = dict(attributes(node))
        clipped = attrs.pop("clip-path", None)
        if clipped is not None:
            # These renderers clip with one rectangle in user coordinates.
            assert clipped[0] == "clipPath" and len(clipped[3]) == 1
            rectangle = clipped[3][0]
            assert rectangle[0] == "rect"
            bounds = dict(rectangle[1])
            x, y, width, height = (float(bounds[name]) for name in ("x", "y", "width", "height"))
            next_clip = x, y, x + width, y + height
            clip = next_clip if clip is None else (
                max(clip[0], next_clip[0]), max(clip[1], next_clip[1]),
                min(clip[2], next_clip[2]), min(clip[3], next_clip[3]),
            )
        if tag == "g":
            groups = (*groups, tuple(attrs.items())) if attrs else groups
            return tuple(item for child in node for item in visit(child, clip, groups))
        if tag == "svg":
            return ((tag, tuple(attrs.items())), *(
                item for child in node for item in visit(child, clip, groups)
            ))
        return ((tag, tuple(attrs.items()), node.text or "", clip, groups,
                 tuple(definition(child) for child in node)),)

    return visit(root)


class BackendSurfaceCacheTests(UIOwnerTestCase):
    def fixture(self, backend):
        app = App(width=320, height=192, reduce_motion=True, cache_paint=False)
        self.addCleanup(app.destroy)
        if backend == "terminal":
            host = TermTextHost(io=MemoryTerminal(40, 12), probe_timeout=0)
            host.open("shared cache", 320, 192, False, 1)
            self.addCleanup(host.close)
        else:
            host = RecordingRenderer()
            host.reset_scene("shared cache", 320, 192)
            self.addCleanup(host.close_scene)
        runtime = Runtime(app, host)
        app._runtime = runtime
        cache = RenderCache(host)
        self.addCleanup(cache.clear)
        self.addCleanup(setattr, app, "_runtime", None)
        return app, host, cache, runtime

    def snapshot(self, host):
        if isinstance(host, TermTextHost):
            return tuple(host._renderer.cells), tuple(host._renderer._ink)
        return svg_drawing(host.export_svg())

    def frame(self, app, host, cache, runtime):
        paint_tree(app, host, runtime.router.focus, cache=cache)

    def assert_direct_matches(self, app, host, runtime):
        cached = self.snapshot(host)
        paint_tree(app, host, runtime.router.focus)
        self.assertEqual(cached, self.snapshot(host))

    def warm(self, app, host, cache, runtime):
        arrange(app, host)
        for _ in range(3):
            self.frame(app, host, cache, runtime)
        self.assertGreater(cache.stats.hits, 0)
        self.assert_direct_matches(app, host, runtime)

    def test_unchanged_invalidation_reuses_surface_without_drawing(self):
        for backend in ("terminal", "svg"):
            with self.subTest(backend=backend):
                app, host, cache, runtime = self.fixture(backend)
                label = Label(parent=app, text="Stable caption", width=200, height=32)
                self.warm(app, host, cache, runtime)
                before = cache.stats
                label.invalidate()
                with patch.object(host, "text", wraps=host.text) as text:
                    self.frame(app, host, cache, runtime)
                    text.assert_not_called()
                self.assertEqual(cache.stats.misses, before.misses)
                self.assertEqual(cache.stats.rebuilds, before.rebuilds)
                self.assertEqual(cache.stats.hits, before.hits + 1)
                self.assert_direct_matches(app, host, runtime)
                label.text = "Updated caption"
                self.frame(app, host, cache, runtime)
                self.assertGreater(cache.stats.misses, before.misses)
                self.assert_direct_matches(app, host, runtime)

    def test_private_scroll_and_fixed_row_metrics_change_cached_output(self):
        for backend in ("terminal", "svg"):
            with self.subTest(backend=backend):
                app, host, cache, runtime = self.fixture(backend)
                host.text_row_height = 16
                listing = ListView(parent=app, width=200, height=128,
                                   items=tuple(f"Row {index}" for index in range(20)))
                self.warm(app, host, cache, runtime)
                initial = self.snapshot(host)
                before = cache.stats.misses
                listing._scroll = 2
                listing.invalidate()
                self.frame(app, host, cache, runtime)
                self.assertGreater(cache.stats.misses, before)
                self.assertNotEqual(self.snapshot(host), initial)
                self.assert_direct_matches(app, host, runtime)
                self.warm(app, host, cache, runtime)
                before = cache.stats.misses
                host.text_row_height = 32
                arrange(app, host)
                listing.invalidate()
                self.frame(app, host, cache, runtime)
                self.assertGreater(cache.stats.misses, before)
                self.assert_direct_matches(app, host, runtime)

    def test_inherited_custom_property_keeps_revision_invalidation(self):
        class CaptionDropdown(Dropdown):
            def _initialize(self):
                super()._initialize()
                self._caption = "Before"

            @property
            def selected_item(self):
                return self._caption

        for backend in ("terminal", "svg"):
            with self.subTest(backend=backend):
                app, host, cache, runtime = self.fixture(backend)
                dropdown = CaptionDropdown(parent=app, width=200, height=32)
                self.warm(app, host, cache, runtime)
                initial = self.snapshot(host)
                before = cache.stats.misses
                dropdown._caption = "After"
                dropdown.invalidate()
                self.frame(app, host, cache, runtime)
                self.assertGreater(cache.stats.misses, before)
                self.assertNotEqual(self.snapshot(host), initial)
                self.assert_direct_matches(app, host, runtime)

    def test_editor_admission_uses_state_after_caret_reveal(self):
        for backend in ("terminal", "svg"):
            with self.subTest(backend=backend):
                app, host, cache, runtime = self.fixture(backend)
                editor = TextBox(parent=app, text="A long line extending beyond the editor",
                                 width=160, height=48)
                arrange(app, host)
                runtime.router.set_focus(editor)
                editor._caret = editor._anchor = len(editor.text)
                editor._reveal_after_paint = True
                self.frame(app, host, cache, runtime)
                self.assertFalse(editor._reveal_after_paint)
                self.assertGreater(editor._scroll_x, 0)
                self.frame(app, host, cache, runtime)
                self.assertEqual(cache.stats.rebuilds, 1)
                before = cache.stats.hits
                editor.invalidate()
                with patch.object(host, "text", wraps=host.text) as text:
                    self.frame(app, host, cache, runtime)
                    text.assert_not_called()
                self.assertEqual(cache.stats.hits, before + 1)
                self.assert_direct_matches(app, host, runtime)

    def test_terminal_backdrop_mismatch_still_rebuilds_unchanged_label(self):
        app, host, cache, runtime = self.fixture("terminal")
        label = Label(parent=app, text="Transparent label", width=200, height=32)
        self.warm(app, host, cache, runtime)
        before = cache.stats
        app.background = "#225588"
        label.invalidate()
        with patch.object(host, "text", wraps=host.text) as text:
            self.frame(app, host, cache, runtime)
            self.assertGreater(text.call_count, 0)
        self.assertEqual(cache.stats.hits, before.hits)
        self.assertGreater(cache.stats.misses, before.misses)
        self.assert_direct_matches(app, host, runtime)
