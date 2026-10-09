"""Public plain-script window semantics, independent of any native display."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

from pysual.app import Window
from pysual import Button, Container, Label, SubWindow
from pysual._engine import call, call_async, get_engine
from pysual.errors import LifecycleError
from pysual.host import CapabilityError, Input, Viewport
from pysual.runtime import wait as wait_windows, wait_async as wait_windows_async
import pysual.runtime as window_runtime
from test_library import RecordingHost


class ModalHost(RecordingHost):
    capabilities = RecordingHost.capabilities | {"native_windows", "native_modal"}

    def __init__(self, *, fail_modal=False):
        super().__init__()
        self.owner = None
        self.fail_modal = fail_modal

    def set_modal_owner(self, owner):
        if self.fail_modal and owner is not None:
            raise CapabilityError("native modality unavailable")
        self.owner = owner


class WindowLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.windows = []
        self.consume_previous_failure()

    def consume_previous_failure(self):
        # Other lifecycle cases deliberately fail openings. Module diagnostics
        # now outlive later openings, so consume them explicitly between tests.
        self.assertFalse(call(lambda: bool(window_runtime._active)))
        try:
            wait_windows(0)
        except BaseException:
            pass

    def make(self, window_type=Window, **kwargs):
        window = window_type(**kwargs)
        self.windows.append(window)
        return window

    async def asyncTearDown(self):
        for window in reversed(self.windows):
            try:
                await window.destroy_async()
            except BaseException:
                pass
        self.consume_previous_failure()

    async def test_run_returns_self_after_first_frame_and_script_can_read_write(self):
        owner_threads = []

        class Demo(Window):
            def __init__(self):
                owner_threads.append(threading.get_ident())
                super().__init__()
                self.status = Label(text="before")

            def build(self):
                owner_threads.append(threading.get_ident())

        window = self.make(Demo)
        host = RecordingHost()
        self.assertIs(window.run(backend=host), window)
        self.assertGreaterEqual(host.frames, 1)
        self.assertTrue(window.is_open)
        window.status.text = "after"
        self.assertEqual(window.status.text, "after")
        self.assertEqual(len(set(owner_threads)), 1)
        self.assertNotEqual(owner_threads[0], threading.get_ident())

    async def test_slow_build_and_first_frame_do_not_expire_opening_gateways(self):
        owner = self.make()
        owner.run(backend=ModalHost())
        for gateway in ("run", "run_blocking", "show_modal"):
            for phase in ("build", "present"):
                with self.subTest(gateway=gateway, phase=phase):
                    entered, release, presented = (threading.Event() for _ in range(3))

                    class SlowWindow(Window):
                        def build(self):
                            if phase == "build":
                                entered.set()
                                release.wait(3)

                    class SlowHost(ModalHost):
                        def present(self):
                            if phase == "present":
                                entered.set()
                                release.wait(3)
                            super().present()
                            presented.set()

                    window = self.make(SlowWindow)
                    host = SlowHost()
                    arguments = (owner,) if gateway == "show_modal" else ()
                    with ThreadPoolExecutor(1) as pool:
                        with patch.object(get_engine(), "timeout", 0.01):
                            future = pool.submit(getattr(window, gateway), *arguments, backend=host)
                            try:
                                self.assertTrue(entered.wait(1))
                                self.assertFalse(release.wait(0.05))
                                self.assertFalse(future.done())
                            finally:
                                release.set()
                        self.assertTrue(presented.wait(2))
                        if gateway == "run":
                            self.assertIs(future.result(2), window)
                            self.assertGreaterEqual(host.frames, 1)
                        window.close("finished")
                        if gateway != "run":
                            result = future.result(2)
                            self.assertEqual(result, "finished" if gateway == "show_modal" else window)
                        await window.wait_async()

    async def test_slow_failed_startup_reports_the_original_error(self):
        failure = ValueError("invalid app data")

        class FailingWindow(Window):
            def build(self):
                threading.Event().wait(0.04)
                raise failure

        window = self.make(FailingWindow)
        host = RecordingHost()
        with patch.object(get_engine(), "timeout", 0.01):
            with self.assertRaises(ValueError) as caught:
                window.run(backend=host)
        self.assertIs(caught.exception, failure)
        self.assertFalse(host.opened)

    async def test_independent_roots_close_and_reopen_without_rebuilding(self):
        builds = []

        class Demo(Window):
            def build(self):
                builds.append(self)
                self.status = Label(text="kept")

        first, second = self.make(Demo), self.make()
        first.run(backend=RecordingHost())
        second.run(backend=RecordingHost())
        child = first.status
        opening = call(first._capture_presentation)
        first.close()
        await first.wait_async()
        self.assertTrue(second.is_open)
        first.run(backend=RecordingHost())
        self.assertIs(first.status, child)
        self.assertEqual(builds, [first])
        self.assertIsNot(call(first._capture_presentation), opening)
        self.assertTrue(opening.done)

    async def test_run_idempotent_for_active_modeless_opening(self):
        window = self.make()
        first, unused = RecordingHost(), RecordingHost()
        window.run(backend=first)
        opening = call(window._capture_presentation)
        window.run(backend=unused)
        self.assertIs(call(window._capture_presentation), opening)
        self.assertFalse(unused.opened)

    async def test_batch_updates_preserve_startup_and_host_viewport_restrictions(self):
        class FixedViewportHost(RecordingHost):
            capabilities = RecordingHost.capabilities - {"host_resize"}

            def set_size(self, width, height):
                raise CapabilityError("The host owns the viewport")

        window = self.make()
        host = FixedViewportHost()
        window.run(backend=host)
        original = window.resizable, window.ui_scale, window.max_pending_handlers
        size = window.width, window.height
        for name, value, error in (
            ("resizable", False, LifecycleError),
            ("ui_scale", 2, LifecycleError),
            ("max_pending_handlers", 3, LifecycleError),
            ("width", 111, CapabilityError),
            ("height", 112, CapabilityError),
        ):
            with self.subTest(name=name):
                with self.assertRaises(error):
                    setattr(window, name, value)
                with self.assertRaises(error):
                    window.update(title="Must not change", **{name: value})
                self.assertEqual(window.title, "Pysual")
                self.assertEqual((window.width, window.height), size)
                self.assertEqual(
                    (window.resizable, window.ui_scale, window.max_pending_handlers),
                    original,
                )
        # Idempotent settings still allow another valid property in the batch.
        window.update(title="Allowed", width=window.width, resizable=window.resizable,
                      ui_scale=window.ui_scale,
                      max_pending_handlers=window.max_pending_handlers)
        await call_async(lambda: None)
        self.assertTrue(window.is_open)
        self.assertEqual(window.title, "Allowed")
        self.assertEqual(call(lambda: window._runtime.dispatcher.limit), original[2])
        self.assertEqual(call(lambda: window._runtime._pending_size), {})

    async def test_browser_viewport_replaces_a_startup_size_without_set_size(self):
        class BrowserHost(RecordingHost):
            capabilities = RecordingHost.capabilities - {"host_resize"}

            def set_size(self, width, height):
                raise CapabilityError("The browser owns the web viewport size")

        class Sized(Window):
            def build(self):
                self.width, self.height = 1120, 820

        window = self.make(Sized)
        host = BrowserHost()
        window.run(backend=host)
        self.assertEqual((host.size), (1120, 820))

        def report_browser_viewport():
            host.size = (640, 480)
            host.events.append(Input("viewport", viewport=Viewport(640, 480, 1)))

        call(report_browser_viewport)
        for _ in range(40):
            if window.lifecycle_state != "RUNNING":
                break
            if (window.width, window.height) == (640, 480):
                break
            await asyncio.sleep(0.005)
        self.assertEqual(window.lifecycle_state, "RUNNING")
        self.assertEqual((window.width, window.height), (640, 480))
        self.assertEqual(call(lambda: window._runtime._pending_size), {})

    async def test_batch_hiding_modal_or_owner_is_atomic_and_keeps_modal_usable(self):
        window = self.make()
        parent = Container(parent=window)
        status = Label(parent=window, text="Original")
        dialog = SubWindow(parent=parent, visible=False)
        window.run(backend=RecordingHost())
        opening = dialog.show(modal=True)
        for control in (dialog, parent):
            with self.subTest(control=type(control).__name__):
                with self.assertRaises(LifecycleError):
                    control.visible = False
                with self.assertRaises(LifecycleError):
                    control.update(background="#123456", visible=False)
                with self.assertRaises(LifecycleError):
                    window.update_children({status: {"text": "Must not change"},
                                            control: {"visible": False}})
                self.assertTrue(control.visible)
                self.assertIsNone(control.background)
                self.assertEqual(status.text, "Original")
                self.assertIs(call(lambda: window._runtime.modal), dialog)
                self.assertFalse(opening.done)
        with self.assertRaises(LifecycleError):
            window.update(title="Must not change", visible=False)
        self.assertEqual(window.title, "Pysual")
        self.assertTrue(await dialog.close_async("accepted"))
        self.assertEqual((await opening.wait_async()).result, "accepted")
        self.assertTrue(call(lambda: window._runtime._modality.allowed(window)))

    async def test_custom_live_validation_runs_on_owner_for_scalar_and_batch(self):
        observed = []

        class GuardedButton(Button):
            def _validate_live_update(self, name, value):
                super()._validate_live_update(name, value)
                if name == "text":
                    observed.append((self, threading.get_ident(), self.text, value))

        window = self.make()
        button = GuardedButton(parent=window, text="Before")
        observed.clear()
        button.text = "Scalar"
        button.update(text="Batch")
        owner_thread = call(threading.get_ident)
        self.assertEqual(observed, [(button, owner_thread, "Before", "Scalar"),
                                    (button, owner_thread, "Scalar", "Batch")])

    async def test_wait_never_opened_rejects_and_closed_wait_keeps_outcome(self):
        window = self.make()
        with self.assertRaisesRegex(LifecycleError, "never"):
            window.wait()
        window.close()
        self.assertEqual(window.lifecycle_state, "CREATED")
        window.run(backend=RecordingHost())
        window.close("value")
        outcome = await window.wait_async()
        self.assertEqual(outcome.result, "value")
        self.assertEqual(window.wait(), outcome)

    async def test_run_blocking_waits_only_its_captured_opening(self):
        first, second = self.make(), self.make()
        second.run(backend=RecordingHost())
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(first.run_blocking, backend=RecordingHost())
            while not first.is_open:
                await asyncio.sleep(0.005)
            first.close()
            self.assertIs(await asyncio.wrap_future(future), first)
            self.assertTrue(second.is_open)

    async def test_blocking_on_owner_rejected_before_open(self):
        window = self.make()
        host = RecordingHost()
        with self.assertRaisesRegex(LifecycleError, "cannot run"):
            await call_async(window.run_blocking, backend=host)
        self.assertFalse(host.opened)
        self.assertEqual(window.lifecycle_state, "CREATED")

    async def test_cancelled_waiter_does_not_cancel_shared_opening(self):
        window = self.make()
        window.run(backend=RecordingHost())
        first = asyncio.create_task(window.wait_async())
        second = asyncio.create_task(window.wait_async())
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertTrue(window.is_open)
        window.close("ok")
        self.assertEqual((await second).result, "ok")

    async def test_close_is_admission_only_and_duplicate_decisions_share_first_result(
        self,
    ):
        window = self.make()
        decisions = []

        async def veto_first(event):
            decisions.append(event)
            await asyncio.sleep(0.03)
            if len(decisions) == 1:
                event.cancel()

        window.closing.connect(veto_first)
        window.run(backend=RecordingHost())
        self.assertIsNone(window.close("first"))
        self.assertFalse(await window.close_async("ignored"))
        self.assertTrue(window.is_open)
        self.assertTrue(await window.close_async("accepted"))
        self.assertEqual((await window.wait_async()).result, "accepted")
        self.assertEqual(len(decisions), 2)

    async def test_close_callback_can_observe_decision_and_completion(self):
        observed = []

        class Demo(Window):
            def build(self):
                self.go = Button(text="close")

            async def go_on_click(self, event):
                observed.append(await self.close_async())
                observed.append((await self.wait_async()).reason)

        window = self.make(Demo)
        window.run(backend=RecordingHost())
        from pysual.events import ClickEvent

        window.go.click.emit(ClickEvent(source=window.go))
        await window.wait_async()
        for _ in range(30):
            if len(observed) == 2:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(observed, [True, "closed"])

    async def test_failed_build_is_terminal_and_disposes_partial_tree(self):
        children = []

        class Broken(Window):
            def build(self):
                child = Label(parent=self)
                children.append(child)
                raise ValueError("broken build")

        window = self.make(Broken)
        host = RecordingHost()
        with self.assertRaisesRegex(ValueError, "broken build"):
            window.run(backend=host)
        self.assertFalse(host.opened)
        self.assertTrue(children[0]._disposed)
        with self.assertRaises(LifecycleError):
            window.run(backend=RecordingHost())

    async def test_failed_native_open_can_retry_without_rebuilding(self):
        builds = []

        class Demo(Window):
            def build(self):
                builds.append(1)

        class Broken(RecordingHost):
            def open(self, *args):
                super().open(*args)
                raise ValueError("open failed")

        window = self.make(Demo)
        bad_host = Broken()
        with self.assertRaisesRegex(ValueError, "open failed"):
            window.run(backend=bad_host)
        self.assertTrue(bad_host.closed)
        window.run(backend=RecordingHost())
        self.assertEqual(builds, [1])

    async def test_native_modal_result_and_unrelated_window_survives(self):
        owner, dialog, unrelated = self.make(), self.make(), self.make()
        owner_host, dialog_host = ModalHost(), ModalHost()
        owner.run(backend=owner_host)
        unrelated.run(backend=RecordingHost())
        waiter = asyncio.create_task(
            dialog.show_modal_async(owner, backend=dialog_host)
        )
        while not dialog.is_open:
            await asyncio.sleep(0.005)
        self.assertIs(dialog_host.owner, owner_host)
        with self.assertRaises(LifecycleError):
            owner.visible = False
        for window in (owner, dialog):
            with self.subTest(window=window), self.assertRaises(LifecycleError):
                window.update(title="Must not change", visible=False)
            self.assertTrue(window.visible)
            self.assertEqual(window.title, "Pysual")
        self.assertTrue(unrelated.is_open)
        dialog.close("selected")
        self.assertEqual(await waiter, "selected")
        self.assertIsNone(dialog_host.owner)
        self.assertTrue(owner.is_open)
        self.assertTrue(unrelated.is_open)

    async def test_unsupported_native_modal_guides_to_subwindow_before_opening(self):
        for supported_owner in (False, True):
            with self.subTest(supported_owner=supported_owner):
                owner, dialog = self.make(), self.make()
                owner.run(backend=ModalHost() if supported_owner else RecordingHost())
                host = RecordingHost()
                with self.assertRaisesRegex(CapabilityError, "SubWindow.show_modal"):
                    await dialog.show_modal_async(owner, backend=host)
                self.assertFalse(host.opened)
                self.assertEqual(dialog.lifecycle_state, "CREATED")

    async def test_native_owner_closes_nested_dialogs_without_child_veto(self):
        owner, child, grandchild = self.make(), self.make(), self.make()
        owner.run(backend=ModalHost())
        child_wait = asyncio.create_task(
            child.show_modal_async(owner, backend=ModalHost())
        )
        while not child.is_open:
            await asyncio.sleep(0.005)
        grand_wait = asyncio.create_task(
            grandchild.show_modal_async(child, backend=ModalHost())
        )
        while not grandchild.is_open:
            await asyncio.sleep(0.005)
        child.closing.connect(lambda event: event.cancel())
        grandchild.closing.connect(lambda event: event.cancel())
        owner.close()
        await asyncio.wait_for(owner.wait_async(), 2)
        self.assertIsNone(await child_wait)
        self.assertIsNone(await grand_wait)
        self.assertEqual((await grandchild.wait_async()).reason, "owner_closed")

    async def test_native_modal_open_failure_unwinds_owner_and_closes_host(self):
        owner, dialog = self.make(), self.make()
        owner.run(backend=ModalHost())
        failed = ModalHost(fail_modal=True)
        with self.assertRaises(CapabilityError):
            await dialog.show_modal_async(owner, backend=failed)
        self.assertTrue(failed.closed)
        self.assertTrue(owner.is_open)
        self.assertFalse(call(lambda: owner._runtime._native_blockers))

    async def test_destroy_open_is_terminal_and_ignores_veto(self):
        window = self.make()
        host = RecordingHost()
        window.closing.connect(lambda event: event.cancel())
        window.run(backend=host)
        await window.destroy_async()
        self.assertTrue(host.closed)
        self.assertEqual(window.lifecycle_state, "DESTROYED")
        with self.assertRaises(LifecycleError):
            window.run(backend=RecordingHost())

    async def test_global_wait_includes_windows_opened_while_waiting(self):
        first, second = self.make(), self.make()
        first.run(backend=RecordingHost())
        waiter = asyncio.create_task(wait_windows_async())
        await asyncio.sleep(0.01)
        second.run(backend=RecordingHost())
        first.close()
        await first.wait_async()
        self.assertFalse(waiter.done())
        second.close()
        await asyncio.wait_for(waiter, 2)
        wait_windows(1)

    async def test_global_wait_failure_is_consumed_but_window_result_is_retained(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                window = self.make()
                error = RuntimeError("loaded failed")

                def fail(event):
                    raise error

                window.loaded.connect(fail)
                window.run(backend=RecordingHost())
                with self.assertRaises(RuntimeError) as caught:
                    if asynchronous:
                        await wait_windows_async()
                    else:
                        wait_windows(2)
                self.assertIs(caught.exception, error)
                self.assertFalse(window.is_open)
                wait_windows(0)
                await wait_windows_async()
                with self.assertRaises(RuntimeError) as caught:
                    await window.wait_async()
                self.assertIs(caught.exception, error)

    async def test_global_wait_preserves_failure_for_existing_waiters_after_timeout(self):
        window = self.make()
        trigger = threading.Event()
        error = TimeoutError("window failure, not a wait timeout")

        async def fail(event):
            while not trigger.is_set():
                await asyncio.sleep(0.001)
            raise error

        window.loaded.connect(fail)
        window.run(backend=RecordingHost())
        with self.assertRaises(TimeoutError):
            wait_windows(0)
        joined = asyncio.Event()
        snapshots = []
        original = window_runtime._wait_for_idle

        async def waiting(future):
            snapshots.append(future)
            if len(snapshots) == 2:
                joined.set()
            await original(future)

        with patch.object(window_runtime, "_wait_for_idle", waiting):
            waiters = [asyncio.create_task(wait_windows_async()) for _ in range(2)]
            await asyncio.wait_for(joined.wait(), 2)
            trigger.set()
            errors = await asyncio.wait_for(
                asyncio.gather(*waiters, return_exceptions=True), 2
            )
        self.assertIs(snapshots[0], snapshots[1])
        self.assertEqual(errors, [error, error])
        wait_windows(0)
        await wait_windows_async()

    async def test_global_wait_retains_unobserved_failure_across_later_openings(self):
        for asynchronous in (False, True):
            with self.subTest(asynchronous=asynchronous):
                first, second = self.make(), self.make()
                error = RuntimeError("unobserved earlier failure")

                def fail(event):
                    raise error

                first.loaded.connect(fail)
                first.run(backend=RecordingHost())
                with self.assertRaises(RuntimeError):
                    await first.wait_async()
                second.run(backend=RecordingHost())
                # The earlier failure must not make a new wait return before
                # this active period becomes idle, nor be consumed by timeout.
                with self.assertRaises(TimeoutError):
                    wait_windows(0)
                second.close()
                await second.wait_async()
                with self.assertRaises(RuntimeError) as caught:
                    if asynchronous:
                        await wait_windows_async()
                    else:
                        wait_windows(0)
                self.assertIs(caught.exception, error)
                wait_windows(0)
                await wait_windows_async()

    async def test_delayed_global_wait_consumes_failure_carried_to_later_idle(self):
        first, second = self.make(), self.make()
        error = RuntimeError("earlier opening failed")

        def fail(event):
            raise error

        first.loaded.connect(fail)
        first.run(backend=RecordingHost())
        old_idle = call(lambda: window_runtime._idle)
        with self.assertRaises(RuntimeError):
            await first.wait_async()
        second.run(backend=RecordingHost())
        second.close()
        await second.wait_async()
        with self.assertRaises(RuntimeError) as caught:
            await window_runtime._wait_for_idle(old_idle)
        self.assertIs(caught.exception, error)
        await wait_windows_async()
        wait_windows(0)

    async def test_delayed_global_wait_keeps_new_active_period_failure(self):
        first, second, third = self.make(), self.make(), self.make()
        first_error = RuntimeError("earlier opening failed")
        second_error = RuntimeError("new active period failed")
        trigger = threading.Event()

        def first_fail(event):
            raise first_error

        async def second_fail(event):
            while not trigger.is_set():
                await asyncio.sleep(0.001)
            raise second_error

        first.loaded.connect(first_fail)
        first.run(backend=RecordingHost())
        old_idle = call(lambda: window_runtime._idle)
        with self.assertRaises(RuntimeError):
            await first.wait_async()
        second.loaded.connect(second_fail)
        second.run(backend=RecordingHost())
        third.run(backend=RecordingHost())
        trigger.set()
        with self.assertRaises(RuntimeError):
            await second.wait_async()
        with self.assertRaises(RuntimeError) as caught:
            await window_runtime._wait_for_idle(old_idle)
        self.assertIs(caught.exception, first_error)
        with self.assertRaises(TimeoutError):
            wait_windows(0)
        third.close()
        with self.assertRaises(RuntimeError) as caught:
            await wait_windows_async()
        self.assertIs(caught.exception, second_error)
        wait_windows(0)

    async def test_old_waiter_cannot_consume_new_occurrence_of_same_exception(self):
        first, second = self.make(), self.make()
        error = RuntimeError("reused exception")

        def fail(event):
            raise error

        first.loaded.connect(fail)
        first.run(backend=RecordingHost())
        old_idle = call(lambda: window_runtime._idle)
        with self.assertRaises(RuntimeError):
            await wait_windows_async()
        second.loaded.connect(fail)
        second.run(backend=RecordingHost())
        with self.assertRaises(RuntimeError):
            await second.wait_async()
        with self.assertRaises(RuntimeError):
            await window_runtime._wait_for_idle(old_idle)
        with self.assertRaises(RuntimeError) as caught:
            await wait_windows_async()
        self.assertIs(caught.exception, error)
        wait_windows(0)

    async def test_global_wait_retains_only_oldest_unobserved_idle_failure(self):
        errors = [RuntimeError("first failure"), RuntimeError("later failure")]
        windows = []
        for error in errors:
            window = self.make()
            windows.append(window)

            def fail(event):
                raise error

            window.loaded.connect(fail)
            window.run(backend=RecordingHost())
            with self.assertRaises(RuntimeError):
                await window.wait_async()
        with self.assertRaises(RuntimeError) as caught:
            await wait_windows_async()
        self.assertIs(caught.exception, errors[0])
        wait_windows(0)
        for window, error in zip(windows, errors):
            with self.assertRaises(RuntimeError) as caught:
                await window.wait_async()
            self.assertIs(caught.exception, error)

    async def test_delayed_global_wait_failure_does_not_replace_new_active_period(self):
        first, second = self.make(), self.make()
        error = RuntimeError("earlier opening failed")

        def fail(event):
            raise error

        first.loaded.connect(fail)
        first.run(backend=RecordingHost())
        old_idle = call(lambda: window_runtime._idle)
        with self.assertRaises(RuntimeError):
            await first.wait_async()
        second.run(backend=RecordingHost())
        with self.assertRaises(RuntimeError) as caught:
            await window_runtime._wait_for_idle(old_idle)
        self.assertIs(caught.exception, error)
        with self.assertRaises(TimeoutError):
            wait_windows(0)
        second.close()
        await wait_windows_async()

    async def test_cancelling_global_wait_after_one_window_fails_keeps_diagnostic(self):
        owner_loop = get_engine().loop
        previous = call(owner_loop.get_exception_handler)
        reports = []
        call(owner_loop.set_exception_handler, lambda loop, context: reports.append(context))
        self.addCleanup(call, owner_loop.set_exception_handler, previous)
        caller_loop = asyncio.get_running_loop()
        previous_caller = caller_loop.get_exception_handler()
        bridge_errors = []
        caller_loop.set_exception_handler(lambda loop, context: bridge_errors.append(context))
        self.addCleanup(caller_loop.set_exception_handler, previous_caller)
        first, second = self.make(), self.make()
        trigger = threading.Event()
        error = ValueError("first window failed")

        async def fail(event):
            while not trigger.is_set():
                await asyncio.sleep(0.001)
            raise error

        first.loaded.connect(fail)
        first.run(backend=RecordingHost())
        second.run(backend=RecordingHost())
        waiter = asyncio.create_task(wait_windows_async())
        await asyncio.sleep(0.01)
        trigger.set()
        async with asyncio.timeout(2):
            while first.lifecycle_state != "FAILED":
                await asyncio.sleep(0.001)
        await call_async(asyncio.sleep, 0)
        self.assertFalse(waiter.done())
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        second.close()
        await second.wait_async()
        await asyncio.sleep(0)
        self.assertEqual([report["exception"] for report in reports], [error])
        self.assertEqual(reports[0]["message"], "Pysual window failed")
        self.assertEqual(bridge_errors, [])
        with self.assertRaises(ValueError) as caught:
            await wait_windows_async()
        self.assertIs(caught.exception, error)
        await wait_windows_async()

    async def test_host_services_distinguish_closed_window_from_unsupported_host(self):
        window = self.make()
        operations = {
            "read_session": lambda: window.read_session("draft"),
            "write_session": lambda: window.write_session("draft", "text"),
            "open_url": lambda: window.open_url("https://example.org"),
            "open_text_file": window.open_text_file,
            "save_text_file": lambda: window.save_text_file("text"),
        }
        for phase in ("created", "open", "closed"):
            if phase == "open":
                window.run(backend=RecordingHost())
            elif phase == "closed":
                await window.close_async()
                await window.wait_async()
            expected = CapabilityError if phase == "open" else LifecycleError
            for name, operation in operations.items():
                with self.subTest(phase=phase, operation=name):
                    with self.assertRaises(expected):
                        await operation()

    async def test_closed_host_services_preserve_argument_validation(self):
        window = self.make()
        operations = (
            lambda: window.read_session(""),
            lambda: window.write_session("draft", 123),
            lambda: window.open_url("relative/path"),
            lambda: window.save_text_file("text", suggested_name="../file.txt"),
        )
        for operation in operations:
            with self.assertRaises(ValueError):
                await operation()

    async def test_first_present_must_acknowledge_before_run_returns(self):
        entered, release = threading.Event(), threading.Event()

        class Delayed(RecordingHost):
            def present(self):
                entered.set()
                if not release.wait(2):
                    raise TimeoutError("test did not acknowledge presentation")
                super().present()

        window = self.make()
        host = Delayed()
        with ThreadPoolExecutor(1) as pool:
            opening = pool.submit(window.run, backend=host)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertFalse(opening.done())
            finally:
                release.set()
            self.assertIs(await asyncio.wrap_future(opening), window)
        self.assertGreaterEqual(host.frames, 1)

    async def test_loaded_handler_can_open_another_window(self):
        first, second = self.make(), self.make()
        second_host = RecordingHost()
        first.loaded.connect(lambda event: second.run(backend=second_host))
        first.run(backend=RecordingHost())
        for _ in range(30):
            if second.is_open:
                break
            await asyncio.sleep(0.005)
        self.assertTrue(second.is_open)
        self.assertGreaterEqual(second_host.frames, 1)

    async def test_close_confirmation_blocks_existing_native_modal(self):
        owner, dialog, confirmation = self.make(), self.make(), self.make()
        owner_host, dialog_host, confirm_host = ModalHost(), ModalHost(), ModalHost()
        owner.run(backend=owner_host)
        dialog_wait = asyncio.create_task(
            dialog.show_modal_async(owner, backend=dialog_host)
        )
        while not dialog.is_open:
            await asyncio.sleep(0.005)

        async def confirm_close(event):
            if not await confirmation.show_modal_async(owner, backend=confirm_host):
                event.cancel()

        owner.closing.connect(confirm_close)
        close_wait = asyncio.create_task(owner.close_async())
        while not confirmation.is_open:
            await asyncio.sleep(0.005)
        self.assertIs(confirm_host.owner, dialog_host)
        confirmation.close(True)
        self.assertTrue(await close_wait)
        await owner.wait_async()
        self.assertIsNone(await dialog_wait)

    async def test_destroy_during_pending_close_settles_decision(self):
        window = self.make()
        started = threading.Event()

        async def pending_decision(event):
            started.set()
            await asyncio.Event().wait()

        window.closing.connect(pending_decision)
        window.run(backend=RecordingHost())
        decision = asyncio.create_task(window.close_async())
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        await window.destroy_async()
        self.assertTrue(await decision)

    async def test_closed_callback_can_reopen_but_cannot_mutate_new_generation(self):
        window = self.make()
        next_host = RecordingHost()
        observed = []

        def reopen(event):
            if observed:
                return
            window.run(backend=next_host)
            try:
                window.title = "stale mutation"
            except LifecycleError:
                observed.append("reopened and guarded")

        window.closed.connect(reopen)
        window.run(backend=RecordingHost())
        first = call(window._capture_presentation)
        window.close()
        await first.wait_async()
        for _ in range(30):
            if observed:
                break
            await asyncio.sleep(0.005)
        self.assertEqual(observed, ["reopened and guarded"])
        self.assertTrue(window.is_open)
        self.assertGreaterEqual(next_host.frames, 1)
