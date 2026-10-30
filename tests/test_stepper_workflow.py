"""Step navigation, prerequisite validation, and asynchronous lifetime boundaries."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase
from test_library import RecordingHost, eventually

from pysual import App, Step, Stepper, TextBox
from pysual.events import Dispatcher
from pysual.host import Input
from pysual.runtime import Runtime


class StepperWorkflowTests(AsyncUIOwnerTestCase):
    def setUp(self):
        self.owner = Stepper()
        self.addCleanup(self.owner.destroy)

    def add_step(self, **properties):
        return Step(parent=self.owner, **properties)

    async def test_empty_and_single_step_navigation(self):
        self.assertEqual(self.owner.steps, ())
        self.assertEqual(self.owner.children, ())
        self.assertIsNone(self.owner.selected_step)
        self.assertFalse(self.owner.next())
        only = self.add_step()
        self.assertEqual(self.owner.children, (only,))
        self.assertTrue(await only.validate())
        self.assertTrue(only.completed)
        self.assertFalse(self.owner.next())

    async def test_synchronous_navigation_requires_existing_validation_results(self):
        first, second, last = self.add_step(), self.add_step(), self.add_step()
        self.assertFalse(self.owner.next())
        self.assertTrue(await first.validate())
        self.assertTrue(self.owner.next())
        self.assertTrue(first.completed)
        self.assertIs(self.owner.selected_step, second)
        self.assertFalse(self.owner.next())
        self.assertTrue(await second.validate())
        self.assertTrue(self.owner.next())
        self.assertTrue(second.completed)
        self.assertIs(self.owner.selected_step, last)
        self.assertFalse(last.completed)
        self.assertFalse(self.owner.next())
        self.assertTrue(self.owner.previous())
        self.assertIs(self.owner.selected_step, second)

    async def test_only_steps_are_owned_and_action_completion_api_is_absent(self):
        first, second = self.add_step(), self.add_step()
        self.assertEqual(self.owner.children, (first, second))
        self.assertEqual(self.owner.steps, self.owner.children)
        for name in ("show_actions", "back_button", "next_button", "advance", "finished", "is_finished"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(Stepper, name))
                self.assertFalse(hasattr(self.owner, name))
        self.assertTrue(await first.validate())
        self.assertTrue(self.owner.next())
        self.assertTrue(first.completed)
        self.assertIs(self.owner.selected_step, second)
        self.assertEqual(self.owner.children, (first, second))

    async def test_reset_retains_field_values_and_clears_validation(self):
        first, second = self.add_step(), self.add_step()
        entry = TextBox(parent=first, text="Keep this value")
        self.assertTrue(await first.validate())
        self.assertTrue(self.owner.next())
        second.set_validation("Needs correction")
        first.scroll_y = 120
        second.scroll_x = 40
        self.owner.scroll_y = 80
        self.owner.reset()
        self.assertEqual(self.owner.selected_index, 0)
        self.assertEqual(entry.text, "Keep this value")
        self.assertEqual([(step.completed, step.error) for step in self.owner.steps],
                         [(False, ""), (False, "")])
        self.assertEqual((first.scroll_y, second.scroll_x, self.owner.scroll_y), (0, 0, 0))
        self.assertFalse(self.owner.next())

    async def test_rejected_step_validation_blocks_cached_navigation(self):
        step = self.add_step()
        self.add_step()
        step.validating.connect(lambda event: False)
        self.assertFalse(await step.validate())
        self.assertEqual(step.error, "Check this step")
        self.assertFalse(self.owner.next())

    async def test_linear_and_explicit_optional_requirements_combine(self):
        first = self.add_step()
        optional = self.add_step(optional=True)
        last = self.add_step(requires=[optional])
        self.assertFalse(self.owner.can_select(2))
        first.set_validation("")
        self.assertTrue(self.owner.can_select(1))
        self.assertFalse(self.owner.can_select(2))
        optional.set_validation("")
        self.assertTrue(self.owner.can_select(2))
        last.requires = []
        optional.set_validation("Invalid optional")
        self.assertTrue(self.owner.can_select(2))

    async def test_nonlinear_transitive_dependencies_follow_live_errors(self):
        self.owner.linear = False
        first = self.add_step()
        second = self.add_step(requires=[first])
        self.add_step(requires=[second])
        self.assertFalse(self.owner.can_select(2))
        first.set_validation("")
        second.set_validation("")
        self.assertTrue(self.owner.can_select(2))
        first.set_validation("Changed data", show_error=False)
        self.assertFalse(self.owner.can_select(2))
        self.assertTrue(second.completed)
        self.assertEqual(self.owner.selected_index, 0)

    async def test_disabled_and_destroyed_prerequisite_never_become_implicit_success(self):
        self.owner.linear = False
        first = self.add_step()
        last = self.add_step(requires=[first])
        first.enabled = False
        self.assertFalse(self.owner.can_select(1))
        first.set_validation("")
        self.assertTrue(self.owner.can_select(1))
        last.enabled = False
        self.assertFalse(self.owner.can_select(1))
        last.enabled = True
        first.destroy()
        self.owner.selected_index = 0
        self.assertIs(self.owner.selected_step, last)
        self.assertFalse(last.completed)
        self.assertEqual(last.requires, (first,))

    async def test_detach_before_navigation_task_starts_releases_reservation_for_reattach(self):
        first, last = self.add_step(), self.add_step()
        calls = []
        first.validating.connect(lambda event: calls.append(event.source))
        dispatcher = Dispatcher()
        self.owner._attach_dispatcher(dispatcher)
        try:
            self.owner.handle_input(Input("key_down", key="ArrowRight"))
            self.assertIsNotNone(self.owner._pending_action)
            self.owner._attach_dispatcher(None)
            await dispatcher.drain()
            self.assertEqual(calls, [])
            self.assertIsNone(self.owner._pending_action)
            self.owner._attach_dispatcher(dispatcher)
            self.owner.handle_input(Input("key_down", key="ArrowRight"))
            await dispatcher.drain()
            self.assertIs(self.owner.selected_step, last)
            self.assertEqual(calls, [first])
        finally:
            self.owner._attach_dispatcher(None)
            await dispatcher.shutdown()

    async def test_navigation_handler_exception_propagates_and_allows_retry(self):
        first, last = self.add_step(), self.add_step()
        fail = True

        async def validate(event):
            await asyncio.sleep(0)
            if fail:
                raise ValueError("Validation service failed")

        first.validating.connect(validate)
        dispatcher = Dispatcher()
        self.owner._attach_dispatcher(dispatcher)
        try:
            self.owner.handle_input(Input("key_down", key="ArrowRight"))
            with self.assertRaisesRegex(ValueError, "Validation service failed"):
                await dispatcher.drain()
            self.assertIsNone(self.owner._pending_action)
            self.assertIs(self.owner.selected_step, first)
            fail = False
            self.owner.handle_input(Input("key_down", key="ArrowRight"))
            await dispatcher.drain()
            self.assertIs(self.owner.selected_step, last)
        finally:
            self.owner._attach_dispatcher(None)
            await dispatcher.shutdown()


class StepperIndicatorRuntimeTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=640, height=480)
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.owner = Stepper(parent=self.app, width=600, height=400, padding=0)
        self.first = Step(parent=self.owner, title="Account")
        self.last = Step(parent=self.owner, title="Review", requires=[self.first])
        self.entry = TextBox(parent=self.first, text="Retained")
        self.review_entry = TextBox(parent=self.last, text="Review notes")
        self.outside_entry = TextBox(parent=self.app, top=420, width=200, height=30)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.app.close()
        await asyncio.wait_for(self.task, 2)

    async def click_indicator(self, index):
        headers, _ = self.owner._geometry(self.owner.content_bounds(self.owner.bounds))
        header = headers[index]
        x, y = header.x + 28, header.y + header.height / 2
        self.host.events.extend([Input("pointer_down", x, y), Input("pointer_up", x, y)])
        await eventually(lambda: not self.host.events)
        await asyncio.sleep(0)

    async def press_key(self, key):
        self.host.events.extend([Input("key_down", key=key), Input("key_up", key=key)])
        await eventually(lambda: not self.host.events)
        await asyncio.sleep(0)

    async def set_orientation(self, orientation):
        frame = self.host.frames
        self.owner.orientation = orientation
        self.owner.invalidate()
        await eventually(lambda: self.host.frames > frame)

    async def test_indicator_and_keyboard_requests_validate_only_prerequisites(self):
        validations, changes = [], []

        def validate_first(event):
            validations.append(event)
            return bool(self.entry.text.strip())

        self.first.validating.connect(validate_first)
        self.last.validating.connect(lambda event: validations.append(event))
        self.owner.changed.connect(lambda event: changes.append(event.origin))
        for orientation in ("horizontal", "vertical"):
            with self.subTest(orientation=orientation):
                self.owner.reset()
                self.entry.text = ""
                await self.set_orientation(orientation)
                self.entry.focus()
                await self.click_indicator(1)
                self.assertIs(self.owner.selected_step, self.first)
                self.assertIs(self.runtime.router.focus, self.owner)
                self.assertEqual(self.first.error, "Check this step")
                forward = "ArrowRight" if orientation == "horizontal" else "ArrowDown"
                await self.press_key(forward)
                self.assertIs(self.owner.selected_step, self.first)

                self.entry.text = "Retained"
                self.assertFalse(self.first.completed)
                await self.click_indicator(1)
                self.assertIs(self.owner.selected_step, self.last)
                self.assertIs(self.runtime.router.focus, self.owner)
                self.assertEqual(changes[-1], "user")
                self.assertFalse(self.first.visible)
                self.assertTrue(self.last.visible)
                self.assertTrue(self.first.completed)
                self.assertEqual(self.first.error, "")
                count = len(validations)
                for key in ("Enter", "Space", "End"):
                    await self.press_key(key)
                    self.assertIs(self.owner.selected_step, self.last)
                    self.assertFalse(self.last.completed)
                await self.click_indicator(1)
                self.assertEqual(len(validations), count)
                self.assertTrue(all(event.source is self.first for event in validations))
                self.assertIn("user", [event.origin for event in validations])

                await self.press_key("Home")
                self.assertIs(self.owner.selected_step, self.first)
                self.assertEqual(self.entry.text, "Retained")
                await self.press_key(forward)
                self.assertIs(self.owner.selected_step, self.last)
                self.assertEqual(self.review_entry.text, "Review notes")

    async def test_hover_paint_and_queries_do_not_validate_and_retry_refreshes_feedback(self):
        calls = []

        def validate(event):
            calls.append(event.origin)
            if not self.entry.text.strip():
                event.reject("Enter an account name")

        self.first.validating.connect(validate)
        self.entry.text = ""
        headers, _ = self.owner._geometry(self.owner.content_bounds(self.owner.bounds))
        header = headers[1]
        x, y = header.x + 28, header.y + header.height / 2
        self.host.events.extend([Input("pointer_move", x, y), Input("pointer_down", x, y)])
        frame = self.host.frames
        self.owner.invalidate()
        await eventually(lambda: not self.host.events and self.host.frames > frame)
        self.assertFalse(self.owner.can_select(1))
        self.assertFalse(self.owner.select(1))
        self.assertEqual(calls, [])
        self.assertEqual(self.first.error, "")
        self.host.events.append(Input("pointer_up", x, y))
        await eventually(lambda: bool(self.first.error))
        self.assertEqual(self.first.error, "Enter an account name")
        self.assertEqual(calls, ["user"])
        self.entry.text = "Corrected"
        await eventually(lambda: self.first.completed and not self.first.error)
        await self.click_indicator(1)
        self.assertIs(self.owner.selected_step, self.last)
        self.assertEqual(self.first.error, "")
        self.assertTrue(self.first.completed)
        await self.click_indicator(0)
        self.entry.text = ""
        await eventually(lambda: not self.first.completed and bool(self.first.error))
        await self.click_indicator(1)
        self.assertIs(self.owner.selected_step, self.first)
        self.assertFalse(self.first.completed)
        self.assertEqual(self.first.error, "Enter an account name")

    async def test_backward_navigation_does_not_validate_the_step_being_left(self):
        calls = []
        self.last.validating.connect(lambda event: calls.append(event) or False)
        self.first.set_validation("")
        self.owner.select(1)
        self.last.set_validation("Fix the review")
        await self.click_indicator(0)
        self.assertIs(self.owner.selected_step, self.first)
        self.assertEqual(calls, [])
        self.assertEqual(self.last.error, "Fix the review")

    async def test_nonlinear_navigation_validates_transitive_optional_requirements(self):
        self.owner.linear = False
        self.last.optional = True
        final = Step(parent=self.owner, title="Done", requires=[self.last])
        calls = []
        self.first.validating.connect(lambda event: calls.append(self.first))
        self.last.validating.connect(lambda event: calls.append(self.last))
        frame = self.host.frames
        self.owner.invalidate()
        await eventually(lambda: self.host.frames > frame)
        await self.click_indicator(2)
        self.assertIs(self.owner.selected_step, final)
        self.assertEqual(calls, [self.first, self.last])
        self.assertTrue(self.first.completed and self.last.completed)
        self.assertFalse(final.completed)

    async def test_optional_step_is_skipped_and_keyboard_stops_at_first_failed_destination(self):
        self.last.optional = True
        final = Step(parent=self.owner, title="Done")
        self.owner.linear = False
        final.requires = []
        rejection = self.first.validating.connect(lambda event: False)
        self.owner.focus()
        await self.press_key("ArrowRight")
        self.assertIs(self.owner.selected_step, self.first)
        self.assertEqual(self.first.error, "Check this step")
        self.assertTrue(self.owner.can_select(2))  # A later destination was not tried.
        self.owner.linear = True
        rejection.disconnect()
        self.first.set_validation("")
        optional_calls = []
        self.last.validating.connect(lambda event: optional_calls.append(event) or False)
        await self.press_key("End")
        self.assertIs(self.owner.selected_step, final)
        self.assertEqual(optional_calls, [])
        self.assertFalse(self.last.completed)

    async def test_async_navigation_reserves_once_before_queued_repeated_clicks(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def validate(event):
            calls.append(event.origin)
            started.set()
            await release.wait()
            return True

        self.first.validating.connect(validate)
        headers, _ = self.owner._geometry(self.owner.content_bounds(self.owner.bounds))
        header = headers[1]
        x, y = header.x + 28, header.y + header.height / 2
        self.host.events.extend([Input(kind, x, y) for kind in (
            "pointer_down", "pointer_up", "pointer_down", "pointer_up")])
        await started.wait()
        await self.press_key("End")
        self.assertEqual(calls, ["user"])
        self.assertIs(self.owner.selected_step, self.first)
        release.set()
        await eventually(lambda: self.owner._pending_action is None)
        self.assertIs(self.owner.selected_step, self.last)

    async def test_pending_navigation_does_not_override_reset_selection_or_dependency_changes(self):
        for change in ("reset", "selection", "requires", "linear", "disabled", "owner_disabled"):
            with self.subTest(change=change):
                self.owner.reset()
                self.owner.enabled = True
                self.owner.linear = True
                self.last.enabled = True
                self.last.requires = [self.first]
                started, release = asyncio.Event(), asyncio.Event()

                async def validate(event):
                    started.set()
                    await release.wait()
                    return True

                subscription = self.first.validating.connect(validate)
                await self.click_indicator(1)
                await started.wait()
                if change == "reset":
                    self.owner.reset()
                elif change == "selection":
                    self.owner.selected_index = 1
                elif change == "requires":
                    self.last.requires = []
                elif change == "linear":
                    self.owner.linear = False
                elif change == "disabled":
                    self.last.enabled = False
                else:
                    self.owner.enabled = False
                release.set()
                await eventually(lambda: self.runtime.dispatcher.pending == 0)
                await asyncio.sleep(0)
                self.assertEqual(self.owner.selected_index, 1 if change == "selection" else 0)
                self.assertIsNone(self.owner._pending_action)
                subscription.disconnect()

    async def test_destroying_stepper_cancels_pending_indicator_validation(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def validate(event):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        self.first.validating.connect(validate)
        await self.click_indicator(1)
        await started.wait()
        self.owner.destroy()
        await cancelled.wait()
        await eventually(lambda: self.runtime.dispatcher.pending == 0)
        self.assertTrue(self.owner._disposed)
        self.assertIsNone(self.owner._pending_action)

    async def test_invalid_indicator_request_keeps_current_step_and_shows_feedback(self):
        self.first.validating.connect(lambda event: event.reject("Enter a name"))
        await self.click_indicator(1)
        self.assertEqual(self.first.error, "Enter a name")
        self.assertIs(self.owner.selected_step, self.first)
        self.assertFalse(self.first.completed)
        self.assertIs(self.runtime.router.focus, self.owner)

    async def test_tab_order_contains_only_header_active_fields_and_external_controls(self):
        self.owner.focus()
        await self.press_key("Tab")
        self.assertIs(self.runtime.router.focus, self.entry)
        await self.press_key("Tab")
        self.assertIs(self.runtime.router.focus, self.outside_entry)
        await self.press_key("Tab")
        self.assertIs(self.runtime.router.focus, self.owner)
        self.entry.focus()
        self.first.set_validation("")
        self.assertTrue(self.owner.select(1))
        self.assertIs(self.runtime.router.focus, self.owner)
        await self.press_key("Tab")
        self.assertIs(self.runtime.router.focus, self.review_entry)
        await self.press_key("Tab")
        self.assertIs(self.runtime.router.focus, self.outside_entry)

    async def test_active_content_uses_all_space_beyond_headers_in_both_orientations(self):
        self.first.set_validation("")
        for orientation in ("horizontal", "vertical"):
            with self.subTest(orientation=orientation):
                await self.set_orientation(orientation)
                for index in (0, 1):
                    frame = self.host.frames
                    self.owner.selected_index = index
                    self.owner.invalidate()
                    await eventually(lambda: self.host.frames > frame)
                    area = self.owner.content_bounds(self.owner.bounds)
                    step = self.owner.selected_step
                    header_rows = 2 if orientation == "vertical" else 1
                    self.assertEqual(step.bounds.height, area.height - 72 * header_rows)
                    self.assertEqual(step.bounds.width, area.width - (48 if orientation == "vertical" else 0))
                    if orientation == "horizontal" or index == 1:
                        self.assertEqual(step.bounds.bottom, area.bottom)
                    self.assertEqual(self.owner.children, (self.first, self.last))

