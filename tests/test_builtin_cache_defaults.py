"""Catalog additions use ordinary invalidation without optimization declarations."""

from contextlib import ExitStack, contextmanager
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase
from test_render_cache import svg_drawing
from test_retained_paint import PatchRecordingHost
from test_web_svg import RecordingRenderer

from pysual import App, Container, Control, Label, Rect
from pysual import _catalog, _paint_state
from pysual._registry import _registrations
from pysual.layout import _size, arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.render_cache import RenderCache
from pysual.runtime import Runtime


@contextmanager
def catalog_entries(*kinds):
    entries = tuple(
        _catalog.BuiltinControl("cache_defaults_" + kind.__name__.lower(), kind, "tests")
        for kind in kinds
    )
    # Exercise initial installation, rather than benefiting accidentally from
    # the already-sealed process catalog. Restore all global metadata afterward.
    with ExitStack() as stack:
        stack.enter_context(patch.object(
            _catalog, "BUILTIN_CONTROLS", (*_catalog.BUILTIN_CONTROLS, *entries),
        ))
        stack.enter_context(patch.dict(_registrations))
        # Also reproduce source edits against older dependency-projection
        # implementations; the revision-only engine has no enrollment tables.
        if hasattr(_paint_state, "_bodies"):
            stack.enter_context(patch.object(_paint_state, "_installed", False))
            stack.enter_context(patch.dict(_paint_state._bodies, clear=True))
            stack.enter_context(patch.dict(_paint_state._original_hooks, clear=True))
        _catalog.install_builtin_controls()
        yield


class BuiltinCacheDefaultTests(UIOwnerTestCase):
    def fixture(self, mode):
        app = App(width=400, height=200, reduce_motion=True, cache_paint=False)
        self.addCleanup(app.destroy)
        host = PatchRecordingHost() if mode == "retained" else RecordingRenderer()
        if mode == "surface":
            host.reset_scene("Catalog defaults", 400, 200)
            self.addCleanup(host.close_scene)
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        retained = RetainedPaintTree(host) if mode == "retained" else None
        cache = RenderCache(host) if mode == "surface" else None
        runtime._retained_paint = retained
        self.addCleanup((retained if retained is not None else cache).clear)

        def frame():
            paint_tree(app, host, retained=retained, cache=cache)

        def snapshot():
            return host.snapshot() if retained is not None else svg_drawing(host.export_svg())

        def assert_direct():
            actual = snapshot()
            paint_tree(app, host)
            direct = tuple(host._drawing) if retained is not None else svg_drawing(host.export_svg())
            self.assertEqual(actual, direct)

        return app, host, retained, frame, snapshot, assert_direct

    def test_new_catalog_control_repaints_private_scalar_and_mutable_state(self):
        class PrivateControl(Control):
            def _initialize(self):
                super()._initialize()
                self._caption = "Before"
                self._items = ["one"]
                self._paints = 0

            def paint(self, painter):
                self._paints += 1
                painter.text(self._caption + ":" + ",".join(self._items), 0, 0)

        with catalog_entries(PrivateControl):
            for mode in ("retained", "surface"):
                with self.subTest(mode=mode):
                    app, host, retained, frame, snapshot, assert_direct = self.fixture(mode)
                    control = PrivateControl(parent=app, width=180, height=30, cache_paint=True)
                    sibling = Label(parent=app, text="Unchanged sibling", left=200,
                                    width=180, height=30)
                    arrange(app, host)
                    for _ in range(3):
                        frame()
                    calls = control._paints
                    frame()
                    self.assertEqual(control._paints, calls)
                    for mutation in (lambda: setattr(control, "_caption", "After"),
                                     lambda: control._items.append("two")):
                        before = snapshot()
                        mutation()
                        control.invalidate()
                        frame()
                        self.assertNotEqual(snapshot(), before)
                        if retained is not None:
                            self.assertIn(retained._records[control].identity, host.updates)
                            self.assertNotIn(retained._records[sibling].identity, host.updates)
                        assert_direct()
                        for _ in range(3):
                            frame()

    def test_new_catalog_parent_keeps_descendant_invalidation(self):
        class Summary(Container):
            def paint(self, painter):
                child = self._children[0]
                painter.rect(Rect(0, 0, 30, 20), child.foreground)
                painter.text(child._caption, 40, 0)

        class Item(Control):
            def _initialize(self):
                super()._initialize()
                self._caption = "Before"

        with catalog_entries(Summary, Item):
            for mode in ("retained", "surface"):
                with self.subTest(mode=mode):
                    app, host, retained, frame, snapshot, assert_direct = self.fixture(mode)
                    parent = Summary(parent=app, width=180, height=100, cache_paint=True)
                    child = Item(parent=parent, foreground="#ff0000", width=100, height=30,
                                 top=40, cache_paint=True)
                    arrange(app, host)
                    for _ in range(3):
                        frame()
                    before = snapshot()
                    child._caption = "Private change"
                    child.invalidate()
                    frame()
                    self.assertNotEqual(snapshot(), before)
                    if retained is not None:
                        self.assertIn(retained._records[parent].identity, host.updates)
                    assert_direct()
                    for _ in range(3):
                        frame()
                    before = snapshot()
                    parent.update_children({child: {"foreground": "#00ff00"}})
                    frame()
                    self.assertNotEqual(snapshot(), before)
                    assert_direct()

    def test_new_catalog_measurement_tracks_image_resource_revision(self):
        class ResourceMeasured(Control):
            def _initialize(self):
                super()._initialize()
                self._measure_calls = 0

            def measure(self, host):
                self._measure_calls += 1
                return host.image_extent, 24

        with catalog_entries(ResourceMeasured):
            control = ResourceMeasured()
            self.addCleanup(control.destroy)
            host = PatchRecordingHost()
            host.metrics_revision = 0
            host.image_extent = 40
            self.assertEqual(_size(control, host), (40, 24))
            calls = control._measure_calls
            self.assertEqual(_size(control, host), (40, 24))
            self.assertEqual(control._measure_calls, calls)
            host.image_extent = 80
            host.resource_revision += 1
            self.assertEqual(_size(control, host), (80, 24))
            self.assertGreater(control._measure_calls, calls)

    def test_control_attributes_do_not_implicitly_enable_optimization(self):
        class LegacyDeclarations(Control):
            _paint_reuse = True

            def _initialize(self):
                super()._initialize()
                self._paints = 0

            def _paint_inputs(self):
                raise AssertionError("Control attributes must not enroll optimization")

            def _prepare_paint(self, host):
                raise AssertionError("Control attributes must not prepare drawing")

            def _paint_measurements(self):
                raise AssertionError("Control attributes must not prefetch measurements")

            def paint(self, painter):
                self._paints += 1
                painter.text(str(self._paints), 0, 0)

        with catalog_entries(LegacyDeclarations):
            app, host, retained, frame, snapshot, assert_direct = self.fixture("retained")
            control = LegacyDeclarations(parent=app, width=180, height=30, cache_paint=False)
            arrange(app, host)
            for _ in range(3):
                frame()
            self.assertEqual(control._paints, 3)
            self.assertFalse(control.cache_paint)
