"""Preferred-size reuse preserves custom hooks and explicit invalidation."""

import math
import unittest

import pytest

from _terminal import MemoryTerminal
from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Container, Control, Label, Rect, dark, prop
from pysual._text_layout import TextLayoutCache
from pysual.backends._web_svg import SVGRenderer
from pysual.backends.native import NativeHost
from pysual.backends.term_text import TermTextHost
from pysual.backends.terminal import TerminalHost
from pysual.host import Viewport
from pysual._engine import call
from pysual.errors import LifecycleError
from pysual.layout import _size, arrange
from pysual.painting import Painter, paint_tree
from pysual.schema import Dirty


class Measured(Control):
    caption: str = prop(default="four", affects=Dirty.MEASURE | Dirty.PAINT)

    def _initialize(self):
        super()._initialize()
        self._measure_calls = 0

    def measure(self, host):
        self._measure_calls += 1
        return len(self.caption) * 10, 20


class Available(Measured):
    def measure_available(self, host, *, width=None, height=None):
        self._measure_calls += 1
        preferred = len(self.caption) * 10
        return min(preferred, width or preferred), 40 if width and width < preferred else 20


class MeasurementCacheTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App()
        self.host = RecordingHost()

    def tearDown(self):
        self.app.destroy()

    def size(self, control, **limits):
        return _size(control, self.host, **limits)

    def test_custom_measure_reuses_clean_siblings_after_caption_change(self):
        parent = Container(parent=self.app, layout="stack", width=300, spacing=0)
        changed = Measured(parent=parent)
        clean = Measured(parent=parent)
        arrange(self.app, self.host)
        before = clean._measure_calls
        changed_before = changed._measure_calls
        changed.caption = "considerably longer"
        arrange(self.app, self.host)
        self.assertEqual(changed.bounds.width, 300)
        self.assertEqual(parent.bounds.width, 300)
        self.assertGreater(changed._measure_calls, changed_before)
        self.assertEqual(clean._measure_calls, before)

    def test_flex_grid_remeasures_intrinsic_stack_ancestors(self):
        self.app.update(layout="stack", padding=0, spacing=0)
        for depth in (0, 2):
            for batched in (False, True):
                with self.subTest(depth=depth, batched=batched):
                    outer = Container(parent=self.app, layout="stack", spacing=0)
                    parent = outer
                    for _ in range(depth):
                        parent = Container(parent=parent, layout="stack", flex=1,
                                           spacing=0)
                    grid = Container(parent=parent, layout="grid", flex=1,
                                     columns=1, spacing=0)
                    label = Label(parent=grid, text="one", font_size=15)
                    arrange(self.app, self.host)
                    self.assertEqual(outer.bounds.height, 18)
                    if batched:
                        outer.update_children({label: {"font_size": 45}})
                    else:
                        label.font_size = 45
                    arrange(self.app, self.host)
                    self.assertEqual(outer.bounds.height, 54)
                    self.assertEqual(grid.bounds.height, 54)
                    self.assertEqual(label.bounds.height, 54)
                    outer.destroy()

    def test_flex_grid_under_fixed_stack_keeps_unchanged_boxes(self):
        self.app.update(layout="stack", padding=0, spacing=0)
        parent = Container(parent=self.app, layout="stack", flex=1, spacing=0)
        grid = Container(parent=parent, layout="grid", flex=1, columns=1, spacing=0)
        label = Label(parent=grid, text="one", font_size=15)
        arrange(self.app, self.host)
        boxes = (parent.bounds, grid.bounds, label.bounds)
        label.font_size = 45
        # Dependency notifications reach ancestors even when placement can
        # reuse the fixed grid's plan. Custom ancestor measurement stays valid.
        self.assertTrue(self.app.dirty & Dirty.MEASURE)
        arrange(self.app, self.host)
        self.assertEqual((parent.bounds, grid.bounds, label.bounds), boxes)

    def test_grid_custom_arranger_still_receives_preferred_size_changes(self):
        class MeasuredGrid(Container):
            def arrange_children(self, area, measure):
                return {child: Rect(area.x, area.y, *measure(child))
                        for child in self.children}

        grid = MeasuredGrid(parent=self.app, layout="grid", width=300, height=200)
        label = Label(parent=grid, text="one", font_size=15)
        arrange(self.app, self.host)
        self.assertEqual(label.bounds.height, 18)
        label.font_size = 45
        arrange(self.app, self.host)
        self.assertEqual(label.bounds.height, 54)

    def test_grid_overlay_still_receives_preferred_size_changes(self):
        class OverlayLabel(Label):
            _overlay = True

        grid = Container(parent=self.app, layout="grid", width=300, height=200)
        label = OverlayLabel(parent=grid, text="one", font_size=15)
        arrange(self.app, self.host)
        self.assertEqual(label.bounds.height, 18)
        label.font_size = 45
        arrange(self.app, self.host)
        self.assertEqual(label.bounds.height, 54)

    def test_fixed_size_can_return_to_current_intrinsic_size(self):
        control = Measured(parent=self.app, width=80, height=30)
        self.assertEqual(self.size(control), (80, 30))
        self.assertEqual(self.size(control), (80, 30))
        self.assertEqual(control._measure_calls, 1)
        control.caption = "longer"
        self.assertEqual(self.size(control), (80, 30))
        control.update(width=None, height=None)
        self.assertEqual(self.size(control), (60, 20))

    def test_paint_only_changes_reuse_preferred_size(self):
        child = Measured(parent=self.app)
        self.assertEqual(self.size(child), (40, 20))
        child.foreground = "#ff0000"
        self.assertEqual(self.size(child), (40, 20))
        child.invalidate(Dirty.PAINT)
        self.assertEqual(self.size(child), (40, 20))
        self.assertEqual(child._measure_calls, 1)

    def test_subwindow_closable_refreshes_automatic_title_width(self):
        for use_update in (False, True):
            for child_width in (None, 120, 300):
                with self.subTest(update=use_update, child_width=child_width):
                    dialog = self.app.sub_window(
                        width=None, title="Project settings", closable=False
                    )
                    if child_width is not None:
                        dialog.panel(width=child_width, height=30)
                    for closable in (False, True, False):
                        if use_update:
                            dialog.update(closable=closable)
                        else:
                            dialog.closable = closable
                        arrange(self.app, self.host)
                        paint_tree(self.app, self.host)
                        expected = 324 if child_width == 300 else 202 if closable else 164
                        self.assertEqual(dialog.bounds.width, expected)
                        self.assertIn("Project settings", self.host.texts)
                    dialog.destroy()

    def test_available_constraints_distinguish_wrapped_measurements(self):
        control = Available(parent=self.app)
        self.assertEqual(self.size(control, width=30, height=100), (30, 40))
        self.assertEqual(self.size(control, width=30, height=100), (30, 40))
        self.assertEqual(control._measure_calls, 1)
        self.assertEqual(self.size(control, width=80, height=100), (40, 20))
        self.assertEqual(control._measure_calls, 2)
        self.size(control, width=80, height=50)
        self.assertEqual(control._measure_calls, 3)

    def test_constraint_cache_is_bounded(self):
        control = Available(parent=self.app)
        for width in range(1, 129):
            self.size(control, width=width)
        before = control._measure_calls
        self.size(control, width=128)
        self.assertEqual(control._measure_calls, before)
        self.size(control, width=1)
        self.assertGreater(control._measure_calls, before)

    def test_layout_size_hook_runs_even_when_preferred_size_is_reused(self):
        class Adjusted(Measured):
            def _initialize(self):
                super()._initialize()
                self._extra = 0

            def _layout_size(self, width, height):
                return width + self._extra, height + self.effective_row_height(0)

        self.host.text_row_height = 5
        control = Adjusted(parent=self.app, min_width=50, max_height=15)
        self.assertEqual(self.size(control), (50, 20))
        control._extra = 7
        self.assertEqual(self.size(control), (57, 20))
        self.assertEqual(control._measure_calls, 1)

    def test_parent_arrange_observes_caption_even_when_child_size_is_fixed(self):
        class CaptionLayout(Container):
            def arrange_children(self, area, measure):
                child = self.children[0]
                width, height = measure(child)
                return {child: Rect(area.x + len(child.caption), area.y, width, height)}

        parent = CaptionLayout(parent=self.app, width=300, height=80)
        child = Measured(parent=parent, width=80, height=20)
        arrange(self.app, self.host)
        self.assertEqual(child.bounds, Rect(4, 0, 80, 20))
        child.caption = "longer"
        arrange(self.app, self.host)
        self.assertEqual(child.bounds, Rect(6, 0, 80, 20))

    def test_private_and_peer_measurements_refresh_when_dependents_invalidate(self):
        class Dependent(Measured):
            def _initialize(self):
                super()._initialize()
                self._peer = None
                self._extra = 0

            def measure(self, host):
                self._measure_calls += 1
                return len(self._peer.caption) * 10 + self._extra, 20

        parent = Container(parent=self.app, layout="stack", spacing=0)
        peer = Measured(parent=parent)
        dependent = Dependent(parent=parent)
        dependent._peer = peer
        self.assertEqual(self.size(parent), (40, 40))
        peer.caption = "longer"
        dependent._extra = 15
        dependent.invalidate(Dirty.MEASURE)
        self.assertEqual(self.size(parent), (75, 40))

    def test_uncached_descendant_bypasses_every_intrinsic_ancestor(self):
        class External(Measured):
            def _initialize(self):
                super()._initialize()
                self._preferred = 40

            def measure(self, host):
                self._measure_calls += 1
                return self._preferred, 20

        outer = Container(parent=self.app, layout="stack", spacing=0)
        inner = Container(parent=outer, layout="stack", spacing=0)
        child = External(parent=inner, cache_measure=False)
        self.assertEqual(self.size(outer), (40, 20))
        child._preferred = 90
        self.assertEqual(self.size(outer), (90, 20))
        child.cache_measure = True
        self.assertEqual(self.size(outer), (90, 20))
        before = child._measure_calls
        self.assertEqual(self.size(outer), (90, 20))
        self.assertEqual(child._measure_calls, before)
        child.cache_measure = False
        child._preferred = 110
        self.assertEqual(self.size(outer), (110, 20))

    def test_structural_changes_refresh_intrinsic_size_and_opt_out_status(self):
        parent = Container(parent=self.app, layout="stack", spacing=0)
        child = Measured(parent=parent)
        self.assertEqual(self.size(parent), (40, 20))
        added = Measured(parent=parent, caption="longer", cache_measure=False)
        self.assertEqual(self.size(parent), (60, 40))
        added.destroy()
        self.assertEqual(self.size(parent), (40, 20))
        before = child._measure_calls
        self.assertEqual(self.size(parent), (40, 20))
        self.assertEqual(child._measure_calls, before)

    def test_explicit_parent_invalidation_refreshes_descendants_but_child_does_not(self):
        parent = Container(parent=self.app, layout="stack", spacing=0)
        first, second = Measured(parent=parent), Measured(parent=parent)
        self.size(parent)
        before = second._measure_calls
        first.invalidate(Dirty.MEASURE)
        self.size(parent)
        self.assertEqual(second._measure_calls, before)
        parent.invalidate(Dirty.MEASURE)
        self.size(parent)
        self.assertGreater(second._measure_calls, before)

    def test_arrange_properties_can_affect_custom_measurement(self):
        class Positioned(Measured):
            def measure(self, host):
                self._measure_calls += 1
                return self.left + self.parent.top + 40, 20

        parent = Container(parent=self.app)
        child = Positioned(parent=parent)
        self.assertEqual(self.size(child), (40, 20))
        child.left = 5
        self.assertEqual(self.size(child), (45, 20))
        parent.top = 10
        self.assertEqual(self.size(child), (55, 20))

    def test_atomic_and_child_batches_refresh_measurements(self):
        parent = Container(parent=self.app, layout="stack", spacing=0)
        first, second = Measured(parent=parent), Measured(parent=parent)
        self.assertEqual(self.size(parent), (40, 40))
        first.update(caption="longer", min_width=80)
        self.assertEqual(self.size(parent), (80, 40))
        parent.update_children({first: {"caption": "ten letters", "min_width": 0},
                                second: {"caption": "a"}})
        self.assertEqual(self.size(parent), (110, 40))

    def test_declared_measure_dirty_survives_custom_changed_hook(self):
        class CustomChanged(Measured):
            def _changed(self, field, old, value):
                # A hook can inspect/consume a field without forwarding it.
                self._last_change = field.name

        parent = Container(parent=self.app, layout="stack", spacing=0)
        child = CustomChanged(parent=parent)
        self.assertEqual(self.size(parent), (40, 20))
        child.caption = "longer"
        self.assertEqual(self.size(parent), (60, 20))
        child.update(caption="a")
        self.assertEqual(self.size(parent), (10, 20))

    def test_host_resources_viewport_theme_and_fixed_rows_refresh_context(self):
        class Contextual(Measured):
            def measure(self, host):
                self._measure_calls += 1
                return (self.effective_theme.tokens.font_size + host.size[0] + host.unit,
                        self.effective_row_height(11))

        self.host.unit, self.host.resource_revision = 1, 0
        child = Contextual(parent=self.app)
        initial = self.size(child)
        self.assertEqual(self.size(child), initial)
        self.assertEqual(child._measure_calls, 1)
        self.host.unit = 2
        self.host.resource_revision += 1
        self.assertEqual(self.size(child), (initial[0] + 1, 11))
        self.host.size = (800, 400)
        self.assertEqual(self.size(child), (initial[0] + 161, 11))
        self.app.theme = self.app.theme.derive(font_size=30)
        self.assertEqual(self.size(child), (832, 11))
        self.host.text_row_height = 24
        self.assertEqual(self.size(child), (832, 24))
        other = RecordingHost()
        other.unit = 4
        self.assertEqual(_size(child, other), (674, 11))
        self.assertIsNone(self.app._measurement_row_height)

    def test_invalidation_during_measurement_does_not_cache_stale_result(self):
        class Changing(Measured):
            def measure(self, host):
                self._measure_calls += 1
                if self._measure_calls == 1:
                    self.invalidate(Dirty.MEASURE)
                    return 40, 20
                return 80, 20

        child = Changing(parent=self.app)
        self.assertEqual(self.size(child), (40, 20))
        self.assertEqual(self.size(child), (80, 20))
        self.assertEqual(self.size(child), (80, 20))
        self.assertEqual(child._measure_calls, 2)

    def test_failed_measurement_is_retried_and_restores_row_context(self):
        class Failing(Measured):
            def measure(self, host):
                self._measure_calls += 1
                if self._measure_calls == 1:
                    raise ValueError("measurement failed")
                return 40, 20

        child = Failing(parent=self.app)
        self.host.text_row_height = 24
        with self.assertRaisesRegex(ValueError, "measurement failed"):
            self.size(child)
        self.assertIsNone(self.app._measurement_row_height)
        self.assertEqual(self.size(child), (40, 20))
        self.assertEqual(self.size(child), (40, 20))
        self.assertEqual(child._measure_calls, 2)

    def test_detached_measurement_adopts_new_parent_and_disposal_is_checked(self):
        child = Measured()
        self.assertEqual(self.size(child), (40, 20))
        before = child._measure_calls
        self.app.add(child)
        self.assertEqual(self.size(child), (40, 20))
        self.assertGreater(child._measure_calls, before)
        child.destroy()
        with self.assertRaises(LifecycleError):
            self.size(child)


class CallerMeasurementCacheTests(unittest.TestCase):
    def test_normal_caller_scalar_assignment_refreshes_owner_measurement(self):
        app, host = App(), RecordingHost()
        try:
            child = Measured(parent=app)
            self.assertEqual(call(_size, child, host), (40, 20))
            child.caption = "longer"
            self.assertEqual(call(_size, child, host), (60, 20))
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()


class TextMetricBridge:
    diagnostics = {}

    def request(self, op, **fields):
        if op == "open":
            return {"width": 320, "height": 180, "scale": 1, "resource_revision": 0}
        if op == "measure":
            return len(fields["text"]) * 8, 16
        if op == "measure_many":
            return [(len(text) * 8, 16) for text in fields["texts"]]
        raise AssertionError(op)

    def close(self):
        pass


def native_host():
    host = NativeHost(client_factory=lambda **_kwargs: TextMetricBridge())
    host.open("Text", 320, 180, True, 1)
    return host


def terminal_host():
    return TermTextHost(io=MemoryTerminal(40, 12), probe_timeout=0, environ={})


def terminal_wrapper():
    host = TerminalHost(renderer="python", hidden=True)
    host.open("Text", 320, 180, True, 1)
    return host


def painter(host):
    return Painter(host, Rect(0, 0, 200, 80), dark())


@pytest.mark.parametrize("factory", [native_host, SVGRenderer, terminal_host, terminal_wrapper])
def test_measure_and_elide_reuse_between_painters_on_all_builtin_hosts(factory, monkeypatch):
    host = factory()
    # This fixture counts shared-cache calls, independently of a host's own LRU.
    monkeypatch.setattr(host, "_text_measure_cache_limit", 0, raising=False)
    raw = host.measure
    calls = []

    def counted(text, size, mono=False):
        calls.append(text)
        return raw(text, size, mono)

    monkeypatch.setattr(host, "measure", counted)
    try:
        first, second = painter(host), painter(host)
        expected = first.measure("caption", size=16, font_family="ui")
        before = len(calls)
        assert second.measure("caption", size=16, font_family="ui") == expected
        assert len(calls) == before
        fitted = first.elide("Repeated café caption", 48, size=16, font_family="ui")
        assert fitted.endswith("…")
        before = len(calls)
        assert second.elide("Repeated café caption", 48, size=16, font_family="ui") == fitted
        assert len(calls) == before
    finally:
        if isinstance(host, SVGRenderer):
            host.close_scene()
        else:
            host.close()


@pytest.mark.parametrize("change", ["resource", "scale", "row"])
def test_metric_environment_changes_clear_old_entries(change, monkeypatch):
    host = SVGRenderer()
    host._text_measure_cache_limit = 0
    width = 10
    monkeypatch.setattr(host, "measure", lambda *_args: (width, 16))
    pen = painter(host)
    assert pen.measure("caption", size=16) == (10, 16)
    width = 20
    if change == "resource":
        host._resource_revision += 1
    elif change == "scale":
        host._viewport = Viewport(*host.size, scale=2)
    else:
        host.text_row_height = 24
    assert pen.measure("caption", size=16) == (20, 16)
    assert len(host._text_layout_cache._entries) == 1


def test_native_metric_replacement_invalidates_both_measurement_and_elision():
    host = native_host()
    try:
        pen = painter(host)
        assert pen.measure("caption", size=16) == (56, 16)
        assert pen.elide("caption", 32, size=16).endswith("…")
        host._store_metric("caption", 16, False, (24, 16))
        assert pen.measure("caption", size=16) == (24, 16)
        assert pen.elide("caption", 32, size=16) == "caption"
        # Native display sanitation still occurs before the raw metric cache.
        assert pen.measure("cap\0tion", size=16) == (24, 16)
    finally:
        host.close()
    assert not host._text_layout_cache._entries


@pytest.mark.parametrize("factory", [SVGRenderer, terminal_host])
def test_reopening_and_closing_release_cached_text(factory):
    host = factory()
    pen = painter(host)
    # Default SVG retains long metrics here while its own font LRU owns short
    # ones. Exercise both shared long measurements and overflowing captions.
    text = ("x" * (host._text_measure_cache_limit + 1)
            if isinstance(host, SVGRenderer) else "before opening")
    pen.measure(text, size=16)
    assert host._text_layout_cache._entries
    if isinstance(host, SVGRenderer):
        host.reset_scene("Text", 320, 180)
    else:
        host.open("Text", 320, 180, True, 1)
    assert not host._text_layout_cache._entries
    pen.elide("during opening", 48, size=16)
    assert host._text_layout_cache._entries
    if isinstance(host, SVGRenderer):
        host.close_scene()
        host.reset_scene("Again", 320, 180)
    else:
        host.close()
        host.open("Again", 320, 180, True, 1)
    assert not host._text_layout_cache._entries
    pen.measure(text, size=16)
    assert host._text_layout_cache._entries
    if isinstance(host, SVGRenderer):
        host.close_scene()
    else:
        host.close()
    assert not host._text_layout_cache._entries


def test_entry_and_text_byte_budgets_evict_oldest_use_and_skip_long_inputs(monkeypatch):
    cache = TextLayoutCache()
    monkeypatch.setattr(cache, "MAX_ENTRIES", 2)
    cache.store(("m", "first"), (1, 1))
    cache.store(("m", "second"), (2, 2))
    assert cache.get(("m", "first")) == (1, 1)
    cache.store(("m", "third"), (3, 3))
    assert cache.get(("m", "second")) is None
    assert len(cache._entries) == 2
    cache.clear()
    monkeypatch.setattr(cache, "MAX_BYTES", 1100)
    cache.store(("e", "a" * 100), "b" * 100)
    assert cache._bytes > 1000  # Includes the fitted output as well as its input.
    cache.store(("m", "next"), (1, 1))
    assert cache.get(("e", "a" * 100)) is None
    assert cache._bytes <= cache.MAX_BYTES
    previous = cache._bytes
    cache.store(("m", "x" * (cache.MAX_TEXT + 1)), (1, 1))
    assert cache._bytes == previous
    cache.prepare((1, 1, 0))
    assert not cache._entries and cache._bytes == 0


def test_third_party_hosts_need_no_new_attribute_or_capability():
    class Host:
        __slots__ = ("calls",)

        def __init__(self):
            self.calls = 0

        def measure(self, text, size, mono=False):
            self.calls += 1
            return len(text) * 8, 16

    host = Host()
    pen = painter(host)
    assert pen.elide("caption", 32, size=16) == "cap…"
    before = host.calls
    assert pen.elide("caption", 32, size=16) == "cap…"
    assert host.calls > before
    assert not hasattr(host, "_text_layout_cache")


def test_invalid_measurements_and_oversized_text_are_not_cached():
    host = SVGRenderer()
    pen = painter(host)
    for size in (math.nan, math.inf, -1):
        with pytest.raises(ValueError):
            pen.measure("caption", size=size)
    assert not host._text_layout_cache._entries
    pen.measure("x" * (TextLayoutCache.MAX_TEXT + 1), size=16)
    assert not host._text_layout_cache._entries
