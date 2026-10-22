"""Chooser modality belongs to one opening, including cancellation and reopen."""

import asyncio
import contextlib
import os

from _ui_testcase import AsyncUIOwnerTestCase

from pysual import Button, Window
from pysual.errors import LifecycleError
from pysual.host import CapabilityError, Input, TextFile
from pysual.runtime import wait as wait_windows
from test_library import RecordingHost, eventually


class DialogHost(RecordingHost):
    capabilities = RecordingHost.capabilities | {"path_dialogs", "text_files"}

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.result = asyncio.get_running_loop().create_future()

    async def choose_path(self, **options):
        self.started.set()
        return await self.result

    async def open_text_file(self):
        path = await self.choose_path()
        return None if path is None else TextFile("notes.txt", "notes", path)

    async def save_text_file(self, text, suggested_name, location):
        path = location or await self.choose_path()
        return None if path is None else TextFile(suggested_name, text, path)


class DialogWindow(Window):
    def build(self):
        self.activate_button = Button(parent=self, text="Activate")


class PathDialogModalityTests(AsyncUIOwnerTestCase):
    def setUp(self):
        self.windows = []
        self.tasks = []

    def open_window(self, host=None):
        window = DialogWindow()
        self.windows.append(window)
        host = host or DialogHost()
        window.run(backend=host)
        return window, host

    def start(self, operation):
        task = asyncio.create_task(operation)
        self.tasks.append(task)
        return task

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for window in reversed(self.windows):
            await window.destroy_async()

    async def test_dialog_blocks_input_clears_capture_and_restores_focus(self):
        window, host = self.open_window()
        other, other_host = self.open_window(RecordingHost())
        clicked, other_clicked = [], []
        window.activate_button.click.connect(lambda event: clicked.append(True))
        other.activate_button.click.connect(lambda event: other_clicked.append(True))
        window.activate_button.focus()
        other.activate_button.focus()
        runtime = window._runtime
        bounds = window.activate_button.bounds
        runtime.router.process(Input("pointer_down", bounds.x + 4, bounds.y + 4))
        self.assertIs(runtime.router.capture, window.activate_button)

        operation = self.start(window.choose_path())
        await asyncio.wait_for(host.started.wait(), 2)
        self.assertIsNone(runtime.router.capture)
        self.assertIsNone(runtime.router.focus)
        self.assertIsNone(runtime.router.hover)
        host.events.extend([
            Input("pointer_up", bounds.x + 4, bounds.y + 4),
            Input("key_down", key="Enter"),
            Input("key_up", key="Enter"),
        ])
        other_host.events.extend([
            Input("key_down", key="Enter"), Input("key_up", key="Enter"),
        ])
        await eventually(lambda: other_clicked and not host.events)
        self.assertEqual(clicked, [])

        # Complete before the pump gets another turn. Queued interaction must
        # be discarded by the chooser scope, not replayed after focus returns.
        host.events.extend([
            Input("key_down", key="Enter"), Input("key_up", key="Enter"),
        ])
        host.result.set_result(os.path.abspath("notes.txt"))
        self.assertEqual(await operation, os.path.abspath("notes.txt"))
        self.assertIs(runtime.router.focus, window.activate_button)
        await asyncio.sleep(0.03)
        self.assertEqual(clicked, [])
        host.events.extend([
            Input("key_down", key="Enter"), Input("key_up", key="Enter"),
        ])
        await eventually(lambda: clicked)

    async def test_cancellation_releases_modality_and_file_busy(self):
        window, host = self.open_window()
        window.activate_button.focus()
        operation = self.start(window.choose_path())
        await asyncio.wait_for(host.started.wait(), 2)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertFalse(window._file_busy)
        self.assertFalse(window._runtime._path_dialog_active)
        self.assertIs(window._runtime.router.focus, window.activate_button)

    async def test_close_event_is_delivered_while_dialog_is_open(self):
        window, host = self.open_window()
        operation = self.start(window.choose_path())
        await asyncio.wait_for(host.started.wait(), 2)
        host.events.append(Input("close"))
        await asyncio.wait_for(window.wait_async(), 2)
        self.assertFalse(window.is_open)
        host.result.set_result(os.path.abspath("late.txt"))
        self.assertIsNone(await operation)

    async def test_old_dialog_cannot_unlock_or_return_into_a_reopened_window(self):
        for operation_name in ("choose_path", "open_text_file", "save_text_file"):
            with self.subTest(operation=operation_name):
                window, first_host = self.open_window()
                old = self.start(
                    window.save_text_file("notes") if operation_name == "save_text_file"
                    else getattr(window, operation_name)()
                )
                await asyncio.wait_for(first_host.started.wait(), 2)
                await window.close_async()
                await window.wait_async()
                second_host = DialogHost()
                window.run(backend=second_host)
                current = self.start(window.choose_path())
                await asyncio.wait_for(second_host.started.wait(), 2)

                first_host.result.set_result(os.path.abspath("old.txt"))
                completed = await old
                if operation_name == "choose_path":
                    self.assertIsNone(completed)
                else:
                    self.assertIsInstance(completed, TextFile)
                self.assertTrue(window._file_busy)
                self.assertTrue(window._runtime._path_dialog_active)
                with self.assertRaises(LifecycleError):
                    await window.choose_path()
                second_host.result.set_result(None)
                self.assertIsNone(await current)
                self.assertFalse(window._file_busy)
                await window.close_async()
                await window.wait_async()

    async def test_cleanup_failure_does_not_replace_dialog_error(self):
        window, host = self.open_window()
        runtime = window._runtime
        original_poll = host.poll

        def failed_poll():
            raise RuntimeError("host poll failed")

        error = CapabilityError("dialog failed")
        with self.assertRaises(CapabilityError) as caught:
            try:
                with runtime.path_dialog():
                    host.poll = failed_poll
                    raise error
            finally:
                host.poll = original_poll
        self.assertIs(caught.exception, error)
        self.assertIn("host poll failed", error.__notes__[0])
        self.assertFalse(runtime._path_dialog_active)

    async def test_cleanup_failure_does_not_replace_a_completed_save(self):
        window, host = self.open_window()
        runtime = window._runtime
        original_poll = host.poll
        error = RuntimeError("host poll failed")

        def failed_poll():
            raise error

        async def committed_save(text, suggested_name, location):
            host.poll = failed_poll
            return TextFile(suggested_name, text, "committed.txt")

        host.save_text_file = committed_save
        try:
            saved = await window.save_text_file("notes")
        finally:
            host.poll = original_poll
        self.assertEqual(saved, TextFile("document.txt", "notes", "committed.txt"))
        self.assertFalse(window._file_busy)
        self.assertFalse(runtime._path_dialog_active)
        # The failure still reaches the window's normal error channel on the
        # next pump turn, independently of the successful file operation.
        with self.assertRaises(RuntimeError) as caught:
            await window.wait_async()
        self.assertIs(caught.exception, error)
        with self.assertRaises(RuntimeError):
            wait_windows(0)
        window.destroy()
        self.windows.remove(window)

    async def test_text_pickers_are_modal_and_direct_save_is_not(self):
        for operation_name in ("open_text_file", "save_text_file"):
            with self.subTest(operation=operation_name):
                window, host = self.open_window()
                operation = self.start(
                    window.open_text_file() if operation_name == "open_text_file"
                    else window.save_text_file("notes")
                )
                await asyncio.wait_for(host.started.wait(), 2)
                self.assertTrue(window._runtime._path_dialog_active)
                host.result.set_result(None)
                self.assertIsNone(await operation)
                self.assertFalse(window._runtime._path_dialog_active)
                window.activate_button.focus()
                saved = await window.save_text_file("notes", location="existing.txt")
                self.assertIsNotNone(saved)
                self.assertIs(window._runtime.router.focus, window.activate_button)
