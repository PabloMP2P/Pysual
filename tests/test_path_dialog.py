"""System open/save dialogs return a path. Cancellation is not a failure."""

import asyncio
import contextlib
import os
import sys
import tempfile
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, patch

from _terminal import MemoryTerminal

from pysual.app import Window
from pysual.backends import _files
from pysual.backends._native import NativeServices
from pysual.backends._web_native import LiveSVGHost
from pysual.backends._web_pyodide import BundledSVGHost
from pysual.backends.native import NativeHost, NativeTermTextHost
from pysual.backends.term_text import TermTextHost
from pysual.errors import LifecycleError
from pysual.host import (
    CapabilityError, Input, PathDialog, TextFile, ensure_allowed_extension,
    prepare_path_dialog,
)
from pysual._engine import call
from pysual.runtime import wait as wait_windows
import pysual.runtime as window_runtime
from test_library import RecordingHost


def find_tools(**available: str) -> Callable[[str], str | None]:
    def find(name: str) -> str | None:
        return available.get(name)
    return find


class DialogPlanTests(unittest.TestCase):
    def test_options_reject_unsafe_names_and_missing_folders(self):
        self.assertEqual(prepare_path_dialog().suggested_name, "")
        self.assertEqual(prepare_path_dialog(save=True, suggested_name="notes.txt").save, True)
        for name in ("../notes.txt", "a/b", "a\\b", "bad:name", "star*.txt", ".", "..", "   ", "trail "):
            with self.subTest(name=name), self.assertRaises(ValueError):
                prepare_path_dialog(suggested_name=name)
        with self.assertRaises(ValueError):
            prepare_path_dialog(save=1)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            prepare_path_dialog(title="two\nlines")
        with self.assertRaises(ValueError):
            prepare_path_dialog(title="x" * 121)
        with self.assertRaises(ValueError):
            prepare_path_dialog(initial_directory="missing-folder-for-dialog")
        with tempfile.TemporaryDirectory() as folder:
            file_path = Path(folder) / "notes.txt"
            file_path.write_text("x", encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare_path_dialog(initial_directory=str(file_path))
            prepared = prepare_path_dialog(initial_directory=folder, title="Open notes")
        self.assertEqual(prepared.initial_directory, os.path.abspath(folder))
        self.assertEqual(prepared.title, "Open notes")

    def test_extensions_are_suffixes_for_file_dialogs(self):
        prepared = prepare_path_dialog(extensions=(".txt", "MD", "*.txt", "*.tar.gz"))
        self.assertEqual(prepared.extensions, (".txt", ".md", ".tar.gz"))
        self.assertEqual(
            prepare_path_dialog(suggested_name="notes.md", extensions=(".txt", ".md")).suggested_name,
            "notes.md",
        )
        self.assertEqual(
            prepare_path_dialog(save=True, suggested_name="notes", extensions=(".txt",)).suggested_name,
            "notes",
        )
        for extensions in ("txt", (".txt",) * 33, ("*",), ("../x",), (".",), ("")):
            with self.subTest(extensions=extensions), self.assertRaises(ValueError):
                prepare_path_dialog(extensions=extensions)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            prepare_path_dialog(directory=True, extensions=(".txt",))
        with self.assertRaises(ValueError):
            prepare_path_dialog(suggested_name="notes.py", extensions=(".txt", ".md"))
        self.assertEqual(
            prepare_path_dialog(
                suggested_name="notes.py", extensions=(".txt", ".md"), all_files=True,
            ).suggested_name,
            "notes.py",
        )
        with self.assertRaises(ValueError):
            prepare_path_dialog(directory=True, all_files=True)
        bare = Path(os.path.abspath("notes"))
        kept = ensure_allowed_extension(
            bare.with_name("notes.py"), save=False, extensions=(".txt", ".md"), all_files=True,
        )
        self.assertEqual(kept.name, "notes.py")
        self.assertEqual(
            ensure_allowed_extension(bare, save=True, extensions=(".txt", ".md")),
            Path(os.path.abspath("notes.txt")),
        )
        self.assertEqual(
            ensure_allowed_extension(bare.with_name("Notes.MD"), save=False, extensions=(".txt", ".md")),
            bare.with_name("Notes.MD"),
        )
        with self.assertRaises(ValueError):
            ensure_allowed_extension(bare.with_name("notes.py"), save=False, extensions=(".txt", ".md"))

    def test_caller_text_is_not_interpolated_into_dialog_scripts(self):
        payload = "notes$(x)"
        request = PathDialog(True, False, payload, "Save $(x)", "/home/me")
        windows = _files.plan_dialog(
            request, platform="win32",
            which=find_tools(**{"powershell.exe": r"C:\Windows\System32\powershell.exe"}),
        )
        script = base64_utf16(windows.argv[-1])
        self.assertNotIn(payload, script)
        self.assertNotIn("$(x)", script)
        self.assertIn("SaveFileDialog", script)
        self.assertIn("FolderBrowserDialog", script)
        self.assertEqual(windows.env["PYSUAL_FILENAME"], payload)
        self.assertEqual(windows.env["PYSUAL_TITLE"], "Save $(x)")
        self.assertEqual(windows.env["PYSUAL_SAVE"], "1")
        self.assertEqual(windows.argv[1:4], ("-STA", "-NoProfile", "-EncodedCommand"))

        apple = _files.plan_dialog(
            PathDialog(False, True, payload, "Save $(x)", "/home/me"),
            platform="darwin", which=find_tools(osascript="/usr/bin/osascript"),
        )
        program = apple.stdin.decode("utf-8") if apple.stdin is not None else ""
        self.assertNotIn(payload, program)
        self.assertNotIn("$(x)", program)
        self.assertIn("setCanChooseDirectories", program)
        self.assertEqual(apple.env["PYSUAL_DIRECTORY"], "1")
        self.assertEqual(apple.argv, ("/usr/bin/osascript", "-s", "he", "-"))

        zenity = _files.plan_dialog(
            request, platform="linux", which=find_tools(zenity="/usr/bin/zenity", kdialog="/usr/bin/kdialog"),
            home="/home/me",
        )
        self.assertEqual(zenity.tool, "zenity")
        self.assertEqual(zenity.argv[0], "/usr/bin/zenity")
        self.assertIn("--save", zenity.argv)
        self.assertIn("--confirm-overwrite", zenity.argv)
        self.assertIn("--filename=/home/me/" + payload, zenity.argv)
        self.assertIn("--title=Save $(x)", zenity.argv)

        folder = _files.plan_dialog(
            PathDialog(False, True, "", "", "/home/me"),
            platform="linux", which=find_tools(zenity="/usr/bin/zenity"), home="/home/me",
        )
        self.assertIn("--directory", folder.argv)
        self.assertNotIn("--save", folder.argv)
        self.assertIn("--filename=/home/me/", folder.argv)

        kdialog = _files.plan_dialog(
            request, platform="linux", which=find_tools(kdialog="/usr/bin/kdialog"), home="/home/me",
        )
        self.assertEqual(kdialog.tool, "kdialog")
        self.assertIn("--getsavefilename", kdialog.argv)
        self.assertIn("/home/me/" + payload, kdialog.argv)

        limited = PathDialog(False, False, "notes.md", "", "/home/me", (".txt", ".md"))
        zenity_filter = _files.plan_dialog(
            limited, platform="linux", which=find_tools(zenity="/usr/bin/zenity"), home="/home/me",
        )
        self.assertIn("--file-filter=Allowed files | *.[tT][xX][tT] *.[mM][dD]", zenity_filter.argv)
        self.assertEqual(zenity_filter.env["PYSUAL_EXTENSIONS"], ".txt;.md")
        kdialog_filter = _files.plan_dialog(
            limited, platform="linux", which=find_tools(kdialog="/usr/bin/kdialog"), home="/home/me",
        )
        self.assertIn("Allowed files (*.txt *.md)", kdialog_filter.argv)
        windows_filter = _files.plan_dialog(
            limited, platform="win32",
            which=find_tools(**{"powershell.exe": "powershell.exe"}),
        )
        self.assertEqual(windows_filter.env["PYSUAL_EXTENSIONS"], ".txt;.md")
        self.assertNotIn(".txt", base64_utf16(windows_filter.argv[-1]))
        apple_filter = _files.plan_dialog(
            limited, platform="darwin", which=find_tools(osascript="/usr/bin/osascript"),
        )
        apple_program = apple_filter.stdin.decode("utf-8") if apple_filter.stdin else ""
        self.assertIn("setAllowedFileTypes", apple_program)
        self.assertNotIn(".txt", apple_program)
        self.assertEqual(apple_filter.env["PYSUAL_EXTENSIONS"], ".txt;.md")
        self.assertEqual(apple_filter.env["PYSUAL_ALL_FILES"], "0")
        either = PathDialog(False, False, "", "", None, (".txt", ".md"), True)
        zenity_any = _files.plan_dialog(
            either, platform="linux", which=find_tools(zenity="/usr/bin/zenity"), home="/home/me",
        )
        self.assertIn("--file-filter=Allowed files | *.[tT][xX][tT] *.[mM][dD]", zenity_any.argv)
        self.assertIn("--file-filter=All files | *", zenity_any.argv)
        self.assertEqual(zenity_any.env["PYSUAL_ALL_FILES"], "1")
        kdialog_any = _files.plan_dialog(
            either, platform="linux", which=find_tools(kdialog="/usr/bin/kdialog"), home="/home/me",
        )
        self.assertIn("Allowed files (*.txt *.md);;All files (*)", kdialog_any.argv)

    def test_availability_follows_the_desktop_session(self):
        zenity = find_tools(zenity="/usr/bin/zenity")
        osascript = find_tools(osascript="/usr/bin/osascript")
        powershell = find_tools(**{"powershell.exe": "powershell.exe"})
        self.assertTrue(_files.dialog_available(platform="linux", which=zenity, environ={"DISPLAY": ":0"}))
        self.assertFalse(_files.dialog_available(platform="linux", which=zenity, environ={}))
        self.assertIn("graphical", _files.unavailable_reason(platform="linux", which=zenity, environ={}))
        self.assertIn(
            "zenity",
            _files.unavailable_reason(platform="linux", which=find_tools(), environ={"WAYLAND_DISPLAY": "wayland-0"}),
        )
        self.assertTrue(_files.dialog_available(platform="darwin", which=osascript, environ={}))
        self.assertFalse(_files.dialog_available(
            platform="darwin", which=osascript, environ={"SSH_CONNECTION": "1"},
        ))
        self.assertIn("macOS", _files.unavailable_reason(
            platform="darwin", which=osascript, environ={"SSH_TTY": "/dev/ttys0"},
        ))
        self.assertTrue(_files.dialog_available(platform="win32", which=powershell, environ={}))
        self.assertFalse(_files.dialog_available(platform="win32", which=find_tools(), environ={}))

    def test_results_distinguish_cancellation_failure_and_a_path(self):
        chosen = os.path.abspath("notes.txt")
        encoded = chosen.encode("utf-8")
        self.assertEqual(_files.interpret_dialog("powershell", 0, encoded + b"\r\n", b""), Path(chosen))
        self.assertIsNone(_files.interpret_dialog("powershell", 0, b"", b""))
        self.assertIsNone(_files.interpret_dialog("powershell", 0, b"\xef\xbb\xbf", b""))
        with self.assertRaisesRegex(CapabilityError, "Add-Type failed"):
            _files.interpret_dialog("powershell", 1, b"", b"Add-Type failed")
        self.assertIsNone(_files.interpret_dialog("zenity", 10, b"", b"Gtk-WARNING mapped without a parent"))
        self.assertIsNone(_files.interpret_dialog("kdialog", 1, b"", b""))
        self.assertEqual(
            _files.interpret_dialog("zenity", 0, b"\xef\xbb\xbf" + encoded + b"\n", b"warning"),
            Path(chosen),
        )
        with self.assertRaises(CapabilityError):
            _files.interpret_dialog("zenity", 255, b"", b"cannot open display")
        self.assertIsNone(_files.interpret_dialog(
            "osascript", 1, b"", b"execution error: User canceled. (-128)",
        ))
        with self.assertRaises(CapabilityError):
            _files.interpret_dialog("osascript", 1, b"", b"syntax error")
        with self.assertRaises(CapabilityError):
            _files.interpret_dialog("zenity", 0, b"\xff", b"")
        if sys.platform == "win32":
            with self.assertRaises(CapabilityError):
                _files.interpret_dialog("zenity", 0, (chosen + "\nextra").encode(), b"")
        else:
            self.assertEqual(
                _files.interpret_dialog("zenity", 0, (chosen + "\nextra\n").encode(), b""),
                Path(chosen + "\nextra"),
            )
        with self.assertRaises(CapabilityError):
            _files.interpret_dialog("zenity", 0, b"x" * 40000, b"")
        self.assertEqual(_files.chosen_path("notes.txt"), Path(chosen))
        if sys.platform == "win32":
            self.assertEqual(
                _files.chosen_path("file:///C:/Users/pablo/notes.txt"),
                Path("C:/Users/pablo/notes.txt"),
            )
        else:
            self.assertEqual(_files.chosen_path("file:///tmp/notes.txt"), Path("/tmp/notes.txt"))
        with self.assertRaises(CapabilityError):
            _files.chosen_path("bad\0path")

    def test_typed_paths_accept_files_and_folders_without_writing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            notes = root / "notes.txt"
            notes.write_text("old", encoding="utf-8")
            self.assertIsNone(_files.classify_typed_path(root, save=False, directory=True))
            self.assertIsNone(_files.classify_typed_path(root, save=True, directory=True))
            self.assertEqual(
                _files.classify_typed_path(notes, save=False, directory=True),
                "Choose an existing folder",
            )
            self.assertIsNone(_files.classify_typed_path(notes, save=False, directory=False))
            self.assertEqual(
                _files.classify_typed_path(root, save=False, directory=False), "Choose a file",
            )
            self.assertEqual(_files.classify_typed_path(notes, save=True, directory=False), "confirm")
            self.assertIsNone(_files.classify_typed_path(root / "new.txt", save=True, directory=False))
            self.assertEqual(
                _files.classify_typed_path(root / "missing.txt", save=False, directory=False),
                "Choose an existing file",
            )
            self.assertEqual(
                _files.classify_typed_path(root / "missing" / "new.txt", save=True, directory=False),
                "The destination folder does not exist",
            )
            self.assertEqual(notes.read_text(encoding="utf-8"), "old")


def base64_utf16(encoded: str) -> str:
    import base64
    return base64.b64decode(encoded).decode("utf-16le")


class DialogProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_dialogs_do_not_overlap_and_a_kill_is_cancellation(self):
        current = 0
        peak = 0

        async def execute(launch, active) -> tuple[int, bytes, bytes, bool]:
            nonlocal current, peak
            del launch
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.05)
            current -= 1
            return 0, (os.path.abspath("chosen.txt") + "\n").encode(), b"", False

        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=sleep_launch()),
            patch.object(_files, "_execute", execute),
        ):
            first, second = await asyncio.gather(_files.choose_path(), _files.choose_path())
        self.assertEqual(peak, 1)
        self.assertEqual(first, second)
        self.assertEqual(first, Path(os.path.abspath("chosen.txt")))

        async def killed(launch, active) -> tuple[int, bytes, bytes, bool]:
            del launch
            return 0, b"C:\\secret.txt\n", b"failed", True

        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=sleep_launch()),
            patch.object(_files, "_execute", killed),
        ):
            self.assertIsNone(await _files.choose_path())

    async def test_a_closed_host_does_not_start_the_process(self):
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=sleep_launch()),
        ):
            result = await asyncio.wait_for(_files.choose_path(accept=lambda: False), 2)
        self.assertIsNone(result)
        self.assertFalse(_files._active_dialogs)

    async def test_cancellation_kills_only_the_owning_dialog(self):
        owner = object()
        other = object()
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=sleep_launch()),
        ):
            task = asyncio.create_task(_files.choose_path(owner=owner))
            try:
                await wait_for_process()
                _files.cancel_dialog(other)
                await asyncio.sleep(0.05)
                self.assertFalse(task.done())
                self.assertTrue(any(active.process is not None for active in _files._active_dialogs))
                _files.cancel_dialog(owner)
                self.assertIsNone(await asyncio.wait_for(task, 5))
            finally:
                _files.cancel_dialog()
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def test_cancelling_the_waiter_stops_the_process(self):
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=sleep_launch()),
        ):
            task = asyncio.create_task(_files.choose_path())
            try:
                await wait_for_process()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            finally:
                _files.cancel_dialog()
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        self.assertFalse(_files._active_dialogs)

    async def test_text_services_still_use_the_file_dialog(self):
        host = NativeServices()
        with patch("pysual.backends._files.choose_path", new=AsyncMock(return_value=None)) as dialog:
            self.assertIsNone(await host.open_text_file())
        dialog.assert_awaited_once_with(
            owner=host, owner_process_id=None, accept=host._accept_path_dialog,
        )
        with patch("pysual.backends._files.choose_path", new=AsyncMock(return_value=None)) as dialog:
            self.assertIsNone(await host.save_text_file("hi", "a.txt", None))
        dialog.assert_awaited_once_with(
            True, "a.txt", owner=host, owner_process_id=None, accept=host._accept_path_dialog,
        )
        with (
            patch("pysual.backends._files.choose_path", new=AsyncMock()) as dialog,
            patch(
                "pysual.backends._files.write_text",
                new=AsyncMock(return_value=TextFile("a.txt", "hi", "L")),
            ),
        ):
            saved = await host.save_text_file("hi", "a.txt", "L")
        dialog.assert_not_awaited()
        self.assertEqual(saved, TextFile("a.txt", "hi", "L"))
        sample = Path(os.path.abspath("notes.txt"))
        with patch("pysual.backends._files.choose_path", new=AsyncMock(return_value=sample)):
            chosen = await host.choose_path(
                save=True, directory=True, suggested_name="notes.txt", title="Save",
            )
        self.assertEqual(chosen, str(sample))
        self.assertIn("path_dialogs", NativeHost.capabilities)
        self.assertIn("path_dialogs", NativeTermTextHost.capabilities)
        self.assertIn("path_dialogs", LiveSVGHost.capabilities)
        self.assertNotIn("path_dialogs", BundledSVGHost.capabilities)
        with self.assertRaisesRegex(CapabilityError, "filesystem path"):
            await BundledSVGHost.choose_path(None)  # type: ignore[arg-type]


def sleep_launch() -> _files.DialogLaunch:
    return _files.DialogLaunch(
        (sys.executable, "-c", "import time; time.sleep(30)"), {}, None, "zenity",
    )


async def wait_for_process() -> None:
    for _ in range(250):
        if any(active.process is not None for active in _files._active_dialogs):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("dialog process did not start")


class TerminalPathTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = TermTextHost(io=MemoryTerminal(80, 24), probe_timeout=0, environ={})
        self.host.open("paths", 640, 384, False, 1)

    def tearDown(self):
        self.host.close()

    async def test_system_dialog_is_modal_and_returns_its_path(self):
        seen: dict[str, object] = {}

        async def fake(*args: object, **kwargs: object) -> Path:
            seen["modal"] = self.host._modal_dialog
            seen["args"] = args
            seen["initial"] = kwargs.get("initial_directory")
            return Path(os.path.abspath("picked.txt"))

        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "choose_path", fake),
        ):
            chosen = await self.host.choose_path(save=True, suggested_name="picked.txt")
        self.assertEqual(chosen, os.path.abspath("picked.txt"))
        self.assertEqual(seen["modal"], True)
        self.assertEqual(seen["args"], (True, "picked.txt"))
        self.assertFalse(self.host._modal_dialog)
        self.host._modal_dialog = True
        self.host._pending.append(Input("key_down", key="a"))
        self.assertFalse(any(event.kind == "key_down" for event in self.host.poll()))
        self.host._modal_dialog = False
        self.host._pending.append(Input("key_down", key="a"))
        self.assertTrue(any(event.kind == "key_down" for event in self.host.poll()))

    async def test_prompt_returns_a_typed_path_and_escape_cancels(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            _files, "dialog_available", return_value=False,
        ):
            notes = Path(folder) / "notes.txt"
            notes.write_text("old", encoding="utf-8")
            saved = asyncio.create_task(self.host.choose_path(
                save=True, suggested_name="notes.txt", initial_directory=folder,
            ))
            await self.wait_prompt()
            self.assertEqual(self.host._prompt.title, "Save file")  # type: ignore[union-attr]
            self.host._prompt_input(Input("key_down", key="Enter"))
            await self.wait_until(lambda: self.host._prompt is not None and self.host._prompt.confirm)
            self.host._prompt_input(Input("key_down", key="Enter"))
            self.assertEqual(await asyncio.wait_for(saved, 2), os.path.abspath(notes))
            self.assertEqual(notes.read_text(encoding="utf-8"), "old")

            opened = asyncio.create_task(self.host.choose_path(directory=True, initial_directory=folder))
            await self.wait_prompt()
            self.assertEqual(self.host._prompt.title, "Choose a folder")  # type: ignore[union-attr]
            self.host._prompt_input(Input("key_down", key="Enter"))
            self.assertEqual(await asyncio.wait_for(opened, 2), os.path.abspath(folder))

            missing = asyncio.create_task(self.host.choose_path())
            await self.wait_prompt()
            prompt = self.host._prompt
            assert prompt is not None
            prompt.value = str(Path(folder) / "missing.txt")
            prompt.cursor = len(prompt.value)
            self.host._prompt_input(Input("key_down", key="Enter"))
            await self.wait_until(lambda: bool(prompt.error))
            self.assertIn("existing", prompt.error.lower())
            self.host._prompt_input(Input("key_down", key="Escape"))
            self.assertIsNone(await asyncio.wait_for(missing, 2))

    async def test_returned_names_must_use_a_requested_extension(self):
        async def chosen(launch, active) -> tuple[int, bytes, bytes, bool]:
            del launch
            return 0, (os.path.abspath("notes.py") + "\n").encode(), b"", False

        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=sleep_launch()),
            patch.object(_files, "_execute", chosen),
        ):
            with self.assertRaises(ValueError):
                await _files.choose_path(False, "", extensions=(".txt", ".md"))

        returned = iter(("notes", "notes.txt"))

        async def bare(launch, active) -> tuple[int, bytes, bytes, bool]:
            del launch
            return 0, (os.path.abspath(next(returned)) + "\n").encode(), b"", False

        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=sleep_launch()),
            patch.object(_files, "_execute", bare),
        ):
            saved = await _files.choose_path(True, "notes", extensions=(".txt", ".md"))
        self.assertEqual(saved, Path(os.path.abspath("notes.txt")))
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=sleep_launch()),
            patch.object(_files, "_execute", chosen),
        ):
            opened = await _files.choose_path(
                False, "", extensions=(".txt", ".md"), all_files=True,
            )
        self.assertEqual(opened, Path(os.path.abspath("notes.py")))

    async def test_prompt_accepts_only_the_requested_extensions(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(
            _files, "dialog_available", return_value=False,
        ):
            notes = Path(folder) / "notes.md"
            notes.write_text("hi", encoding="utf-8")
            task = asyncio.create_task(self.host.choose_path(
                extensions=(".txt", ".md"), initial_directory=folder,
            ))
            await self.wait_prompt()
            prompt = self.host._prompt
            assert prompt is not None
            prompt.value = str(Path(folder) / "notes.py")
            prompt.cursor = len(prompt.value)
            self.host._prompt_input(Input("key_down", key="Enter"))
            await self.wait_until(lambda: ".txt" in prompt.error)
            prompt.value = str(notes)
            prompt.cursor = len(prompt.value)
            self.host._prompt_input(Input("key_down", key="Enter"))
            self.assertEqual(await asyncio.wait_for(task, 2), os.path.abspath(notes))

    async def test_prompt_requires_an_open_host(self):
        closed = TermTextHost(io=MemoryTerminal(20, 6), probe_timeout=0, environ={})
        with patch.object(_files, "dialog_available", return_value=False):
            with self.assertRaises(CapabilityError):
                await closed.choose_path()

    async def wait_prompt(self) -> None:
        await self.wait_until(lambda: self.host._prompt is not None)

    async def wait_until(self, predicate) -> None:
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0.02)
        self.fail("terminal path prompt did not update")


class PathDialogAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.windows = []
        self.assertFalse(call(lambda: bool(window_runtime._active)))
        try:
            wait_windows(0)
        except BaseException:
            pass

    def make(self) -> Window:
        window = Window()
        self.windows.append(window)
        return window

    async def asyncTearDown(self):
        for window in reversed(self.windows):
            try:
                await window.destroy_async()
            except BaseException:
                pass

    async def test_choose_path_uses_the_host_and_one_operation_at_a_time(self):
        window = self.make()
        with self.assertRaises(ValueError):
            await window.choose_path(suggested_name="../notes.txt")
        with self.assertRaises(LifecycleError):
            await window.choose_path()
        host = PathHost()
        window.run(backend=host)
        plain = self.make()
        plain.run(backend=RecordingHost())
        with self.assertRaises(CapabilityError):
            await plain.choose_path(directory=True)
        await plain.close_async()
        await plain.wait_async()
        with self.assertRaises(ValueError):
            await plain.choose_path(title="bad\nname")
        with self.assertRaises(LifecycleError):
            await plain.choose_path()

        task = asyncio.create_task(window.choose_path(
            save=True, directory=True, suggested_name="notes.txt", title="Save notes",
        ))
        try:
            self.assertTrue(await asyncio.to_thread(host.started.wait, 2))
            with self.assertRaises(LifecycleError):
                await window.choose_path()
            with self.assertRaises(LifecycleError):
                await window.open_text_file()
        finally:
            host.release.set()
        self.assertEqual(await task, host.result)
        self.assertEqual(host.kwargs["save"], True)
        self.assertEqual(host.kwargs["directory"], True)
        self.assertEqual(host.kwargs["suggested_name"], "notes.txt")
        self.assertEqual(host.kwargs["title"], "Save notes")
        host.release.clear()
        host.block = False
        self.assertIsNone(await window.choose_path())

    async def test_initial_directory_is_absolute_before_the_host_sees_it(self):
        window = self.make()
        host = PathHost()
        host.block = False
        window.run(backend=host)
        with tempfile.TemporaryDirectory() as folder:
            chosen = await window.choose_path(initial_directory=folder)
        self.assertIsNone(chosen)
        self.assertEqual(host.kwargs["initial_directory"], os.path.abspath(folder))


class PathHost(RecordingHost):
    capabilities = RecordingHost.capabilities | {"path_dialogs", "text_files"}

    def __init__(self):
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = True
        self.kwargs = {}
        self.result = os.path.abspath("chosen.txt")

    async def choose_path(self, **kwargs):
        self.kwargs = kwargs
        if not self.block:
            return None
        self.started.set()
        if not await asyncio.to_thread(self.release.wait, 2):
            raise TimeoutError("dialog test was not released")
        return self.result
