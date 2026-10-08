"""Interaction motion is finite, interruptible, and independent of layout/FPS."""

import unittest
from _ui_testcase import UIOwnerTestCase
from math import inf
from unittest.mock import patch

from pysual import App, Button, Container, Dirty, Style, Theme, Toggle, get_theme
from pysual.host import Input
from pysual.painting import Painter, RetainedPaintTree, resolve_style
from pysual.runtime import Runtime
from test_library import RecordingHost
from test_retained_paint import PatchRecordingHost


class MotionTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=400, height=220, fps_limit=None)
        self.button = Button(parent=self.app, text="Action", width=120, height=40)
        self.other = Button(parent=self.app, left=160, width=120, height=40)
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.app._attach_dispatcher(self.runtime.dispatcher)
        self.app._wake = self.runtime.invalidate
        self.runtime._paint_frame()

    def tearDown(self):
        self.runtime.motion.clear()
        self.app._runtime = None
        self.app.destroy()

    def event(self, kind, x=10, y=10, *, now=100, **kwargs):
        with patch("pysual.motion.perf_counter", return_value=now):
            self.runtime.router.process(Input(kind, x, y, **kwargs))

    def style(self, control=None):
        control = control or self.button
        return Painter(self.host, control.bounds, control.effective_theme, control).style()

    def test_hover_blends_then_releases_all_work_and_keeps_layout_stable(self):
        normal, bounds = self.style(), self.button.bounds
        self.event("pointer_move")
        target = resolve_style(self.button)
        self.assertEqual(self.style(), normal)
        self.runtime.motion.tick(100.06)
        blended = self.style()
        self.assertNotEqual(blended.fill, normal.fill)
        self.assertNotEqual(blended.fill, target.fill)
        self.assertEqual(self.button.bounds, bounds)
        self.assertFalse(self.button.dirty & (Dirty.MEASURE | Dirty.ARRANGE))
        self.runtime._dirty = False
        self.assertAlmostEqual(self.runtime._next_frame_at(), 100.06 + 1 / 60)
        self.runtime.motion.tick(100.2)
        self.assertEqual(self.style(), target)
        self.assertIsNone(self.button._motion)
        self.assertEqual(self.runtime._next_frame_at(), inf)
        revision = self.button._paint_revision
        self.runtime.motion.tick(500)
        self.assertEqual(self.button._paint_revision, revision)

    def test_reversal_starts_from_the_last_visible_color(self):
        normal = self.style()
        self.event("pointer_move")
        self.runtime.motion.tick(100.06)
        visible = self.style()
        self.event("pointer_leave", now=100.065)
        self.assertEqual(self.style(), visible)
        self.runtime.motion.tick(100.23)
        self.assertEqual(self.style(), normal)
        self.assertIsNone(self.button._motion)

    def test_optional_light_and_shadow_layers_fade_in_and_out(self):
        self.button.theme_override = Theme().styled(
            Button,
            Style(
                glow="#70aaff80", glow_width=4, shadow="#20306090",
                shadow_blur=8, shadow_y=3, highlight="#ffffffa0",
                inner_border="#80b0ff60",
            ),
            state="hover",
        )
        self.event("pointer_move")
        target = resolve_style(self.button)
        self.runtime.motion.tick(100.04)
        for name in ("glow", "shadow", "highlight", "inner_border"):
            with self.subTest(layer=name):
                color = getattr(self.style(), name)
                self.assertEqual(color[:7], getattr(target, name)[:7])
                self.assertGreater(int(color[7:], 16), 0)
                self.assertLess(int(color[7:], 16), int(getattr(target, name)[7:], 16))
        visible = self.style()
        self.event("pointer_leave", now=100.045)
        self.assertEqual(self.style(), visible)
        self.runtime.motion.tick(100.09)
        for name in ("glow", "shadow", "highlight", "inner_border"):
            with self.subTest(layer=name):
                color = getattr(self.style(), name)
                self.assertEqual(color[:7], getattr(target, name)[:7])
                self.assertGreater(int(color[7:], 16), 0)
                self.assertLess(int(color[7:], 16), int(getattr(visible, name)[7:], 16))
        self.runtime.motion.tick(100.21)
        self.assertIsNone(self.style().glow)
        self.assertIsNone(self.style().shadow)
        self.assertEqual(self.runtime.motion.next_frame_at, inf)

    def test_gradient_endpoints_start_and_finish_at_the_solid_face_color(self):
        self.button.theme_override = Theme().styled(
            Button, Style(fill="#404040", border="#606060")
        ).styled(
            Button,
            Style(fill="#404040", fill_end="#808080", border_end="#a0a0a0"),
            state="hover",
        )
        self.event("pointer_move")
        self.runtime.motion.tick(100.02)
        entering = self.style()
        self.assertTrue(0x40 < int(entering.fill_end[1:3], 16) < 0x80)
        self.assertTrue(0x60 < int(entering.border_end[1:3], 16) < 0xa0)
        self.assertEqual(entering.fill_end[7:], "ff")
        self.runtime.motion.tick(100.2)
        self.event("pointer_leave", now=100.21)
        self.runtime.motion.tick(100.25)
        leaving = self.style()
        self.assertTrue(0x40 < int(leaving.fill_end[1:3], 16) < 0x80)
        self.assertEqual(leaving.fill_end[7:], "ff")
        self.runtime.motion.tick(100.4)
        self.assertIsNone(self.style().fill_end)

    def test_continuous_uncapped_frames_sample_motion_at_most_60_times_a_second(self):
        self.app.redraw_interval = 0
        self.event("pointer_move")
        revision = self.button._paint_revision
        for millisecond in range(1, 201):
            self.runtime.motion.tick(100 + millisecond / 1000)
        self.assertLessEqual(self.button._paint_revision - revision, 10)
        self.assertGreater(self.button._paint_revision - revision, 3)
        self.assertIsNone(self.button._motion)
        self.assertEqual(self.runtime.motion.next_frame_at, inf)

    def test_reduced_motion_snaps_current_and_future_transitions(self):
        self.event("pointer_move")
        self.runtime.motion.tick(100.05)
        self.app.reduce_motion = True
        self.assertIsNone(self.button._motion)
        self.assertEqual(self.style(), resolve_style(self.button))
        self.event("pointer_leave")
        self.assertIsNone(self.button._motion)
        self.assertEqual(self.runtime.motion.next_frame_at, inf)

    def test_hover_exit_invalidates_previous_cached_body_in_every_exit_path(self):
        self.app.reduce_motion = True
        for exit_kind in ("pointer_move", "pointer_leave", "pointer_cancel", "blur"):
            with self.subTest(exit_kind=exit_kind):
                self.event("pointer_move")
                revision = self.button._paint_revision
                self.event(exit_kind, x=170)
                self.assertFalse(self.button._hover)
                self.assertGreater(self.button._paint_revision, revision)

    def test_theme_disable_hide_and_destroy_remove_active_animations(self):
        for change in ("theme", "enabled", "visible", "destroy"):
            with self.subTest(change=change):
                control = Button(parent=self.app, width=120, height=40, top=70)
                self.runtime._paint_frame()
                self.event("pointer_move", y=80)
                self.assertIsNotNone(control._motion)
                if change == "theme":
                    control.theme_override = get_theme("winxp")
                elif change == "destroy":
                    control.destroy()
                    self.assertNotIn(control, self.runtime.motion._active)
                    self.assertIsNone(control._motion)
                else:
                    setattr(control, change, False)
                self.runtime.motion.tick(100.2)
                self.assertIsNone(control._motion)
                control.destroy()

    def test_toggle_thumb_travels_without_delaying_the_semantic_value(self):
        toggle = Toggle(parent=self.app, width=180, height=40, top=70)
        self.runtime._paint_frame()
        self.event("pointer_down", y=80)
        self.event("pointer_up", y=80, now=100.01)
        self.assertTrue(toggle.checked)
        self.assertEqual(toggle._motion.checked, 0)
        self.runtime.motion.tick(100.08)
        self.assertGreater(toggle._motion.checked, 0)
        self.assertLess(toggle._motion.checked, 1)
        self.runtime.motion.tick(100.2)
        self.assertIsNone(toggle._motion)

    def test_press_feedback_tracks_pointer_containment_and_keyboard_release(self):
        self.event("pointer_down")
        self.assertTrue(self.button._pressed)
        self.event("pointer_move", x=150)
        self.assertFalse(self.button._pressed)
        self.event("pointer_move")
        self.assertTrue(self.button._pressed)
        self.event("pointer_up")
        self.assertFalse(self.button._pressed)
        self.runtime.router.set_focus(self.button)
        self.event("key_down", key="Space")
        self.assertTrue(self.button._pressed)
        self.event("key_up", key="Space")
        self.assertFalse(self.button._pressed)
        self.event("key_down", key="Enter")
        self.runtime.router.set_focus(self.other)
        self.assertFalse(self.button._pressed)
        self.runtime.router.set_focus(self.button)
        self.event("key_down", key="Enter")
        self.event("blur")
        self.assertFalse(self.button._pressed)
        with patch.object(Button, "activate") as activate:
            self.button.handle_input(Input("pointer_up", 10, 10))
            self.button.handle_input(Input("key_up", key="Enter"))
            activate.assert_not_called()

    def test_focus_loss_and_pointer_cancellation_ease_from_the_visible_press(self):
        for exit_kind in ("focus", "pointer_cancel", "blur"):
            with self.subTest(exit_kind=exit_kind):
                self.event("pointer_down", now=100)
                self.runtime.motion.tick(100.04)
                visible = self.style()
                if exit_kind == "focus":
                    with patch("pysual.motion.perf_counter", return_value=100.045):
                        self.runtime.router.set_focus(self.other)
                else:
                    self.event(exit_kind, now=100.045)
                self.assertFalse(self.button._pressed)
                self.assertEqual(self.style(), visible)
                self.runtime.motion.tick(100.21)
                self.assertEqual(self.style(), resolve_style(self.button))
                self.assertIsNone(self.button._motion)
                self.runtime.router.cancel_capture()

    def test_cap_and_suspend_are_respected_without_creating_app_background_motion(self):
        self.event("pointer_move", x=350, y=180)
        self.assertIsNone(self.app._motion)
        self.event("pointer_move")
        self.runtime._dirty = False
        self.runtime._last_frame_started = 100
        self.app.fps_limit = 10
        self.runtime._dirty = False
        self.assertEqual(self.runtime._next_frame_at(), 100.1)
        self.runtime._suspend(True)
        self.assertEqual(self.runtime._next_frame_at(), inf)
        self.assertIsNone(self.button._motion)


class RetainedMotionTests(UIOwnerTestCase):
    """Scene patches also need Python motion when the host does not animate."""

    def setUp(self):
        theme = Theme().styled(Button, Style(fill="#ff0000")).styled(
            Button, Style(fill="#00ff00"), state="hover"
        )
        self.app = App(width=400, height=220, fps_limit=None, theme=theme)
        self.panel = Container(parent=self.app, width=400, height=220)
        self.button = Button(parent=self.panel, width=120, height=40)
        self.toggle = Toggle(parent=self.panel, width=180, height=40, top=60)
        self.other = Button(parent=self.panel, left=200, width=120, height=40)
        self.host = PatchRecordingHost()
        self.host.native_retained = False
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.app._attach_dispatcher(self.runtime.dispatcher)
        self.app._wake = self.runtime.invalidate
        self.retained = self.runtime._retained_paint = RetainedPaintTree(self.host)
        self.frame(99)

    def tearDown(self):
        self.runtime.motion.clear()
        self.app._runtime = None
        self.app.destroy()

    def event(self, kind, *, now=100, y=10):
        with patch("pysual.motion.perf_counter", return_value=now):
            self.runtime.router.process(Input(kind, 10, y))

    def frame(self, now):
        with patch("pysual.runtime.perf_counter", return_value=now):
            self.runtime._paint_frame()

    def rectangles(self, control):
        identity = self.retained._records[control].identity
        return [args for kind, clip, args, kwargs in self.host.segments[identity]
                if kind == "rect"]

    def assert_only_updated(self, control):
        self.assertEqual(self.host.updates, [self.retained._records[control].identity])

    def test_hover_entry_and_exit_repaint_only_the_animated_segment_then_idle(self):
        stable = {control: self.host.segments[self.retained._records[control].identity]
                  for control in (self.app, self.panel, self.other, self.toggle)}
        for kind, started, initial, target in (
            ("pointer_move", 100, "#ff0000", "#00ff00"),
            ("pointer_leave", 101, "#00ff00", "#ff0000"),
        ):
            with self.subTest(kind=kind):
                self.event(kind, now=started)
                self.frame(started)
                self.assertEqual(self.rectangles(self.button)[0][1], initial)
                self.runtime._wake_event.clear()
                self.frame(started + .06)
                self.assert_only_updated(self.button)
                self.assertNotIn(self.rectangles(self.button)[0][1], (initial, target))
                self.assertFalse(self.runtime._wake_event.is_set())
                self.assertAlmostEqual(self.runtime._next_frame_at(), started + .06 + 1 / 60)
                self.frame(started + .2)
                self.assert_only_updated(self.button)
                self.assertEqual(self.rectangles(self.button)[0][1], target)
                self.assertIsNone(self.button._motion)
                self.assertEqual(self.runtime._next_frame_at(), inf)
        self.frame(500)
        self.assertEqual(self.host.updates, [])
        for control, commands in stable.items():
            self.assertEqual(self.host.segments[self.retained._records[control].identity], commands)

    def test_toggle_thumb_reaches_the_checked_position_in_retained_commands(self):
        def thumb_x():
            return next(rect.x for rect, *_ in self.rectangles(self.toggle)
                        if rect.width == rect.height == 18)

        start = thumb_x()
        self.event("pointer_down", y=70)
        self.event("pointer_up", now=100.01, y=70)
        self.assertTrue(self.toggle.checked)
        self.frame(100.01)
        self.assertEqual(thumb_x(), start)
        self.frame(100.08)
        self.assert_only_updated(self.toggle)
        self.assertGreater(thumb_x(), start)
        self.assertLess(thumb_x(), start + 19)
        self.frame(100.2)
        self.assert_only_updated(self.toggle)
        self.assertEqual(thumb_x(), start + 19)
        self.assertIsNone(self.toggle._motion)
        self.assertEqual(self.runtime._next_frame_at(), inf)

    def test_reduced_motion_submits_the_final_style_and_stops(self):
        self.event("pointer_move")
        self.frame(100.06)
        self.app.reduce_motion = True
        self.frame(100.07)
        self.assertEqual(self.rectangles(self.button)[0][1], "#00ff00")
        self.assertIsNone(self.button._motion)
        self.assertEqual(self.runtime._next_frame_at(), inf)
        self.event("pointer_leave", now=101)
        self.frame(101)
        self.assertEqual(self.rectangles(self.button)[0][1], "#ff0000")
        self.assertEqual(self.runtime._next_frame_at(), inf)

    def test_native_retained_host_submits_target_once_without_python_motion(self):
        self.host.native_retained = True
        with patch.object(self.host, "request_transition") as transition:
            self.event("pointer_move")
            transition.assert_called_once_with(.16)
        self.frame(100)
        self.assert_only_updated(self.button)
        self.assertEqual(self.rectangles(self.button)[0][1], "#00ff00")
        self.assertIsNone(self.button._motion)
        self.assertEqual(self.runtime._next_frame_at(), inf)
        self.frame(100.2)
        self.assertEqual(self.host.updates, [])


if __name__ == "__main__":
    unittest.main()
