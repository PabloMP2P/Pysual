"""Step overflow stays reachable through wheel, scrollbar, and keyboard focus."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase
from test_library import RecordingHost, eventually

from pysual import App, Step, Stepper, TextBox
from pysual.host import Input
from pysual.painting import resolve_style
from pysual.runtime import Runtime


class StepperScrollingTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=400, height=340)
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.wizard = Stepper(parent=self.app, width=375, height=240, linear=False)
        self.steps = [Step(parent=self.wizard, title=f"Step {index + 1}") for index in range(12)]
        self.fields = [TextBox(parent=self.steps[0], text=f"Value {index}", height=40)
                       for index in range(20)]
        TextBox(parent=self.steps[-1], text="Last step", height=40)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.app.close()
        await asyncio.wait_for(self.task, 2)

    async def settle(self):
        frame = self.host.frames
        self.wizard.invalidate()
        await eventually(lambda: self.host.frames > frame)

    async def input(self, *events):
        self.host.events.extend(events)
        await eventually(lambda: not self.host.events)
        await self.settle()

    async def wheel(self, delta):
        area = self.wizard.content_bounds(self.wizard.bounds)
        await self.input(Input("wheel", area.x + 80, area.y + 120, delta=delta))

    def header(self, index):
        return self.wizard._geometry(self.wizard.content_bounds(self.wizard.bounds))[0][index]

    async def test_horizontal_active_step_scrolls_without_moving_headings(self):
        header = self.header(0)
        step = self.steps[0]
        self.assertGreater(step._scroll_limits[1], 0)
        self.assertEqual(self.wizard._scroll_limits[1], 0)
        await self.wheel(1000)
        self.assertEqual(step.scroll_y, step._scroll_limits[1])
        self.assertEqual(self.header(0), header)
        self.assertEqual(self.fields[-1]._clip, self.fields[-1].bounds)
        self.assertEqual(self.fields[0].text, "Value 0")

    async def test_vertical_many_headings_never_collapse_active_content(self):
        self.wizard.orientation = "vertical"
        await self.settle()
        step = self.steps[0]
        self.assertGreater(step.bounds.height, self.wizard.bounds.height)
        self.assertEqual(step._scroll_limits[1], 0)
        self.assertTrue(all(self.header(i).height == 72 for i in range(12)))
        self.assertGreater(self.wizard._scroll_limits[1], 0)
        await self.wheel(1000)
        self.assertEqual(self.wizard.scroll_y, self.wizard._scroll_limits[1])
        last = self.header(11)
        self.assertGreaterEqual(last.y, self.wizard.bounds.y)
        self.assertLessEqual(last.bottom, self.wizard.bounds.bottom)
        await self.input(Input("pointer_down", last.x + 28, last.y + 36),
                         Input("pointer_up", last.x + 28, last.y + 36))
        await eventually(lambda: self.wizard.selected_index == 11)
        await self.settle()
        self.assertGreaterEqual(self.header(11).y, self.wizard.bounds.y)
        self.assertEqual(self.steps[-1].children[0]._clip, self.steps[-1].children[0].bounds)
        self.assertEqual(self.fields[0].text, "Value 0")

    async def test_keyboard_reveals_selected_header_and_focused_fields(self):
        for orientation in ("horizontal", "vertical"):
            with self.subTest(orientation=orientation):
                self.wizard.orientation = orientation
                self.wizard.selected_index = 0
                await self.settle()
                self.fields[-1].focus()
                await eventually(lambda: self.fields[-1]._clip == self.fields[-1].bounds)
                self.assertIs(self.runtime.router.focus, self.fields[-1])
                self.fields[0].focus()
                await eventually(lambda: self.fields[0]._clip == self.fields[0].bounds)
                self.wizard.focus()
                await self.input(Input("key_down", key="End"))
                await eventually(lambda: self.wizard.selected_index == 11)
                await self.settle()
                self.assertGreaterEqual(self.header(11).y, self.wizard.bounds.y)
                self.assertLessEqual(self.header(11).bottom, self.wizard.bounds.bottom)
                await self.input(Input("key_down", key="Home"))
                await eventually(lambda: self.wizard.selected_index == 0)
                await self.settle()
                self.assertEqual(self.wizard.scroll_y, 0)

    async def test_rejected_current_step_is_revealed_after_scrolling_to_later_heading(self):
        self.wizard.orientation = "vertical"
        self.steps[-1].requires = [self.steps[0]]
        self.steps[0].validating.connect(lambda event: event.reject("Correct the first field"))
        await self.settle()
        await self.wheel(1000)
        last = self.header(11)
        self.assertLess(self.header(0).bottom, self.wizard.bounds.y)
        await self.input(Input("pointer_down", last.x + 28, last.y + 36),
                         Input("pointer_up", last.x + 28, last.y + 36))
        await eventually(lambda: bool(self.steps[0].error))
        await self.settle()
        self.assertEqual(self.wizard.selected_index, 0)
        self.assertGreaterEqual(self.header(0).y, self.wizard.bounds.y)
        self.assertEqual(self.fields[0]._clip, self.fields[0].bounds)

    async def test_changing_orientation_keeps_selected_later_step_visible(self):
        self.wizard.selected_index = 11
        await self.settle()
        for orientation in ("vertical", "horizontal"):
            with self.subTest(orientation=orientation):
                self.wizard.orientation = orientation
                await self.settle()
                area = self.wizard.content_bounds(self.wizard.bounds)
                header = self.header(11)
                self.assertEqual(self.wizard.selected_index, 11)
                self.assertGreaterEqual(header.y, area.y)
                self.assertLessEqual(header.bottom, area.bottom)
                field = self.steps[-1].children[0]
                self.assertEqual(field._clip, field.bounds)
                self.assertEqual(field.text, "Last step")

    async def test_scrollbar_drag_moves_the_correct_viewport_and_is_visible(self):
        for orientation in ("horizontal", "vertical"):
            with self.subTest(orientation=orientation):
                self.wizard.orientation = orientation
                await self.settle()
                scroller = self.wizard if orientation == "vertical" else self.steps[0]
                bar, = scroller._scrollbars()
                self.assertNotEqual(resolve_style(scroller, "thumb").fill, "#00000000")
                x, y = bar.thumb.x + bar.thumb.width / 2, bar.thumb.y + 3
                await self.input(Input("pointer_down", x, y, pointer_id=4),
                                 Input("pointer_move", x, bar.track.bottom, pointer_id=4),
                                 Input("pointer_up", x, bar.track.bottom, pointer_id=4))
                self.assertEqual(scroller.scroll_y, scroller._scroll_limits[1])
                self.assertEqual(self.wizard.selected_index, 0)
                self.assertIsNone(scroller._scroll_drag)

    async def test_short_horizontal_viewport_can_scroll_to_usable_content(self):
        self.wizard.height = 60
        await self.settle()
        self.assertGreater(self.steps[0].bounds.height, 0)
        self.assertGreater(self.wizard._scroll_limits[1], 0)
        self.fields[0].focus()
        await eventually(lambda: self.fields[0]._clip == self.fields[0].bounds)

    async def test_resize_and_reset_clamp_scroll_without_losing_field_values(self):
        self.wizard.orientation = "vertical"
        await self.settle()
        await self.wheel(1000)
        self.assertGreater(self.wizard.scroll_y, 0)
        self.wizard.height = 3000
        await self.settle()
        self.assertEqual(self.wizard.scroll_y, 0)
        self.assertEqual(self.wizard._scroll_axes, (False, False))
        self.wizard.height = 240
        await self.settle()
        await self.wheel(1000)
        self.wizard.reset()
        await self.settle()
        self.assertEqual(self.wizard.scroll_y, 0)
        self.assertEqual(self.fields[0].text, "Value 0")
