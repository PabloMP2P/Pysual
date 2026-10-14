import asyncio
import inspect
import unittest

from _ui_testcase import AsyncUIOwnerTestCase
from pysual import App
from pysual.errors import LifecycleError
from pysual.runtime import Runtime, open_window, wait_async as wait_windows_async
import pysual.runtime as window_runtime
from test_library import RecordingHost


class CloseDecisionTests(AsyncUIOwnerTestCase):
    @unittest.skipUnless(hasattr(asyncio, "eager_task_factory"), "requires Python 3.12")
    async def test_eager_loaded_handler_can_destroy_before_pump_starts(self):
        app = App()
        host = RecordingHost()
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        app.loaded.connect(lambda event: app.destroy())
        try:
            try:
                loop.set_task_factory(asyncio.eager_task_factory)
                presentation = open_window(app, backend=host)
            finally:
                loop.set_task_factory(previous)
            outcome = await asyncio.wait_for(presentation.wait_async(), 0.5)
            self.assertEqual(outcome.reason, "destroyed")
            self.assertTrue(host.closed)
            self.assertEqual(app.lifecycle_state, "DESTROYED")
        finally:
            if app._runtime is not None:
                app._runtime.force_close()
                await app._runtime._cleanup()
            app.destroy()

    async def test_failed_startup_cancels_admitted_pump(self):
        # Module wait deliberately retains the oldest unobserved failure across
        # openings. Consume an earlier test's diagnostic before producing ours,
        # and check its identity so this cannot hide this test's startup error.
        self.assertFalse(window_runtime._active)
        previous_failure = window_runtime._idle.failure
        if previous_failure is not None:
            with self.assertRaises(BaseException) as previous:
                await wait_windows_async()
            self.assertIs(previous.exception, previous_failure.error)

        class BrokenHost(RecordingHost):
            def open(self, *args, **kwargs):
                super().open(*args, **kwargs)
                raise RuntimeError("host open failed")

        factories = [None]
        if hasattr(asyncio, "eager_task_factory"):
            factories.append(asyncio.eager_task_factory)
        for task_factory in factories:
            with self.subTest(factory=task_factory):
                app = App()
                host = BrokenHost()
                loop = asyncio.get_running_loop()
                previous = loop.get_task_factory()
                admitted = []

                def record(loop, coroutine, **kwargs):
                    task = (
                        asyncio.Task(coroutine, loop=loop, **kwargs)
                        if task_factory is None
                        else task_factory(loop, coroutine, **kwargs)
                    )
                    admitted.append(task)
                    return task

                try:
                    try:
                        loop.set_task_factory(record)
                        with self.assertRaisesRegex(RuntimeError, "host open failed"):
                            open_window(app, backend=host)
                    finally:
                        loop.set_task_factory(previous)
                    await asyncio.sleep(0)
                    self.assertEqual(len(admitted), 1)
                    self.assertTrue(admitted[0].cancelled())
                    self.assertTrue(host.closed)
                    self.assertIsNone(app._runtime)
                    with self.assertRaisesRegex(RuntimeError, "host open failed"):
                        await wait_windows_async()
                finally:
                    app.destroy()

    async def test_rejected_window_task_does_not_open_host_and_allows_retry(self):
        app = App()
        host = RecordingHost()
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        rejected = []

        def reject(loop, coroutine, **kwargs):
            rejected.append(coroutine)
            raise RuntimeError("task admission failed")

        try:
            try:
                loop.set_task_factory(reject)
                with self.assertRaisesRegex(RuntimeError, "task admission failed"):
                    open_window(app, backend=host)
            finally:
                loop.set_task_factory(previous)
            self.assertFalse(host.opened)
            self.assertIsNone(app._runtime)
            self.assertEqual(app.lifecycle_state, "CREATED")
            self.assertEqual(inspect.getcoroutinestate(rejected[0]), inspect.CORO_CLOSED)
            presentation = open_window(app, backend=RecordingHost())
            self.assertTrue(await app.close_async())
            await asyncio.wait_for(presentation.wait_async(), 0.5)
        finally:
            # Keep a broken admission implementation from leaking into the
            # other owner-loop tests when this regression fails.
            for coroutine in rejected:
                coroutine.close()
            if app._runtime is not None:
                app._runtime.force_close()
                await app._runtime._cleanup()
            app.destroy()

    async def test_closing_handler_cannot_await_its_own_decision(self):
        app = App()
        runtime = Runtime(app, RecordingHost())
        rejected = []

        async def decide(event):
            with self.assertRaisesRegex(LifecycleError, "own close decision"):
                await app.close_async()
            rejected.append(event)

        app.closing.connect(decide)
        runtime.start()
        try:
            self.assertTrue(await asyncio.wait_for(runtime.request_close(), 0.5))
            self.assertEqual(len(rejected), 1)
        finally:
            runtime.force_close()
            await runtime._cleanup()
            app.destroy()

    @unittest.skipUnless(hasattr(asyncio, "eager_task_factory"), "requires Python 3.12")
    async def test_eager_closing_handler_shares_reentrant_request(self):
        app = App()
        runtime = Runtime(app, RecordingHost())
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        decisions = []

        def decide(event):
            decisions.append(event)
            # Bound the regression: the broken implementation admits one extra
            # decision rather than recursing until the Python stack overflows.
            if len(decisions) == 1:
                app.close("nested")

        app.closing.connect(decide)
        runtime.start()
        try:
            try:
                loop.set_task_factory(asyncio.eager_task_factory)
                waiter = runtime.request_close("first")
            finally:
                loop.set_task_factory(previous)
            self.assertTrue(await asyncio.wait_for(waiter, 0.5))
            self.assertEqual(len(decisions), 1)
            self.assertEqual(runtime._close_result, "first")
            self.assertEqual(runtime.dispatcher.pending, 0)
        finally:
            runtime.force_close()
            await runtime._cleanup()
            app.destroy()

    async def test_rejected_close_task_releases_coroutine_and_allows_retry(self):
        app = App()
        runtime = Runtime(app, RecordingHost())
        loop = asyncio.get_running_loop()
        previous = loop.get_task_factory()
        rejected = []

        def reject(loop, coroutine, **kwargs):
            rejected.append(coroutine)
            raise RuntimeError("task admission failed")

        runtime.start()
        try:
            try:
                loop.set_task_factory(reject)
                with self.assertRaisesRegex(RuntimeError, "task admission failed"):
                    runtime.request_close()
            finally:
                loop.set_task_factory(previous)
            self.assertEqual(inspect.getcoroutinestate(rejected[0]), inspect.CORO_CLOSED)
            self.assertTrue(await runtime.request_close())
        finally:
            runtime.force_close()
            await runtime._cleanup()
            app.destroy()

    async def test_retry_between_veto_and_done_callback_settles_both_callers(self):
        app = App()
        runtime = Runtime(app, RecordingHost())
        decisions = []
        waiters = []

        def decide(event):
            decisions.append(event)
            if len(decisions) == 1:
                event.cancel()

        app.closing.connect(decide)
        runtime.start()
        try:
            waiters.append(runtime.request_close())
            # The decision task runs first, then this retry, then the decision's
            # done callback. Both requests must retain their own result future.
            asyncio.get_running_loop().call_soon(
                lambda: waiters.append(runtime.request_close())
            )
            for _ in range(6):
                await asyncio.sleep(0)
            self.assertEqual(len(waiters), 2)
            self.assertTrue(all(waiter.done() for waiter in waiters))
            self.assertEqual([waiter.result() for waiter in waiters], [False, True])
        finally:
            runtime.force_close()
            await runtime._cleanup()
            app.destroy()
