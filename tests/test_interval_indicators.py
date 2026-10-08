"""Interval controls keep validation, input ownership, and painting portable."""

from math import isfinite
import xml.etree.ElementTree as ET

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Container, ProgressBar, Slider, Style, Theme, blueprint
from pysual._controls.indicators import CircularProgress, RangeSlider
from pysual.backends._term_cells import CellRenderer
from pysual.backends._web_svg import SVGRenderer
from pysual.errors import LifecycleError
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import Painter, paint_tree, resolve_style
from pysual.runtime import Runtime


class IndicatorHost(RecordingHost):
    def begin(self, color):
        super().begin(color)
        self.rects = []
        self.lines_drawn = []
        self.text_calls = []

    def rect(self, *args, **kwargs):
        self.rects.append((args, kwargs))

    def line(self, *args, **kwargs):
        self.lines_drawn.append(args)

    def text(self, text, *args, **kwargs):
        super().text(text, *args, **kwargs)
        self.text_calls.append((text, args))


class IntervalIndicatorTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=500, height=250)
        self.host = IndicatorHost()
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.addCleanup(self.app.destroy)
        self.addCleanup(setattr, self.app, "_runtime", None)

    def slider(self, **properties):
        owner = RangeSlider(parent=self.app, left=10, top=10, width=218,
                            **properties)
        arrange(self.app, self.host)
        return owner

    def ring(self, **properties):
        owner = CircularProgress(parent=self.app, left=20, top=20, **properties)
        arrange(self.app, self.host)
        return owner

    def pointer(self, owner, kind, value, *, pointer_id=7, button=1, routed=False):
        left, width, y = owner._track()
        x = owner.bounds.x + left + width * (value - owner.minimum) / (owner.maximum - owner.minimum)
        event = Input(kind, x, owner.bounds.y + y, pointer_id=pointer_id, button=button)
        self.runtime.router.process(event) if routed else owner.handle_input(event)

    def paint(self):
        paint_tree(self.app, self.host)

    def test_pointer_edits_nearest_endpoint_and_thumbs_cannot_cross(self):
        slider = self.slider()
        self.pointer(slider, "pointer_down", 30)
        self.assertEqual(slider.value, (30, 75))
        self.pointer(slider, "pointer_move", 90)
        self.assertEqual(slider.value, (75, 75))
        self.pointer(slider, "pointer_move", -10)
        self.assertEqual(slider.value, (0, 75))
        self.pointer(slider, "pointer_up", 20)
        self.assertEqual(slider.value, (20, 75))
        self.assertFalse(slider._pressed)
        self.pointer(slider, "pointer_down", 85)
        self.pointer(slider, "pointer_move", 10)
        self.assertEqual(slider.value, (20, 20))
        self.pointer(slider, "pointer_up", 110)
        self.assertEqual(slider.value, (20, 100))

    def test_coincident_thumbs_can_be_separated_in_both_directions(self):
        slider = self.slider(value=(50, 50))
        self.pointer(slider, "pointer_down", 49)
        self.pointer(slider, "pointer_up", 20)
        self.assertEqual(slider.value, (20, 50))
        slider.value = (50, 50)
        self.pointer(slider, "pointer_down", 51)
        self.pointer(slider, "pointer_up", 80)
        self.assertEqual(slider.value, (50, 80))

    def test_step_uses_minimum_origin_and_reaches_nonmultiple_maximum(self):
        slider = self.slider(minimum=3, maximum=14, value=(3, 14), step=4)
        self.pointer(slider, "pointer_down", 8)
        self.assertEqual(slider.value, (7, 14))
        self.pointer(slider, "pointer_up", 3)
        self.pointer(slider, "pointer_down", 13)
        self.assertEqual(slider.value, (3, 11))
        self.pointer(slider, "pointer_up", 14)
        self.assertEqual(slider.value, (3, 14))

    def test_pointer_identity_button_and_cancel_preserve_ownership(self):
        slider = self.slider()
        self.pointer(slider, "pointer_down", 30, button=3)
        self.assertEqual(slider.value, (25, 75))
        self.pointer(slider, "pointer_down", 30)
        for kind in ("pointer_down", "pointer_move", "pointer_up", "pointer_cancel"):
            self.pointer(slider, kind, 90, pointer_id=8)
            self.assertEqual(slider.value, (30, 75))
            self.assertTrue(slider._pressed)
        self.pointer(slider, "pointer_up", 50, button=3)
        self.assertTrue(slider._pressed)
        self.pointer(slider, "pointer_cancel", 30)
        self.pointer(slider, "pointer_move", 50)
        self.pointer(slider, "pointer_up", 50)
        self.assertEqual(slider.value, (30, 75))
        self.assertFalse(slider._pressed)

    def test_keyboard_selects_both_thumbs_without_intercepting_tab(self):
        slider = self.slider(step=5)
        for key, expected in (
            ("ArrowRight", (30, 75)), ("ArrowUp", (35, 75)),
            ("ArrowDown", (30, 75)), ("Home", (0, 75)),
            ("PageUp", (50, 75)), ("End", (75, 75)),
            ("ArrowRight", (75, 75)), ("Home", (0, 75)),
            ("Space", (0, 75)), ("End", (0, 100)),
            ("ArrowRight", (0, 100)), ("PageDown", (0, 50)),
            ("Home", (0, 0)), ("Enter", (0, 0)), ("Tab", (0, 0)),
        ):
            slider.handle_input(Input("key_down", key=key))
            self.assertEqual(slider.value, expected, key)
        self.assertEqual(slider.active_thumb, "lower")
        self.pointer(slider, "pointer_down", 80)
        slider.handle_input(Input("key_down", key="Home"))
        self.pointer(slider, "pointer_up", 90)
        self.assertEqual(slider.value, (0, 0))

    def test_read_only_disabled_hidden_and_blur_cancel_drag(self):
        for setting, value in (("read_only", True), ("enabled", False), ("visible", False)):
            slider = self.slider()
            self.pointer(slider, "pointer_down", 30)
            setattr(slider, setting, value)
            self.pointer(slider, "pointer_up", 50)
            slider.handle_input(Input("key_down", key="End"))
            self.assertEqual(slider.value, (30, 75))
            self.assertFalse(slider._pressed)
            slider.value = (20, 80)
            self.assertEqual(slider.value, (20, 80))
            slider.destroy()
        parent = Container(parent=self.app, enabled=False)
        slider = RangeSlider(parent=parent)
        slider.handle_input(Input("key_down", key="End"))
        self.assertEqual(slider.value, (25, 75))
        slider = self.slider()
        self.pointer(slider, "pointer_down", 30)
        slider.handle_input(Input("blur"))
        self.pointer(slider, "pointer_up", 50)
        self.assertEqual(slider.value, (30, 75))

    def test_atomic_updates_preserve_live_gesture_on_failure(self):
        slider = self.slider()
        self.pointer(slider, "pointer_down", 30)
        revision = slider._paint_revision
        for changes in ({"value": (80, 20)}, {"maximum": 20, "value": (10, 30)},
                        {"step": 0}, {"value": (True, 50)}, {"value": (float("nan"), 75)},
                        {"value": (10, 20, 30)}, {"active_thumb": "middle"}):
            with self.assertRaises((TypeError, ValueError)):
                slider.update(enabled=False, **changes)
            self.assertEqual(slider.value, (30, 75))
            self.assertTrue(slider.enabled)
            self.assertTrue(slider._pressed)
            self.assertEqual(slider._paint_revision, revision)
        slider.update(minimum=200, maximum=400, value=(225, 375))
        self.assertEqual(slider.value, (225, 375))
        self.assertFalse(slider._pressed)
        self.pointer(slider, "pointer_up", 390)
        self.assertEqual(slider.value, (225, 375))

    def test_set_range_clamps_both_endpoints_and_validates_before_commit(self):
        slider = self.slider()
        slider.set_range(40, 60)
        self.assertEqual(slider.value, (40, 60))
        slider.set_range(80, 90)
        self.assertEqual(slider.value, (80, 80))
        slider.set_range(-20, -10, value=(-18, -12))
        self.assertEqual(slider.value, (-18, -12))
        revision = slider._paint_revision
        for low, high, pair in ((-10, -20, (-18, -12)), (0, 5, (8, 10)),
                               (True, 5, (1, 2)), (0, float("inf"), (1, 2))):
            with self.assertRaises((TypeError, ValueError)):
                slider.set_range(low, high, value=pair)
            self.assertEqual(slider.value, (-18, -12))
            self.assertEqual(slider._paint_revision, revision)

    def test_style_fallbacks_visible_labels_and_concrete_overrides(self):
        theme = (Theme().styled(Slider, Style(fill="#112233"), part="fill")
                 .styled(RangeSlider, Style(fill="#445566"), part="thumb")
                 .styled(ProgressBar, Style(fill="#778899"), part="fill"))
        slider = self.slider(theme_override=theme, lower_label="Min", upper_label="Max")
        self.assertEqual(resolve_style(slider, "fill").fill, "#112233")
        self.assertEqual(resolve_style(slider, "thumb").fill, "#445566")
        self.paint()
        self.assertEqual(self.host.texts, ["Min: 25", "Max: 75"])
        fill_rects = [args[0] for args, _ in self.host.rects if args[1] == "#112233"]
        self.assertEqual(len(fill_rects), 1)
        self.assertEqual(fill_rects[0].width, 100)
        slider.show_values = False
        self.paint()
        self.assertEqual(self.host.texts, [])
        ring = self.ring(theme_override=theme, value=50)
        self.assertEqual(resolve_style(ring, "fill").fill, "#778899")

    def test_instances_blueprints_and_destroy_have_independent_lifetimes(self):
        class Form(Container):
            interval = blueprint(RangeSlider, value=(10, 40))
            progress = blueprint(CircularProgress, value=40)

        first, second = Form(parent=self.app), Form(parent=self.app)
        first.interval.handle_input(Input("key_down", key="ArrowRight"))
        first.progress.value = 70
        self.assertEqual((second.interval.value, second.progress.value), ((10, 40), 40))
        slider = self.slider()
        self.pointer(slider, "pointer_down", 30)
        slider.destroy()
        self.assertFalse(slider._pressed)
        slider.destroy()
        with self.assertRaises(LifecycleError):
            slider.handle_input(Input("key_down", key="End"))

    def test_ring_validation_and_atomic_range_updates(self):
        ring = self.ring(value=75)
        ring.set_range(50)
        self.assertEqual((ring.maximum, ring.value), (50, 50))
        ring.update(maximum=300, value=250)
        for changes in ({"maximum": 0}, {"value": -1}, {"value": 301},
                        {"thickness": 0}, {"value": float("inf")}, {"value": True}):
            with self.assertRaises((TypeError, ValueError)):
                ring.update(**changes)
            self.assertEqual((ring.maximum, ring.value), (300, 250))
        with self.assertRaises(ValueError):
            ring.set_range(20, value=30)
        self.assertEqual((ring.maximum, ring.value), (300, 250))

    def test_ring_is_clockwise_and_empty_full_and_small_rings_are_finite(self):
        theme = (Theme().styled(CircularProgress, Style(fill="#112233"), part="track")
                 .styled(CircularProgress, Style(fill="#445566"), part="fill")
                 .styled(CircularProgress, Style(foreground="#abcdef")))
        ring = self.ring(value=25, width=100, height=100, theme_override=theme)
        self.paint()
        fill = [line for line in self.host.lines_drawn if line[4] == "#445566"]
        self.assertAlmostEqual(fill[0][0], 70)
        self.assertLess(fill[0][1], 70)
        self.assertGreater(fill[-1][2], 70)
        self.assertAlmostEqual(fill[-1][3], 70)
        self.assertIn("25%", self.host.texts)
        self.assertEqual(self.host.text_calls[0][1][2], "#abcdef")
        ring.value = 0
        self.paint()
        self.assertFalse(any(line[4] == "#445566" for line in self.host.lines_drawn))
        ring.value = 100
        self.paint()
        track = [line[:4] for line in self.host.lines_drawn if line[4] == "#112233"]
        fill = [line[:4] for line in self.host.lines_drawn if line[4] == "#445566"]
        self.assertEqual(track, fill)
        for dimension in (0, 1, 4, 12, 20):
            ring.update(width=dimension, height=dimension, thickness=30)
            arrange(self.app, self.host)
            self.paint()
            self.assertEqual(self.host.texts, [])
            self.assertTrue(all(isfinite(item) for line in self.host.lines_drawn
                                for item in (*line[:4], line[5])))

    def test_ring_uses_bounded_tessellation_and_show_text_is_optional(self):
        ring = self.ring(value=50, width=10000, height=10000, show_text=False)
        self.paint()
        self.assertLessEqual(len(self.host.lines_drawn), 384)
        self.assertLessEqual(len(self.host.lines_drawn) + len(self.host.rects), 769)
        self.assertEqual(self.host.texts, [])
        ring.enabled = False
        ring.value = 80
        self.assertEqual(ring.value, 80)

    def test_ring_joins_and_caps_cover_gaps_between_independent_strokes(self):
        theme = (Theme().styled(CircularProgress, Style(fill="#112233"), part="track")
                 .styled(CircularProgress, Style(fill="#445566"), part="fill"))
        self.ring(value=68, width=100, height=100, thickness=6, theme_override=theme)
        self.paint()
        for color in ("#112233", "#445566"):
            strokes = [line for line in self.host.lines_drawn if line[4] == color]
            caps = [args for args, _ in self.host.rects if args[1] == color]
            centers = {(round(args[0].x + args[0].width / 2, 8),
                        round(args[0].y + args[0].height / 2, 8)) for args in caps}
            self.assertTrue(strokes)
            # Every independent line endpoint is covered by a round disk as
            # wide as the stroke, including where the full track closes.
            for line in strokes:
                for x, y in ((line[0], line[1]), (line[2], line[3])):
                    self.assertIn((round(x, 8), round(y, 8)), centers)
            for args in caps:
                self.assertEqual((args[0].width, args[0].height, args[2]), (6, 6, 3))

    def test_svg_and_character_cell_hosts_render_labels_and_progress(self):
        slider = self.slider(lower_label="Min", upper_label="Max")
        ring = self.ring(value=68, width=100, height=100)
        ring.left = 280
        arrange(self.app, self.host)
        scene = SVGRenderer()
        scene.reset_scene("Interval indicators", 500, 250)
        cells = CellRenderer(64, 16)
        self.addCleanup(cells.close)
        self.addCleanup(scene.close_scene)
        for host in (scene, cells):
            host.begin(self.app.theme.tokens.background)
            for control in (slider, ring):
                host.clip(control.bounds)
                control.paint(Painter(host, control.bounds, control.effective_theme, control))
        scene.publish_scene()
        xml = ET.fromstring(scene.export_svg())
        text = [node.text for node in xml.findall(".//{http://www.w3.org/2000/svg}text")]
        self.assertEqual(text, ["Min: 25", "Max: 75", "68%"])
        rendered = "".join(cell.text for cell in cells.cells)
        self.assertIn("Min: 25", rendered)
        self.assertIn("Max: 75", rendered)
        self.assertIn("68%", rendered)


class LiveIntervalIndicatorTests(AsyncUIOwnerTestCase):
    async def test_routed_tuple_events_focus_and_keyboard_thumb_selection(self):
        app, host = App(width=400, height=180), IndicatorHost()
        slider = RangeSlider(parent=app, left=10, top=10, width=218)
        seen = []
        slider.changed.connect(seen.append)
        try:
            app.run(backend=host)
            runtime = app._runtime
            _, _, y = slider._track()
            for kind in ("pointer_down", "pointer_up"):
                runtime.router.process(Input(kind, 79, slider.bounds.y + y))
            self.assertTrue(slider.focused)
            runtime.router.process(Input("key_down", key="Space"))
            runtime.router.process(Input("key_down", key="End"))
            await runtime.dispatcher.drain()
            self.assertEqual([(event.old_value, event.new_value) for event in seen],
                             [((25, 75), (30, 75)), ((30, 75), (30, 100))])
            self.assertTrue(all(event.origin == "user" for event in seen))
            self.assertTrue(all(event.property_name == "value" for event in seen))
            slider.value = (20, 80)
            await runtime.dispatcher.drain()
            self.assertEqual(seen[-1].origin, "program")
        finally:
            await app.destroy_async()
