"""Clipboard bounds are recoverable service failures, without touching the OS clipboard."""
import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from _ui_testcase import AsyncUIOwnerTestCase
from test_library import RecordingHost, eventually
from pysual import App
from pysual.backends._native_client import NativeClient, MAX_PAYLOAD
from pysual.backends.native import NativeHost
from pysual.backends._web_native import LiveSVGHost
from pysual.backends.term_text import TermTextHost
from pysual.host import CapabilityError, Input, MAX_TEXT_BYTES
from pysual.runtime import Runtime


class ClipboardLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_local_limits_reject_without_starting_or_sending(self):
        host = NativeHost()
        client = NativeClient()
        client._start = Mock(side_effect=AssertionError("Must not start a process"))
        client._send = Mock(side_effect=AssertionError("Must not send clipboard data"))
        host._request = client.request
        for value in ("x" * (MAX_TEXT_BYTES + 1), "\x01" * (3 * 1024 * 1024), "\ud800"):
            with self.subTest(length=len(value)), self.assertRaises(CapabilityError):
                await host.clipboard_write(value)
        self.assertFalse(client._closed.is_set())
        self.assertEqual(client.diagnostics["requests_sent"], 0)
        client._start.assert_not_called()
        client._send.assert_not_called()
        with self.assertRaises(ValueError):
            client.request("clipboard_write", text="x" * MAX_PAYLOAD)
        self.assertFalse(client._closed.is_set())
        client.close()

    async def test_native_denial_is_recoverable_but_timeout_policy_is_unchanged(self):
        host = NativeHost()
        for method, arguments in ((host.clipboard_read, ()), (host.clipboard_write, ("test",))):
            host._request = Mock(side_effect=RuntimeError("clipboard unavailable"))
            with self.assertRaises(CapabilityError):
                await method(*arguments)
            host._request = Mock(side_effect=TimeoutError("unknown commit"))
            with self.assertRaises(TimeoutError):
                await method(*arguments)
        host._request = Mock(return_value="x" * (MAX_TEXT_BYTES + 1))
        with self.assertRaises(CapabilityError):
            await host.clipboard_read()

    async def test_web_and_terminal_limits_never_call_platform_clipboard(self):
        web = LiveSVGHost(open_browser=False)
        web._rpc = AsyncMock(side_effect=AssertionError("Must not call browser clipboard"))
        terminal = TermTextHost()
        terminal._clipboard_command = Mock(side_effect=AssertionError("Must not discover OS clipboard"))
        for host in (web, terminal):
            with self.assertRaises(CapabilityError):
                await host.clipboard_write("x" * (MAX_TEXT_BYTES + 1))
        with self.assertRaises(CapabilityError):
            await web.clipboard_write("\ud800")
        web._rpc.assert_not_called()
        terminal._clipboard_command.assert_not_called()


class ClipboardRuntimeTests(AsyncUIOwnerTestCase):
    async def test_rejected_native_cut_preserves_selection_and_runtime(self):
        app = App()
        entry = app.text_box(text="retain selected text")
        host = RecordingHost()
        native = NativeHost()
        native._request = Mock(side_effect=ValueError("Request exceeds the 16 MiB protocol limit"))
        host.clipboard_write = native.clipboard_write
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            entry.focus()
            entry.select_all()
            host.events.append(Input("key_down", key="x", ctrl=True))
            await eventually(lambda: native._request.call_count == 1)
            await asyncio.sleep(.025)
            runtime.dispatcher.raise_errors()
            self.assertFalse(task.done())
            self.assertTrue(app.is_open)
            self.assertEqual(entry.selection_text, "retain selected text")
        finally:
            runtime._stop.set()
            await task
            app.destroy()
