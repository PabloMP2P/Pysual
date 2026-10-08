"""Keyboard focus remains visible through the shared scrolling layout."""

import asyncio
import unittest
from _ui_testcase import UIOwnerTestCase, AsyncUIOwnerTestCase
from unittest.mock import patch

from pysual import App, Button, Container, Popup, ScrollArea, TextBox
from pysual.host import Input
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


class FocusNavigationTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=640, height=400)
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task

    def frame(self):
        self.runtime._paint_frame()

    def test_tab_and_reverse_tab_reveal_both_axes_with_minimum_scroll(self):
        area = ScrollArea(parent=self.app, width=200, height=120, padding=10)
        first = Button(parent=area, width=80, height=30)
        last = TextBox(parent=area, left=250, top=200, width=80, height=30)
        self.frame()
        self.runtime.router.process(Input("key_down", key="Tab"))
        self.frame()
        self.assertIs(self.runtime.router.focus, first)
        self.assertEqual((area.scroll_x, area.scroll_y), (0, 0))
        viewport = area.content_bounds(area.bounds)
        expected_scroll = (
            last.bounds.right - viewport.right,
            last.bounds.bottom - viewport.bottom,
        )
        self.runtime.router.process(Input("key_down", key="Tab"))
        self.frame()
        self.assertIs(self.runtime.router.focus, last)
        self.assertEqual((area.scroll_x, area.scroll_y), expected_scroll)
        self.assertEqual((last.bounds.right, last.bounds.bottom),
                         (viewport.right, viewport.bottom))
        self.assertEqual(last._clip, last.bounds)
        self.assertEqual(self.host.text_target, last.bounds)
        self.runtime.router.process(Input("key_down", key="Tab", shift=True))
        self.frame()
        self.assertIs(self.runtime.router.focus, first)
        self.assertEqual((area.scroll_x, area.scroll_y), (0, 0))
        self.assertEqual(first._clip, first.bounds)

    def test_nested_scroll_areas_reveal_through_intermediate_container(self):
        outer = ScrollArea(parent=self.app, width=220, height=160, padding=10)
        wrapper = Container(parent=outer, left=250, top=230, width=180, height=120)
        inner = ScrollArea(parent=wrapper, width=180, height=120, padding=8)
        target = Button(parent=inner, left=260, top=270, width=80, height=30)
        target.focus()
        self.frame()
        self.assertIs(self.runtime.router.focus, target)
        self.assertGreater(inner.scroll_x, 0)
        self.assertGreater(inner.scroll_y, 0)
        self.assertGreater(outer.scroll_x, 0)
        self.assertGreater(outer.scroll_y, 0)
        self.assertEqual(target._clip, target.bounds)

    def test_focus_uses_pending_layout_and_can_reveal_the_same_control_again(self):
        area = ScrollArea(parent=self.app, width=200, height=120)
        target = Button(parent=area, top=200, width=100, height=30)
        self.frame()
        target.focus()
        target.top = 400
        self.frame()
        self.assertEqual(area.scroll_y, 310)
        self.assertEqual(target._clip, target.bounds)
        area.scroll_y = 0
        self.frame()
        self.frame()
        self.assertEqual(area.scroll_y, 0)
        target.focus()
        self.frame()
        self.assertEqual(area.scroll_y, 310)

    def test_invalidated_focus_requests_do_not_scroll(self):
        for invalidation in ("hidden", "disabled", "disposed", "not_focusable"):
            with self.subTest(invalidation=invalidation):
                area = ScrollArea(parent=self.app, width=200, height=120)
                target = Button(parent=area, top=300, width=80, height=30)
                target.focus()
                if invalidation == "hidden":
                    target.visible = False
                elif invalidation == "disabled":
                    area.enabled = False
                elif invalidation == "disposed":
                    target.destroy()
                else:
                    target.focusable = False
                self.frame()
                self.assertIsNone(self.runtime.router.focus)
                self.assertEqual(area.scroll_y, 0)
                area.destroy()

    def test_non_scrollable_clipping_does_not_move_an_outer_scroll_area(self):
        outer = ScrollArea(parent=self.app, width=200, height=120)
        wrapper = Container(parent=outer, top=300, width=160, height=80)
        target = Button(parent=wrapper, top=150, width=100, height=30)
        target.focus()
        self.frame()
        self.assertEqual(outer.scroll_y, 0)
        self.assertIs(self.runtime.router.focus, target)
        self.assertEqual(target._clip.height, 0)

    def test_latest_focus_request_wins_before_the_next_frame(self):
        area = ScrollArea(parent=self.app, width=200, height=120)
        first = Button(parent=area, width=100, height=30)
        last = Button(parent=area, top=300, width=100, height=30)
        last.focus()
        first.focus()
        self.frame()
        self.assertIs(self.runtime.router.focus, first)
        self.assertEqual(area.scroll_y, 0)

    def test_pointer_focus_and_popup_restore_preserve_partial_visibility(self):
        area = ScrollArea(parent=self.app, width=200, height=120)
        target = Button(parent=area, top=100, width=100, height=40)
        self.frame()
        self.runtime.router.process(Input("pointer_down", x=20, y=110))
        self.runtime.router.process(Input("pointer_up", x=20, y=110))
        self.frame()
        self.assertIs(self.runtime.router.focus, target)
        self.assertEqual(area.scroll_y, 0)
        popup = Popup(width=100, height=60)
        Button(parent=popup, text="Action")
        popup.show(target)
        self.frame()
        popup.dismiss()
        self.frame()
        self.assertIs(self.runtime.router.focus, target)
        self.assertEqual(area.scroll_y, 0)
        target.focus()
        self.frame()
        self.assertEqual(area.scroll_y, 20)

    def test_oversized_focus_uses_nearest_edge_and_does_not_oscillate(self):
        area = ScrollArea(parent=self.app, width=160, height=120)
        target = Button(parent=area, left=250, top=200, width=300, height=240)
        target.focus()
        self.frame()
        self.assertEqual((area.scroll_x, area.scroll_y), (250, 200))
        self.assertEqual(target._clip, area.content_bounds(area.bounds))
        area.scroll_x, area.scroll_y = 300, 250
        target.focus()
        self.frame()
        self.assertEqual((area.scroll_x, area.scroll_y), (300, 250))
        self.frame()
        self.assertEqual((area.scroll_x, area.scroll_y), (300, 250))


class FocusTimingTests(UIOwnerTestCase):
    def test_reveal_layout_is_profiled_once_and_idle_focus_adds_no_clock_reads(self):
        app, host = App(profile_frames=True), RecordingHost()
        runtime = Runtime(app, host)
        app._runtime = runtime
        try:
            area = ScrollArea(parent=app, width=200, height=120)
            target = Button(parent=area, top=300, width=100, height=30)
            runtime._paint_frame()
            app.drain_frame_timings()
            target.focus()
            with patch(
                "pysual.runtime.perf_counter",
                side_effect=[10, 10.001, 10.004, 10.005, 10.009],
            ):
                runtime._paint_frame()
            (revealed,) = app.drain_frame_timings()
            self.assertAlmostEqual(revealed.layout_seconds, 0.003)
            self.assertAlmostEqual(revealed.paint_seconds, 0.004)
            self.assertEqual(target._clip, target.bounds)
            with patch("pysual.runtime.perf_counter", side_effect=[20, 20.001, 20.004]):
                runtime._paint_frame()
            (idle,) = app.drain_frame_timings()
            self.assertEqual(idle.layout_seconds, 0)
            self.assertAlmostEqual(idle.paint_seconds, 0.003)
        finally:
            app._runtime = None
            app.destroy()


if __name__ == "__main__":
    unittest.main()
