"""Shadow overhang survives layout wrappers without escaping real viewports."""

import importlib.util
import os
import unittest
from unittest.mock import patch

from test_library import RecordingHost

from pysual import App, Button, CheckBox, Container, ScrollArea, Slider, Style, SubWindow, Theme
from pysual.input import Router
from pysual.layout import arrange
from pysual.painting import Painter, paint_tree
from pysual.render_cache import RenderCache


class EffectClippingTests(unittest.TestCase):
    def setUp(self):
        self.app = App(
            theme=Theme().styled(
                Button,
                Style(
                    fill="#ffffff",
                    radius=8,
                    shadow="#000000a0",
                    shadow_blur=12,
                    shadow_y=4,
                ),
            )
        )
        self.addCleanup(self.app.destroy)
        self.panel = Container(
            parent=self.app,
            left=30,
            top=30,
            width=200,
            height=100,
            padding=12,
            background="#eeeeee",
        )
        self.row = Container(
            parent=self.panel,
            top=16,
            width=176,
            height=36,
            layout="stack",
            direction="horizontal",
        )
        self.button = Button(parent=self.row, text="Depth", width=110, height=36)

    def test_nested_layout_wrappers_preserve_effects_and_hit_regions(self):
        arrange(self.app, RecordingHost())
        bounds = Painter(
            RecordingHost(), self.button.bounds, self.app.theme, self.button
        ).effect_bounds()
        self.assertLess(bounds.y, self.row.bounds.y)
        self.assertGreater(bounds.bottom, self.row.bounds.bottom)
        self.assertEqual(bounds.x, self.panel.bounds.x)
        self.assertEqual(self.button._clip, self.button.bounds)
        self.assertIs(
            Router(None).hit(self.app, self.button.bounds.x + 10, bounds.y + 1),
            self.panel,
        )

    def test_effect_preparation_skips_controls_and_parts_without_effect_rules(self):
        from pysual import painting

        slider = Slider(parent=self.app, left=300, top=40, width=120, height=38)
        host = RecordingHost()
        arrange(self.app, host)
        # A Button rule must not make every Slider prepare eight part states.
        with patch.object(painting, "resolve_style", wraps=painting.resolve_style) as resolve:
            self.assertEqual(Painter(host, slider.bounds, self.app.theme, slider).effect_bounds(), slider.bounds)
            self.assertEqual(resolve.call_count, 0)
        self.app.theme = self.app.theme.styled(
            Slider, Style(shadow="#ffffff", shadow_blur=9), part="thumb", state="selected"
        )
        arrange(self.app, host)
        with patch.object(painting, "resolve_style", wraps=painting.resolve_style) as resolve:
            bounds = Painter(host, slider.bounds, self.app.theme, slider).effect_bounds()
            self.assertEqual(bounds.x, slider.bounds.x - 9)
            self.assertEqual({call.args[1] for call in resolve.call_args_list}, {"thumb"})
            self.assertTrue(any(call.kwargs.get("selected") for call in resolve.call_args_list))

    def test_effect_candidates_preserve_inherited_custom_parts_and_outgoing_motion(self):
        from pysual import Control
        from pysual.motion import _Transition
        from pysual.painting import resolve_style

        class Badge(Control):
            style_parts = ("body", "badge")

            def paint_bounds(self):
                return self.bounds.inset(-4)

        class SpecializedBadge(Badge):
            pass

        self.app.theme = Theme().styled(
            Badge, Style(glow="#ffffff", glow_width=10), part="badge", state="hover"
        )
        badge = SpecializedBadge(parent=self.app, left=300, top=40, width=120, height=38)
        host = RecordingHost()
        arrange(self.app, host)
        first = resolve_style(badge, "badge", state="hover")
        last = resolve_style(badge, "badge", state="normal")
        badge._motion = _Transition(badge, ("normal", False), {"badge": (first, last)}, 0, 0)
        badge._motion.progress = .5
        original = self.app.theme.to_json()
        bounds = Painter(host, badge.bounds, self.app.theme, badge).effect_bounds()
        self.assertLess(bounds.x, badge.bounds.x - 4)
        self.assertEqual(self.app.theme.to_json(), original)

    def test_adding_and_removing_a_surface_updates_descendant_effect_clips(self):
        host = RecordingHost()
        arrange(self.app, host)
        self.assertEqual(self.button._paint_clip, self.panel.bounds)
        self.row.background = "#eeeeee"
        arrange(self.app, host)
        self.assertEqual(self.button._paint_clip, self.row.bounds)
        self.row.background = None
        arrange(self.app, host)
        self.assertEqual(self.button._paint_clip, self.panel.bounds)

    def test_scroll_areas_and_custom_content_bounds_keep_effects_in_the_viewport(self):
        for kind in (ScrollArea, SubWindow):
            with self.subTest(kind=kind.__name__):
                viewport = kind(
                    parent=self.app,
                    left=250,
                    top=30,
                    width=200,
                    height=130,
                    padding=12,
                )
                row = Container(parent=viewport, width=176, height=36)
                button = Button(parent=row, width=110, height=36)
                arrange(self.app, RecordingHost())
                self.assertEqual(
                    button._paint_clip, viewport.content_bounds(viewport.bounds)
                )
                viewport.destroy()

    def test_painted_child_uses_its_visible_boundary_inside_a_narrow_wrapper(self):
        wrapper = Container(parent=self.panel, width=90, height=70)
        surface = Container(
            parent=wrapper, width=150, height=70, background="#ffffff"
        )
        button = Button(parent=surface, left=60, width=25, height=30)
        host = RecordingHost()
        arrange(self.app, host)
        self.assertLess(surface._clip.width, surface.bounds.width)
        self.assertEqual(button._paint_clip, surface._clip)
        effect = Painter(host, button.bounds, self.app.theme, button).effect_bounds()
        self.assertLessEqual(effect.right, surface._clip.right)
