"""Worker-thread access routes through the UI owner; reads see committed values."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace
import unittest

from pysual import Container, Label
from pysual._engine import (
    DispatchTimeout,
    Engine,
    _origin,
    call,
    capture_origin,
    check_origin,
    is_ui_thread,
)
from pysual.errors import LifecycleError


class OriginGuardTests(unittest.TestCase):
    def check(self, obj, origins):
        token = _origin.set(origins)
        try:
            check_origin(obj)
        finally:
            _origin.reset(token)

    def window(self, parent=None):
        return SimpleNamespace(_parent=parent, _presentation=object(), _generation=1)

    def test_current_nested_presentations_allow_access_but_reopened_parent_does_not(self):
        parent = self.window()
        child = self.window(parent)
        control = SimpleNamespace(_parent=child)
        origins = capture_origin(control)
        self.check(control, origins)
        parent._presentation = object()
        parent._generation += 1
        with self.assertRaises(LifecycleError):
            self.check(control, origins)

    def test_changed_unrelated_origin_does_not_block_current_window(self):
        current, unrelated = self.window(), self.window()
        control = SimpleNamespace(_parent=current)
        origins = capture_origin(current) + capture_origin(unrelated)
        unrelated._presentation = object()
        unrelated._generation += 1
        self.check(control, origins)
        with self.assertRaises(LifecycleError):
            self.check(SimpleNamespace(_parent=unrelated), origins)

    def test_initial_invocation_allows_first_open_but_not_reopen(self):
        window = SimpleNamespace(_parent=None, _presentation=None, _generation=0)
        control = SimpleNamespace(_parent=window)
        origins = capture_origin(window)
        window._presentation, window._generation = object(), 1
        self.check(control, origins)
        window._presentation, window._generation = object(), 2
        with self.assertRaises(LifecycleError):
            self.check(control, origins)

    def test_replacement_presentation_is_checked_by_identity(self):
        window = self.window()
        window._presentation = SimpleNamespace(state="open")
        origins = capture_origin(window)
        window._presentation = SimpleNamespace(state="open")
        self.assertEqual(window._presentation, origins[0][1])
        with self.assertRaises(LifecycleError):
            self.check(window, origins)


class WorkerAccessTests(unittest.TestCase):
    """Semantics of property access from threads other than the UI owner."""

    def test_writes_run_on_the_owner_and_reads_see_committed_values(self):
        panel = Container(layout="stack")
        label = Label(parent=panel, text="before")
        self.addCleanup(lambda: call(panel.destroy))
        observed = {}

        def worker():
            label.text = "after"
            observed["read"] = label.text
            observed["parent_read"] = panel.layout

        with ThreadPoolExecutor(1) as pool:
            pool.submit(worker).result(5)
        self.assertEqual(observed, {"read": "after", "parent_read": "stack"})
        self.assertEqual(call(lambda: label.text), "after")
        self.assertFalse(is_ui_thread())

    def test_worker_reads_keep_disposal_and_origin_checks(self):
        label = Label(text="x")
        call(label.destroy)
        with ThreadPoolExecutor(1) as pool:
            with self.assertRaises(LifecycleError):
                pool.submit(lambda: label.text).result(5)

    def test_customized_liveness_dispatches_reads_to_the_owner(self):
        threads = []

        class Audited(Label):
            def _check_live(self):
                threads.append(threading.get_ident())
                super()._check_live()

        label = Audited(text="audited")
        self.addCleanup(lambda: call(label.destroy))
        with ThreadPoolExecutor(1) as pool:
            value = pool.submit(lambda: label.text).result(5)
        self.assertEqual(value, "audited")
        owner = call(threading.get_ident)
        self.assertTrue(threads and all(ident == owner for ident in threads))

    def test_a_write_is_visible_to_the_next_read_from_any_thread(self):
        labels = [Label(text="0"), Label(text="0")]
        for label in labels:
            self.addCleanup(lambda label=label: call(label.destroy))

        def producer(label, start):
            for index in range(start, start + 50):
                label.text = str(index)
                self.assertEqual(label.text, str(index))

        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(producer, label, start)
                       for label, start in zip(labels, (0, 1000))]
            for future in futures:
                future.result(10)
        self.assertEqual([call(lambda l=l: l.text) for l in labels], ["49", "1049"])


class DrainPendingTests(unittest.TestCase):
    def make_engine(self, **kwargs):
        engine = Engine(**kwargs)
        self.addCleanup(engine.shutdown)
        return engine

    def test_drain_pending_serves_requests_queued_before_entry_in_order(self):
        engine = self.make_engine()
        entered, admitted = threading.Event(), threading.Event()
        changes = []

        def owner_callback():
            entered.set()
            self.assertTrue(admitted.wait(2))
            engine.drain_pending()
            changes.append("after-drain")

        with ThreadPoolExecutor(1) as pool:
            engine.loop.call_soon_threadsafe(owner_callback)
            self.assertTrue(entered.wait(2))
            futures = [engine._admit(lambda i=i: changes.append(i)) for i in range(3)]
            admitted.set()
            for future in futures:
                future.result(2)
        engine.call(lambda: None)
        self.assertEqual(changes, [0, 1, 2, "after-drain"])

    def test_drain_pending_is_owner_only_and_reports_request_errors_to_callers(self):
        engine = self.make_engine()
        with self.assertRaises(LifecycleError):
            engine.drain_pending()

        def failing():
            raise ValueError("request failed")

        future = engine._admit(failing)
        with self.assertRaises(ValueError):
            future.result(2)
        engine.call(engine.drain_pending)  # An empty inbox is a no-op.

    def test_call_timeout_before_execution_is_cancelled_not_run(self):
        engine = self.make_engine(timeout=0.03)
        entered, release = threading.Event(), threading.Event()
        changes = []

        def hold():
            entered.set()
            release.wait(2)

        engine.loop.call_soon_threadsafe(hold)
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(DispatchTimeout) as caught:
                engine.call(changes.append, "must not run")
            self.assertFalse(caught.exception.started)
        finally:
            release.set()
        engine.call(lambda: None)
        self.assertEqual(changes, [])

    def test_opening_waits_through_queued_and_running_work(self):
        engine = self.make_engine(timeout=0.01)
        held, enter, release_queue, release_opening = (threading.Event() for _ in range(4))

        def hold():
            held.set()
            release_queue.wait(2)

        def opening(*, timeout):
            enter.set()
            release_opening.wait(2)
            return timeout  # The called function still owns its keyword arguments.

        engine.loop.call_soon_threadsafe(hold)
        self.assertTrue(held.wait(1))
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(engine.call_opening, opening, timeout="application value")
            try:
                self.assertFalse(enter.wait(0.05))
                self.assertFalse(future.done())
                release_queue.set()
                self.assertTrue(enter.wait(1))
                self.assertFalse(release_opening.wait(0.05))
                self.assertFalse(future.done())
            finally:
                release_queue.set()
                release_opening.set()
            self.assertEqual(future.result(2), "application value")

    def test_opening_propagates_the_actual_timeout_error(self):
        engine = self.make_engine()
        failure = TimeoutError("application startup failed")

        def opening():
            raise failure

        with self.assertRaises(TimeoutError) as caught:
            engine.call_opening(opening)
        self.assertIs(caught.exception, failure)

    def test_sustained_producers_allow_timer_heartbeats(self):
        engine = self.make_engine()
        started = [threading.Event(), threading.Event()]
        stop, heartbeats_done = threading.Event(), threading.Event()
        heartbeat_count = 0
        writes = [0]

        def mutate():
            writes[0] += 1

        def producer(index):
            engine.call(mutate)
            started[index].set()
            while not stop.is_set():
                engine.call(mutate)

        def heartbeat():
            nonlocal heartbeat_count
            heartbeat_count += 1
            if heartbeat_count == 20:
                heartbeats_done.set()
            elif not stop.is_set():
                engine.loop.call_later(0.001, heartbeat)

        with ThreadPoolExecutor(2) as pool:
            producers = [pool.submit(producer, index) for index in range(2)]
            try:
                self.assertTrue(all(event.wait(1) for event in started))
                engine.loop.call_soon_threadsafe(heartbeat)
                self.assertTrue(
                    heartbeats_done.wait(2), "Producer traffic starved owner timers"
                )
                self.assertGreater(writes[0], 2)
            finally:
                stop.set()
            for producer_future in producers:
                producer_future.result(1)

    def test_original_handle_context_and_cancellation_are_preserved(self):
        from contextvars import ContextVar

        engine = self.make_engine()
        marker = ContextVar("worker_access_context", default="default")
        observed = []
        ready = threading.Event()
        token = marker.set("caller")
        try:
            engine.loop.call_soon_threadsafe(
                lambda: (observed.append(marker.get()), ready.set())
            )
        finally:
            marker.reset(token)
        self.assertTrue(ready.wait(1))
        self.assertEqual(observed, ["caller"])

        def cancel_before_entry():
            handle = engine.loop.call_soon(observed.append, "cancelled")
            handle.cancel()

        engine.call(cancel_before_entry)
        engine.call(lambda: None)
        self.assertEqual(observed, ["caller"])
        self.assertIsInstance(engine.loop, asyncio.AbstractEventLoop)


if __name__ == "__main__":
    unittest.main()
