import asyncio
import inspect
import unittest

from _ui_testcase import AsyncUIOwnerTestCase
from pysual import Button, ClickEvent
from pysual.events import Dispatcher


class EventDispatchTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.button = Button()
        self.dispatcher = Dispatcher(limit=10)
        self.button._attach_dispatcher(self.dispatcher)

    async def asyncTearDown(self):
        await self.dispatcher.shutdown()
        self.button.destroy()

    @unittest.skipUnless(hasattr(asyncio, "eager_task_factory"), "requires Python 3.12")
    async def test_eager_deliveries_release_pending_exactly_once(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        release = asyncio.Event()
        seen = []

        def immediate(event):
            seen.append("immediate")

        async def deferred(event):
            seen.append("deferred")
            await release.wait()

        self.button.click.connect(immediate)
        self.button.click.connect(deferred)
        try:
            loop.set_task_factory(asyncio.eager_task_factory)
            self.button.click.emit(ClickEvent(source=self.button))
        finally:
            loop.set_task_factory(previous)
        await asyncio.sleep(0)
        self.assertEqual(seen, ["immediate", "deferred"])
        self.assertEqual(self.dispatcher.pending, 1)
        release.set()
        await self.dispatcher.drain()
        self.assertEqual(self.dispatcher.pending, 0)
        self.assertEqual(self.dispatcher._running, 0)

    async def test_rejected_delivery_releases_unscheduled_reservations(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        rejected = []
        seen = []

        def factory(loop, coroutine, **kwargs):
            if not seen:
                seen.append("scheduled")
                return asyncio.Task(coroutine, loop=loop, **kwargs)
            rejected.append(coroutine)
            raise RuntimeError("task admission failed")

        def listener(index):
            return lambda event: seen.append(index)

        for index in range(3):
            self.button.click.connect(listener(index))
        try:
            loop.set_task_factory(factory)
            with self.assertRaisesRegex(RuntimeError, "task admission failed"):
                self.button.click.emit(ClickEvent(source=self.button))
        finally:
            loop.set_task_factory(previous)
        self.assertEqual(self.dispatcher.pending, 1)
        self.assertEqual(inspect.getcoroutinestate(rejected[0]), inspect.CORO_CLOSED)
        await self.dispatcher.drain()
        self.assertEqual(self.dispatcher.pending, 0)
        self.assertEqual(seen, ["scheduled", 0])

    async def test_rejected_owned_task_closes_wrapper_and_inner_coroutine(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        rejected = []

        def reject(loop, coroutine, **kwargs):
            rejected.append(coroutine)
            raise RuntimeError("task admission failed")

        async def work():
            await asyncio.sleep(0)

        coroutine = work()
        try:
            loop.set_task_factory(reject)
            with self.assertRaisesRegex(RuntimeError, "task admission failed"):
                self.dispatcher.create_task(coroutine, self.button)
        finally:
            loop.set_task_factory(previous)
        self.assertEqual(inspect.getcoroutinestate(coroutine), inspect.CORO_CLOSED)
        self.assertEqual(inspect.getcoroutinestate(rejected[0]), inspect.CORO_CLOSED)
        self.assertEqual(self.dispatcher._tasks, set())

    async def test_delivery_cancelled_before_entry_releases_reservation(self):
        seen = []
        self.button.click.connect(lambda event: seen.append(event))
        self.button.click.emit(ClickEvent(source=self.button))
        self.dispatcher.cancel_owner(self.button)
        await self.dispatcher.drain()
        self.assertEqual(seen, [])
        self.assertEqual(self.dispatcher.pending, 0)
        self.assertEqual(self.dispatcher._running, 0)

    async def test_snapshot_preserves_order_and_unhashable_callback_equality(self):
        class Handler:
            def __init__(self, name):
                self.name = name

            def __eq__(self, other):
                return isinstance(other, Handler) and self.name == other.name

            def __call__(self, event):
                pass

        channel = self.button.click
        channel._convention = Handler("convention")
        channel._namespace = Handler("namespace")
        callbacks = [Handler(name) for name in ("first", "convention", "namespace", "last")]
        for callback in callbacks:
            channel.connect(callback)
        channel.connect(Handler("first"))
        snapshot = channel._snapshot()
        self.assertEqual([callback.name for callback in snapshot], ["convention", "namespace", "first", "last"])
        self.assertIs(snapshot[0], channel._convention)
        channel._namespace = Handler("convention")
        self.assertEqual([callback.name for callback in channel._snapshot()], ["convention", "first", "namespace", "last"])

    async def test_snapshot_does_not_rescan_unique_explicit_listeners(self):
        class Handler:
            comparisons = 0

            def __eq__(self, other):
                type(self).comparisons += 1
                return self is other

            def __call__(self, event):
                pass

        callbacks = [Handler() for _ in range(100)]
        for callback in callbacks:
            self.button.click.connect(callback)
        Handler.comparisons = 0
        self.assertEqual(self.button.click._snapshot(), tuple(callbacks))
        self.assertLessEqual(Handler.comparisons, 2 * len(callbacks))

    async def test_falsey_bound_callable_is_still_delivered(self):
        seen = []

        class Handler:
            def __bool__(self):
                return False

            def __call__(self, event):
                seen.append(event)

        self.button.click._convention = Handler()
        event = ClickEvent(source=self.button)
        self.button.click.emit(event)
        await self.dispatcher.drain()
        self.assertEqual(seen, [event])
