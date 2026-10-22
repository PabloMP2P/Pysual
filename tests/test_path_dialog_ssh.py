"""Windows SSH sessions keep path selection inside the terminal."""

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from _terminal import MemoryTerminal, until
from pysual.backends import _files
from pysual.backends.term_text import TermTextHost
from pysual.host import Input


class WindowsSSHDialogTests(unittest.TestCase):
    def test_powershell_does_not_make_an_ssh_session_graphical(self):
        for variable in ("SSH_CONNECTION", "SSH_TTY"):
            with self.subTest(variable=variable):
                options = {
                    "platform": "win32",
                    "which": lambda name: "powershell.exe",
                    "environ": {variable: "remote-session"},
                }
                self.assertFalse(_files.dialog_available(**options))
                self.assertIn("Windows desktop session", _files.unavailable_reason(**options))
        self.assertTrue(_files.dialog_available(
            platform="win32", which=lambda name: "powershell.exe", environ={},
        ))


class WindowsSSHTerminalTests(unittest.IsolatedAsyncioTestCase):
    async def test_ssh_uses_a_working_terminal_prompt_instead_of_a_native_dialog(self):
        host = TermTextHost(io=MemoryTerminal(80, 24), probe_timeout=0)
        host.open("SSH paths", 640, 384, False, 1)
        task = None
        try:
            with (
                tempfile.TemporaryDirectory() as folder,
                patch.object(_files.sys, "platform", "win32"),
                patch.object(_files, "_environment", return_value={"SSH_CONNECTION": "remote"}),
                patch.object(_files, "_finder", return_value=lambda name: "powershell.exe"),
                patch.object(host, "_system_path", new=AsyncMock(
                    side_effect=AssertionError("A native dialog cannot serve an SSH session"),
                )) as system_path,
            ):
                selected = Path(folder) / "notes.txt"
                selected.write_text("unchanged", encoding="utf-8")
                task = asyncio.create_task(host.choose_path(
                    suggested_name=selected.name, initial_directory=folder,
                ))
                await until(lambda: host._prompt is not None or task.done())
                self.assertIsNotNone(host._prompt)
                host._prompt_input(Input("key_down", key="Enter"))
                self.assertEqual(await asyncio.wait_for(task, 2), str(selected))
                self.assertEqual(selected.read_text(encoding="utf-8"), "unchanged")
                system_path.assert_not_awaited()

                task = asyncio.create_task(host.choose_path(directory=True))
                await until(lambda: host._prompt is not None or task.done())
                self.assertIsNotNone(host._prompt)
                host._prompt_input(Input("key_down", key="Escape"))
                self.assertIsNone(await asyncio.wait_for(task, 2))
                self.assertIsNone(host._prompt)
        finally:
            if task is not None:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, AssertionError):
                    await task
            host.close()
