"""Caller-side free-function conventions through public window gateways."""

import asyncio
import code
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import gc
import io
import threading
import unittest
import weakref

from pysual import Button, ClosingEvent, Container, Label, Window
from pysual._engine import call, call_async, is_ui_thread
from pysual.errors import BindingError
from test_library import RecordingHost
from test_window_lifecycle import ModalHost


class NamespaceHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.windows = []
        self.assertFalse(is_ui_thread())

    def make(self, cls=Window):
        window = cls()
        self.windows.append(window)
        return window

    async def asyncTearDown(self):
        for window in reversed(self.windows):
            async with asyncio.timeout(3):
                await window.destroy_async()

    def dispatcher(self, window):
        return call(lambda: window._runtime.dispatcher)

    async def drain(self, dispatcher):
        async with asyncio.timeout(3):
            await call_async(dispatcher.drain)

    async def click(self, window, button=None):
        dispatcher = self.dispatcher(window)
        (window.action_button if button is None else button).activate()
        await self.drain(dispatcher)

    async def test_direct_modal_calls_discover_root_and_child_handlers_on_reopening(self):
        owner = self.make()
        owner.run(backend=ModalHost())
        seen = []

        class Dialog(Window):
            def action_button_on_click(self, event):
                seen.append("method")

        dialog = self.make(Dialog)
        dialog.action_button = Button()

        def dialog_on_loaded(event):
            seen.append("loaded")
            dialog.action_button.activate()

        def dialog_action_button_on_click(event):
            seen.append("namespace")

        def explicit(event):
            seen.append("explicit")
            dialog.close("done")

        dialog.action_button.click.connect(explicit)
        dialog.action_button.click.connect(dialog_action_button_on_click)
        # A watchdog makes a regression fail rather than hang the synchronous call.
        for gateway in ("sync", "async", "async"):
            seen.clear()
            watchdog = threading.Timer(3, lambda: dialog.close("watchdog"))
            watchdog.start()
            try:
                if gateway == "sync":
                    result = dialog.show_modal(owner, backend=ModalHost())
                else:
                    result = await dialog.show_modal_async(owner, backend=ModalHost())
            finally:
                watchdog.cancel()
            self.assertEqual(result, "done")
            self.assertEqual(seen, ["loaded", "method", "namespace", "explicit"])

    async def test_scheduled_modal_keeps_explicit_subscriptions(self):
        owner, dialog = self.make(), self.make()
        owner.run(backend=ModalHost())
        dialog.loaded.connect(lambda e: dialog.close("explicit"))
        async with asyncio.timeout(3):
            result = await asyncio.create_task(dialog.show_modal_async(owner, backend=ModalHost()))
        self.assertEqual(result, "explicit")

    async def test_interactive_console_example_needs_no_subclass_or_connect(self):
        errors = []

        class Console(code.InteractiveConsole):
            def write(self, text):
                errors.append(text)

        console = Console({"Window": Window, "Label": Label, "Button": Button,
                           "host": RecordingHost()})
        statements = (
            "window = Window()",
            "window.message = Label(text='Ready')",
            "window.hello_button = Button(text='Hello')",
            "def window_hello_button_on_click(event):",
            "    window.message.text = 'Hello from the prompt'",
            "",
            "window.run(backend=host)",
        )
        with redirect_stdout(io.StringIO()):
            for statement in statements:
                pending = console.push(statement)
                if "window" in console.locals and not self.windows:
                    self.windows.append(console.locals["window"])
        self.assertFalse(pending)
        self.assertEqual(errors, [])
        window = console.locals["window"]
        await self.click(window, window.hello_button)
        self.assertEqual(window.message.text, "Hello from the prompt")

    async def test_late_async_function_binds_children_created_during_build(self):
        caller_thread = threading.get_ident()
        observed = []

        class BuiltWindow(Window):
            def build(self):
                self.action_button = Button(text="Click")

        window = self.make(BuiltWindow)

        async def window_action_button_on_click(event):
            await asyncio.sleep(0)
            observed.append((event.source, threading.get_ident(), is_ui_thread()))

        window.run(backend=RecordingHost())
        await self.click(window)
        self.assertEqual(len(observed), 1)
        self.assertIs(observed[0][0], window.action_button)
        self.assertNotEqual(observed[0][1], caller_thread)
        self.assertTrue(observed[0][2])

    async def test_local_callback_shadows_global_callback(self):
        window = self.make()
        window.action_button = Button()
        seen = []
        global_namespace = {"window": window, "window_action_button_on_click": lambda e: seen.append("global")}
        local_namespace = {"host": RecordingHost(), "window_action_button_on_click": lambda e: seen.append("local")}
        exec("window.run(backend=host)", global_namespace, local_namespace)
        await self.click(window)
        self.assertEqual(seen, ["local"])

    async def test_local_window_alias_shadows_global_alias_by_identity(self):
        target, other = self.make(), self.make()
        target.action_button = Button()
        seen = []
        global_namespace = {"window": target, "window_action_button_on_click": lambda e: seen.append("wrong")}
        local_namespace = {"window": other, "target": target, "host": RecordingHost()}
        exec("target.run(backend=host)", global_namespace, local_namespace)
        await self.click(target)
        self.assertEqual(seen, [])

    async def test_distinct_windows_discover_only_their_own_aliases(self):
        alpha, beta = self.make(), self.make()
        alpha.action_button, beta.action_button = Button(), Button()
        seen = []
        namespace = {"alpha": alpha, "beta": beta,
                     "alpha_host": RecordingHost(), "beta_host": RecordingHost(),
                     "alpha_action_button_on_click": lambda e: seen.append("alpha"),
                     "beta_action_button_on_click": lambda e: seen.append("beta")}
        exec("alpha.run(backend=alpha_host)\nbeta.run(backend=beta_host)", namespace)
        await self.click(alpha)
        await self.click(beta)
        self.assertEqual(seen, ["alpha", "beta"])

    async def test_same_callback_aliases_deduplicate_and_child_aliases_do_not_bind(self):
        window = self.make()
        window.action_button = Button()
        window.alias = window.action_button
        seen = []

        def window_action_button_on_click(event):
            seen.append("canonical")

        def window_alias_on_click(event):
            seen.append("child alias")

        alias = window
        alias_action_button_on_click = window_action_button_on_click
        window.run(backend=RecordingHost())
        await self.click(window)
        self.assertEqual(seen, ["canonical"])

    async def test_conflicting_root_aliases_fail_before_replacing_active_bindings(self):
        window = self.make()
        window.action_button = Button()
        seen = []

        def window_action_button_on_click(event):
            seen.append("original")

        window.action_button.click.connect(lambda e: seen.append("explicit"))
        window.run(backend=RecordingHost())
        alias = window

        def alias_action_button_on_click(event):
            seen.append("conflicting")

        with self.assertRaisesRegex(BindingError, "same event"):
            window.run()
        self.assertTrue(window.is_open)
        await self.click(window)
        self.assertEqual(seen, ["original", "explicit"])

    async def test_flattened_nested_path_collision_is_atomic(self):
        window = self.make()
        window.action_button = Button()
        seen = []

        def window_action_button_on_click(event):
            seen.append("original")

        window.run(backend=RecordingHost())
        window.tools_save = Button()
        window.tools = Container()
        window.tools.save = Button()

        def window_tools_save_on_click(event):
            seen.append("ambiguous")

        with self.assertRaisesRegex(BindingError, "multiple controls/events"):
            window.run()
        await self.click(window)
        await self.click(window, window.tools_save)
        await self.click(window, window.tools.save)
        self.assertEqual(seen, ["original"])

    async def test_invalid_handlers_preserve_namespace_and_explicit_subscriptions(self):
        window = self.make()
        window.action_button = Button()
        seen = []

        def original(event):
            seen.append("original")

        def extra_argument(event, unexpected):
            pass

        def wrong_event(event: ClosingEvent):
            pass

        namespace = {"window": window, "host": RecordingHost(), "window_action_button_on_click": original}
        window.action_button.click.connect(lambda e: seen.append("explicit"))
        exec("window.run(backend=host)", namespace)
        for invalid in (extra_argument, wrong_event, 42):
            with self.subTest(invalid=invalid):
                namespace["window_action_button_on_click"] = invalid
                with self.assertRaises((BindingError, TypeError, ValueError)):
                    exec("window.run()", namespace)
                seen.clear()
                await self.click(window)
                self.assertEqual(seen, ["original", "explicit"])

    async def test_method_namespace_and_explicit_callbacks_keep_order_and_deduplicate(self):
        seen = []

        class MethodWindow(Window):
            def action_button_on_click(self, event):
                seen.append("method")

        window = self.make(MethodWindow)
        window.action_button = Button()

        def window_action_button_on_click(event):
            seen.append("namespace")

        window.action_button.click.connect(lambda e: seen.append("explicit"))
        window.action_button.click.connect(window_action_button_on_click)
        window.action_button.click.connect(window.action_button_on_click)
        window.run(backend=RecordingHost())
        await self.click(window)
        self.assertEqual(seen, ["method", "namespace", "explicit"])
        handlers = window.action_button.inspect().handlers
        self.assertEqual([handler.convention for handler in handlers], [True, True, False])

    async def test_repeat_run_refreshes_then_removes_only_namespace_callback(self):
        window = self.make()
        window.action_button = Button()
        seen, loaded = [], []

        def window_action_button_on_click(event):
            seen.append("first")

        window.loaded.connect(lambda e: loaded.append(e.source))
        window.action_button.click.connect(lambda e: seen.append("explicit"))
        host = RecordingHost()
        window.run(backend=host)
        await self.click(window)
        presentation = call(window._capture_presentation)

        def window_action_button_on_click(event):
            seen.append("replacement")

        self.assertIs(window.run(), window)
        self.assertIs(call(window._capture_presentation), presentation)
        await self.click(window)
        del window_action_button_on_click
        window.run()
        await self.click(window)
        self.assertEqual(seen, ["first", "explicit", "replacement", "explicit", "explicit"])
        self.assertEqual(loaded, [window])

    async def test_no_direct_window_alias_preserves_current_namespace_bindings(self):
        window = self.make()
        window.action_button = Button()
        seen = []

        def window_action_button_on_click(event):
            seen.append("preserved")

        window.run(backend=RecordingHost())
        exec("holder[0].run()", {"holder": [window]})
        await self.click(window)
        self.assertEqual(seen, ["preserved"])

    async def test_window_lifecycle_callbacks_survive_close_and_reopen(self):
        window = self.make()
        window.action_button = Button()
        retained = window.action_button
        seen = []

        def window_on_loaded(event):
            seen.append(("loaded", event.source))

        def window_on_closed(event):
            seen.append(("closed", event.source))

        def window_action_button_on_click(event):
            seen.append(("clicked", event.source))

        for _ in range(2):
            window.run(backend=RecordingHost())
            dispatcher = self.dispatcher(window)
            await self.drain(dispatcher)
            await self.click(window)
            async with asyncio.timeout(3):
                self.assertTrue(await window.close_async())
                await window.wait_async()
            await self.drain(dispatcher)
        self.assertIs(window.action_button, retained)
        self.assertEqual(seen, [("loaded", window), ("clicked", retained), ("closed", window)] * 2)

    async def test_run_blocking_binds_free_loaded_handler_that_closes_window(self):
        target = self.make()
        host = RecordingHost()
        seen = []

        def worker():
            window = target

            def window_on_loaded(event):
                seen.append(event.source)
                event.source.close("free loaded handler")

            return window.run_blocking(backend=host)

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(worker)
        try:
            result = await asyncio.wait_for(asyncio.wrap_future(future), 3)
            self.assertIs(result, target)
            self.assertEqual(target.wait(1).result, "free loaded handler")
            self.assertEqual(seen, [target])
            self.assertTrue(host.closed)
        finally:
            if not future.done():
                target.close()
                future.result(timeout=3)
            pool.shutdown(wait=False, cancel_futures=True)

    async def test_namespace_snapshot_does_not_retain_unrelated_caller_locals(self):
        window = self.make()
        window.action_button = Button()
        seen = []

        class Payload:
            pass

        def launch():
            payload = Payload()
            reference = weakref.ref(payload)

            def window_action_button_on_click(event):
                seen.append("called")

            window.run(backend=RecordingHost())
            return reference

        reference = launch()
        gc.collect()
        self.assertIsNone(reference(), "Caller locals outlived namespace discovery")
        await self.click(window)
        self.assertEqual(seen, ["called"])

    async def test_refresh_preserves_callable_snapshot_for_already_queued_event(self):
        window = self.make()
        window.action_button = Button()
        started, release = call(asyncio.Event), call(asyncio.Event)
        seen = []

        async def window_action_button_on_click(event):
            started.set()
            await release.wait()
            seen.append("original")

        window.run(backend=RecordingHost())
        dispatcher = self.dispatcher(window)
        window.action_button.activate()
        async with asyncio.timeout(3):
            await call_async(started.wait)

        def window_action_button_on_click(event):
            seen.append("replacement")

        window.run()
        window.action_button.activate()
        call(release.set)
        await self.drain(dispatcher)
        self.assertCountEqual(seen, ["original", "replacement"])


if __name__ == "__main__":
    unittest.main()
