"""Validation decisions reuse event delivery without changing notification returns."""

import asyncio
from typing import ClassVar

from _ui_testcase import AsyncUIOwnerTestCase
from pysual import ClosingEvent, Control, Event, UiEvent, ValidationEvent
from pysual.errors import BindingError, LifecycleError
from pysual.events import Dispatcher, Validation


class ValidationControl(Control):
    validating: ClassVar[Validation] = Validation()
    closing: ClassVar[Event[ClosingEvent]] = Event(ClosingEvent)
    notified: ClassVar[Event[UiEvent]] = Event(UiEvent)


class ValidationEventTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.owner = ValidationControl()
        self.dispatcher = Dispatcher()
        self.owner._attach_dispatcher(self.dispatcher)

    async def asyncTearDown(self):
        await self.dispatcher.shutdown()
        self.owner.destroy()

    async def test_only_exact_false_rejects_for_sync_and_async_handlers(self):
        for asynchronous in (False, True):
            for result in (False, True, None, 0):
                with self.subTest(asynchronous=asynchronous, result=result):
                    def immediate(event):
                        return result

                    async def delayed(event):
                        await asyncio.sleep(0)
                        return result

                    subscription = self.owner.validating.connect(delayed if asynchronous else immediate)
                    event = ValidationEvent(source=self.owner)
                    accepted = await self.dispatcher.decide(self.owner.validating, event)
                    self.assertIs(accepted, result is not False)
                    self.assertIs(event.cancelled, result is False)
                    self.assertEqual(event.message, "")
                    self.assertEqual(self.dispatcher.pending, 0)
                    with self.assertRaisesRegex(LifecycleError, "sealed"):
                        event.reject("Too late")
                    subscription.disconnect()

    async def test_rejection_keeps_first_nonempty_message_and_awaits_all_handlers(self):
        calls = []

        def empty(event):
            calls.append("empty")
            return False

        async def first(event):
            calls.append("first started")
            await asyncio.sleep(0)
            event.reject("Enter a workspace name")
            calls.append("first finished")

        def later(event):
            calls.append("later")
            event.reject("A later validation message")
            return True

        for callback in (empty, first, later):
            self.owner.validating.connect(callback)
        event = ValidationEvent(source=self.owner)
        self.assertFalse(await self.dispatcher.decide(self.owner.validating, event))
        self.assertEqual(event.message, "Enter a workspace name")
        self.assertEqual(calls, ["empty", "first started", "first finished", "later"])
        self.assertEqual(self.dispatcher.pending, 0)

    async def test_emit_requires_awaited_dispatch_and_decisions_cannot_be_reused(self):
        event = ValidationEvent(source=self.owner)
        with self.assertRaisesRegex(TypeError, "awaited decision"):
            self.owner.validating.emit(event)
        self.assertTrue(await self.dispatcher.decide(self.owner.validating, event))
        with self.assertRaises(LifecycleError):
            await self.dispatcher.decide(self.owner.validating, event)
        with self.assertRaises(LifecycleError):
            await self.dispatcher.decide(
                self.owner.validating, ValidationEvent(source=object())
            )
        self.assertEqual(self.dispatcher.pending, 0)

    async def test_reject_validates_message_before_mutating_the_decision(self):
        event = ValidationEvent(source=self.owner)
        for message in (None, False, 1):
            with self.subTest(message=message), self.assertRaises(TypeError):
                event.reject(message)
        self.assertFalse(event.cancelled)
        self.assertEqual(event.message, "")
        event.reject()
        event.reject("Required")
        self.assertTrue(event.cancelled)
        self.assertEqual(event.message, "Required")

    async def test_handler_failure_seals_the_event_and_releases_pending_slots(self):
        calls = []

        async def fail(event):
            await asyncio.sleep(0)
            raise ValueError("Validation failed unexpectedly")

        def later(event):
            calls.append(event)

        self.owner.validating.connect(fail)
        self.owner.validating.connect(later)
        event = ValidationEvent(source=self.owner)
        with self.assertRaisesRegex(ValueError, "unexpectedly"):
            await self.dispatcher.decide(self.owner.validating, event)
        self.assertEqual(calls, [])
        self.assertEqual(self.dispatcher.pending, 0)
        with self.assertRaisesRegex(LifecycleError, "sealed"):
            event.reject("Too late")

    async def test_validation_connections_keep_type_checks_disconnect_and_lifecycle(self):
        def wrong_event(event: int):
            return False

        with self.assertRaises(BindingError):
            self.owner.validating.connect(wrong_event)
        channel = self.owner.validating
        subscription = channel.connect(lambda event: False)
        subscription.disconnect()
        self.assertTrue(await self.dispatcher.decide(
            channel, ValidationEvent(source=self.owner)
        ))
        self.owner.destroy()
        with self.assertRaises(LifecycleError):
            channel.connect(lambda event: False)

    async def test_cancelled_validation_seals_the_event_and_releases_pending_slots(self):
        entered = asyncio.Event()

        async def wait(event):
            entered.set()
            await asyncio.Future()

        self.owner.validating.connect(wait)
        self.owner.validating.connect(lambda event: True)
        event = ValidationEvent(source=self.owner)
        decision = asyncio.create_task(self.dispatcher.decide(self.owner.validating, event))
        await entered.wait()
        self.assertEqual(self.dispatcher.pending, 2)
        decision.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await decision
        self.assertEqual(self.dispatcher.pending, 0)
        with self.assertRaisesRegex(LifecycleError, "sealed"):
            event.reject("Too late")

    async def test_false_returns_do_not_cancel_closing_or_other_events(self):
        calls = []

        def closing(event):
            calls.append("closing")
            return False

        async def notified(event):
            await asyncio.sleep(0)
            calls.append("notified")
            return False

        self.owner.closing.connect(closing)
        self.owner.notified.connect(notified)
        event = ClosingEvent(source=self.owner)
        self.assertTrue(await self.dispatcher.decide(self.owner.closing, event))
        self.assertFalse(event.cancelled)
        self.owner.notified.emit(UiEvent(source=self.owner))
        await self.dispatcher.drain()
        self.assertEqual(calls, ["closing", "notified"])
