"""Terminal dialog ownership survives cancellation and host reuse."""

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _terminal import MemoryTerminal, until
from pysual.backends import _files
from pysual.backends.term_text import TermTextHost
from pysual.host import Input


class TerminalDialogLifetimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = TermTextHost(io=MemoryTerminal(80, 24), probe_timeout=0)
        self.host.open("paths", 640, 384, False, 1)

    def tearDown(self):
        self.host.close()

    async def test_old_dialog_cleanup_preserves_reopened_hosts_modal_state(self):
        started = [asyncio.Event(), asyncio.Event()]
        release = [asyncio.Event(), asyncio.Event()]
        calls = 0

        async def dialog(*args, **kwargs):
            nonlocal calls
            index = calls
            calls += 1
            started[index].set()
            await release[index].wait()
            return None

        tasks = []
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "choose_path", dialog),
            patch.object(_files, "cancel_dialog"),
        ):
            try:
                tasks.append(asyncio.create_task(self.host.choose_path()))
                await asyncio.wait_for(started[0].wait(), 2)
                self.host.close()
                self.host.open("reopened", 640, 384, False, 1)
                tasks.append(asyncio.create_task(self.host.choose_path()))
                await asyncio.wait_for(started[1].wait(), 2)

                release[0].set()
                self.assertIsNone(await asyncio.wait_for(tasks[0], 2))
                self.assertTrue(self.host._modal_dialog)
                self.host._pending.append(Input("key_down", key="a"))
                self.assertFalse(any(e.kind == "key_down" for e in self.host.poll()))

                release[1].set()
                self.assertIsNone(await asyncio.wait_for(tasks[1], 2))
                self.assertFalse(self.host._modal_dialog)
                self.host._pending.append(Input("key_down", key="a"))
                self.assertTrue(any(e.kind == "key_down" for e in self.host.poll()))
            finally:
                for event in release:
                    event.set()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_cancelling_the_waiter_releases_the_terminal_prompt(self):
        with patch.object(_files, "dialog_available", return_value=False):
            task = asyncio.create_task(self.host.choose_path())
            try:
                await until(lambda: self.host._prompt is not None)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertIsNone(self.host._prompt)

                task = asyncio.create_task(self.host.choose_path(directory=True))
                await until(lambda: self.host._prompt is not None)
                self.host._prompt_input(Input("key_down", key="Escape"))
                self.assertIsNone(await asyncio.wait_for(task, 2))
                self.assertIsNone(self.host._prompt)
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def test_save_confirms_the_final_extension_without_writing(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            _files, "dialog_available", return_value=False,
        ):
            destination = Path(folder) / "notes.txt"
            destination.write_text("keep this text", encoding="utf-8")
            task = asyncio.create_task(self.host.choose_path(
                save=True, suggested_name="notes", initial_directory=folder,
                extensions=(".txt",),
            ))
            try:
                await until(lambda: self.host._prompt is not None)
                self.host._prompt_input(Input("key_down", key="Enter"))
                await until(lambda: self.host._prompt is not None and self.host._prompt.confirm)
                prompt = self.host._prompt
                assert prompt is not None
                self.assertEqual(prompt.value, str(destination))
                self.assertFalse(task.done())
                self.host._prompt_input(Input("key_down", key="Enter"))
                self.assertEqual(await asyncio.wait_for(task, 2), str(destination))
                self.assertEqual(destination.read_text(encoding="utf-8"), "keep this text")
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
