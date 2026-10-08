"""Range interactions remain owned and progress curves stay smooth cheaply."""

from math import hypot, isfinite

from _ui_testcase import UIOwnerTestCase
from test_interval_indicators import IndicatorHost

from pysual import App, Slider, Style, Theme, Toggle
from pysual._controls.indicators import CircularProgress, RangeSlider
from pysual.errors import LifecycleError
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import Painter, paint_tree
from pysual.runtime import Runtime


class RangePolishTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=500, height=300)
        self.host = IndicatorHost()
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.addCleanup(self.app.destroy)
        self.addCleanup(setattr, self.app, "_runtime", None)

    def slider(self, **properties):
        slider = Slider(parent=self.app, left=10, top=10, width=218,
                        height=36, **properties)
        arrange(self.app, self.host)
        return slider

    def pointer(self, slider, kind, value, *, pointer_id=7, button=1):
        left, width = slider._track()
        fraction = (value - slider.minimum) / (slider.maximum - slider.minimum)
        slider.handle_input(Input(kind, slider.bounds.x + left + width * fraction,
                                  slider.bounds.y + slider.bounds.height / 2,
                                  pointer_id=pointer_id, button=button))

    def test_drag_ignores_secondary_buttons_and_unrelated_pointers(self):
        slider = self.slider(value=25)
        self.pointer(slider, "pointer_down", 60, button=3)
        self.assertEqual(slider.value, 25)
        self.pointer(slider, "pointer_down", 30)
        for kind in ("pointer_down", "pointer_move", "pointer_up", "pointer_cancel"):
            self.pointer(slider, kind, 90, pointer_id=8)
            self.assertEqual(slider.value, 30)
            self.assertTrue(slider._pressed)
        self.pointer(slider, "pointer_up", 90, button=3)
        self.assertTrue(slider._pressed)
        self.pointer(slider, "pointer_up", 50)
        self.assertEqual(slider.value, 50)
        self.assertFalse(slider._pressed)

    def test_cancel_and_blur_do_not_resume_on_a_later_move_or_release(self):
        for kind in ("pointer_cancel", "blur"):
            slider = self.slider(value=25)
            self.pointer(slider, "pointer_down", 30)
            self.pointer(slider, kind, 30)
            for tail in ("pointer_move", "pointer_up"):
                self.pointer(slider, tail, 80)
            self.assertEqual(slider.value, 30)
            self.assertFalse(slider._pressed)
            slider.destroy()

    def test_model_changes_cancel_drag_but_invalid_updates_preserve_it(self):
        slider = self.slider(value=25)
        self.pointer(slider, "pointer_down", 30)
        revision = slider._paint_revision
        with self.assertRaises(ValueError):
            slider.update(value=101, enabled=False)
        self.assertTrue(slider._pressed)
        self.assertEqual(slider._paint_revision, revision)
        slider.value = 40
        self.pointer(slider, "pointer_up", 80)
        self.assertEqual(slider.value, 40)
        for property_name in ("enabled", "visible"):
            self.pointer(slider, "pointer_down", 30)
            setattr(slider, property_name, False)
            self.pointer(slider, "pointer_up", 80)
            slider.handle_input(Input("key_down", key="End"))
            self.assertEqual(slider.value, 30)
            self.assertFalse(slider._pressed)
            setattr(slider, property_name, True)

    def test_press_and_release_repaint_even_when_value_does_not_change(self):
        slider = self.slider(value=50)
        revision = slider._paint_revision
        self.pointer(slider, "pointer_down", 50)
        self.assertGreater(slider._paint_revision, revision)
        revision = slider._paint_revision
        self.pointer(slider, "pointer_up", 50)
        self.assertGreater(slider._paint_revision, revision)
        slider.destroy()
        with self.assertRaises(LifecycleError):
            slider.handle_input(Input("key_down", key="End"))

    def test_keyboard_paging_cancels_drag_and_preserves_nondivisible_endpoints(self):
        slider = self.slider(minimum=3, maximum=94, value=7, step=4)
        for key, expected in (("PageUp", 47), ("PageDown", 7), ("Home", 3),
                              ("End", 94), ("ArrowLeft", 90)):
            slider.handle_input(Input("key_down", key=key))
            self.assertEqual(slider.value, expected)
        self.pointer(slider, "pointer_down", 30)
        slider.handle_input(Input("key_down", key="Home"))
        self.pointer(slider, "pointer_up", 80)
        self.assertEqual(slider.value, 3)
        self.pointer(slider, "pointer_down", 94)
        self.assertEqual(slider.value, 94)

    def test_large_finite_ranges_do_not_overflow_pointer_snapping(self):
        slider = self.slider(maximum=1e308, step=1e-6)
        self.pointer(slider, "pointer_down", 5e307)
        self.assertTrue(isfinite(slider.value))
        self.assertAlmostEqual(slider.value / 1e308, .5)
        with self.assertRaises(ValueError):
            slider.update(minimum=-1e308, maximum=1e308)
        self.assertEqual(slider.minimum, 0)

    def test_constrained_slider_thumb_stays_round_inside_its_bounds(self):
        slider = self.slider(value=50)
        for width, height in ((4, 36), (218, 4), (4, 4)):
            slider.update(width=width, height=height)
            arrange(self.app, self.host)
            self.host.begin("#ffffff")
            slider.paint(Painter(self.host, slider.bounds, slider.effective_theme, slider))
            filled = [args for args, _ in self.host.rects if args[1]]
            thumb = filled[-1][0]
            self.assertEqual(thumb.width, thumb.height)
            self.assertGreaterEqual(thumb.x, slider.bounds.x)
            self.assertGreaterEqual(thumb.y, slider.bounds.y)
            self.assertLessEqual(thumb.right, slider.bounds.right)
            self.assertLessEqual(thumb.bottom, slider.bounds.bottom)

    def test_range_drag_only_depresses_its_active_thumb(self):
        theme = (Theme().styled(Slider, Style(fill="#112233"), part="thumb")
                 .styled(Slider, Style(fill="#445566"), part="thumb", state="pressed"))
        slider = RangeSlider(parent=self.app, left=10, top=10, width=218,
                             theme_override=theme, show_values=False)
        arrange(self.app, self.host)
        slider.handle_input(Input("pointer_down", 79, slider.bounds.y + 18))
        paint_tree(self.app, self.host)
        faces = [args[1] for args, _ in self.host.rects
                 if args[0].width == 18 and args[0].height == 18 and args[1]]
        self.assertEqual(faces, ["#112233", "#445566"])

    def test_thumb_radius_reaches_semantic_and_graphical_renderers(self):
        class SemanticHost(IndicatorHost):
            def marker(self, rect, style, *, shape, checked=False):
                self.markers.append((rect, shape, checked))
                return True

        for control_type in (Slider, RangeSlider, Toggle):
            for radius, shape in ((0, "square"), (3, "square"), (9, "circle")):
                with self.subTest(control=control_type.__name__, radius=radius):
                    theme = Theme().styled(control_type, Style(fill="#123456", radius=radius), part="thumb")
                    control = control_type(parent=self.app, width=218, height=36, theme_override=theme)
                    if isinstance(control, RangeSlider):
                        control.show_values = False
                    arrange(self.app, self.host)
                    semantic = SemanticHost()
                    semantic.begin("#ffffff")
                    semantic.markers = []
                    control.paint(Painter(semantic, control.bounds, theme, control))
                    self.assertTrue(semantic.markers)
                    self.assertTrue(all(marker[1] == shape for marker in semantic.markers))
                    self.host.begin("#ffffff")
                    control.paint(Painter(self.host, control.bounds, theme, control))
                    faces = [args for args, _ in self.host.rects if args[1] == "#123456"]
                    self.assertEqual(len(faces), len(semantic.markers))
                    self.assertTrue(all(args[2] == radius for args in faces))
                    control.destroy()

    def test_active_square_range_thumb_uses_a_grip_instead_of_a_checkbox_tick(self):
        class SemanticHost(IndicatorHost):
            def marker(self, rect, style, *, shape, checked=False):
                self.markers.append((shape, checked))
                return True

        slider = RangeSlider(parent=self.app, width=218, height=36, show_values=False,
                             theme_override=Theme().styled(Slider, Style(radius=0, foreground="#123456"), part="thumb"))
        arrange(self.app, self.host)
        left, width, y = slider._track()
        low = left + width * slider._fractions()[0]
        slider.handle_input(Input("pointer_down", slider.bounds.x + low, slider.bounds.y + y))
        host = SemanticHost()
        host.begin("#ffffff")
        host.markers = []
        slider.paint(Painter(host, slider.bounds, slider.effective_theme, slider))
        self.assertEqual(host.markers, [("square", False), ("square", False)])
        grips = [args for args, _ in host.rects
                 if args[0].width == 2 and args[0].height == 8 and args[1] == "#123456"]
        self.assertEqual(len(grips), 1)

    def test_progress_curves_use_fewer_operations_with_subpixel_chord_error(self):
        ring = CircularProgress(parent=self.app, left=20, top=20, width=100,
                                height=100, value=68, show_text=False)
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        # A 100px ring previously submitted 244 strokes plus 245 round joints.
        # Count real portable-host output, including caps, not cached helpers.
        self.assertLessEqual(len(self.host.lines_drawn), 80)
        self.assertLessEqual(len(self.host.lines_drawn) + len(self.host.rects), 165)
        cx, cy = ring.bounds.x + 50, ring.bounds.y + 50
        radius = (98 - ring.thickness) / 2
        for x1, y1, x2, y2, _, _ in self.host.lines_drawn:
            midpoint_radius = hypot((x1 + x2) / 2 - cx, (y1 + y2) / 2 - cy)
            self.assertLessEqual(radius - midpoint_radius, .125 + 1e-9)
