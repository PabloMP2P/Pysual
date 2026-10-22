"""Dialog cancellation retains ownership while a process starts or completes."""

import asyncio
import base64
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pysual.backends import _files
from pysual.host import CapabilityError, PathDialog, prepare_path_dialog


class DialogProcess:
    """A process whose communication and exit can be controlled independently."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.returncode: int | None = None
        self.ready = threading.Event()
        self.finished = asyncio.Event()
        self.killed = False
        self.reaped = False

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
        self.ready.set()
        await self.finished.wait()
        self.reaped = True
        return os.path.abspath("chosen.txt").encode("utf-8"), b""

    async def wait(self) -> int:
        await self.finished.wait()
        self.reaped = True
        assert self.returncode is not None
        return self.returncode

    def finish(self) -> None:
        if self.returncode is None:
            self.returncode = 0
        if not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.finished.set)

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.finish()


def plan(request: PathDialog) -> _files.DialogLaunch:
    return _files.DialogLaunch(("test-dialog",), {"DIALOG_TITLE": request.title}, None, "powershell")


class DialogCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_close_cancels_a_queued_dialog_even_if_the_host_reopens(self) -> None:
        owner = object()
        accepted = True
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", side_effect=plan),
            patch.object(_files, "_spawn") as spawn,
        ):
            async with _files._dialog_lock():
                task = asyncio.create_task(_files.choose_path(
                    owner=owner, accept=lambda: accepted,
                ))
                await asyncio.sleep(0)
                accepted = False
                _files.cancel_dialog(owner)
                accepted = True
            try:
                self.assertIsNone(await asyncio.wait_for(task, 2))
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        spawn.assert_not_awaited()

    async def test_cancelling_a_queued_task_releases_its_registry_entry(self) -> None:
        previous = set(_files._active_dialogs)
        with patch.object(_files, "dialog_available", return_value=True):
            async with _files._dialog_lock():
                task = asyncio.create_task(_files.choose_path(owner=object()))
                await asyncio.sleep(0)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
        self.assertEqual(_files._active_dialogs, previous)

    async def test_communication_failure_stops_and_reaps_the_process(self) -> None:
        process = DialogProcess()
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", side_effect=plan),
            patch.object(_files, "_spawn", return_value=process),
            patch.object(process, "communicate", side_effect=OSError("pipe failed")),
        ):
            with self.assertRaisesRegex(OSError, "pipe failed"):
                await asyncio.wait_for(_files.choose_path(), 2)
        self.assertTrue(process.killed)
        self.assertTrue(process.reaped)

    async def test_cancellation_during_spawn_reaps_the_created_process(self) -> None:
        process = DialogProcess()
        started = asyncio.Event()
        release = asyncio.Event()

        async def spawn(*args: object) -> DialogProcess:
            started.set()
            await release.wait()
            return process

        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", side_effect=plan),
            patch.object(_files, "_spawn", side_effect=spawn),
        ):
            task = asyncio.create_task(_files.choose_path())
            try:
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertTrue(process.killed)
                self.assertTrue(process.reaped)
            finally:
                release.set()
                process.finish()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_closing_the_host_discards_an_already_completed_result(self) -> None:
        process = DialogProcess()
        accepted = True

        async def communicate(data: bytes | None = None) -> tuple[bytes, bytes]:
            nonlocal accepted
            accepted = False
            process.returncode = 0
            return os.path.abspath("chosen.txt").encode("utf-8"), b""

        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", side_effect=plan),
            patch.object(_files, "_spawn", return_value=process),
            patch.object(process, "communicate", side_effect=communicate),
        ):
            self.assertIsNone(await _files.choose_path(accept=lambda: accepted))

    async def test_owner_cancellation_discards_a_process_that_has_already_exited(self) -> None:
        process = DialogProcess()
        owner = object()

        async def communicate(data: bytes | None = None) -> tuple[bytes, bytes]:
            process.returncode = 0
            _files.cancel_dialog(owner)
            return os.path.abspath("chosen.txt").encode("utf-8"), b""

        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", side_effect=plan),
            patch.object(_files, "_spawn", return_value=process),
            patch.object(process, "communicate", side_effect=communicate),
        ):
            self.assertIsNone(await _files.choose_path(owner=owner))
        self.assertFalse(process.killed)

    async def test_separate_event_loops_cancel_only_their_own_process(self) -> None:
        owners = [object(), object()]
        processes: dict[str, DialogProcess] = {}
        started = [threading.Event(), threading.Event()]

        async def spawn(argv: tuple[str, ...], env: dict[str, str]) -> DialogProcess:
            name = env["DIALOG_TITLE"]
            process = DialogProcess()
            processes[name] = process
            started[int(name)].set()
            return process

        def run(index: int) -> Path | None:
            return asyncio.run(_files.choose_path(owner=owners[index], title=str(index)))

        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", side_effect=plan),
            patch.object(_files, "_spawn", side_effect=spawn),
        ):
            tasks = [asyncio.create_task(asyncio.to_thread(run, index)) for index in range(2)]
            try:
                for event in started:
                    self.assertTrue(await asyncio.to_thread(event.wait, 2))
                for process in processes.values():
                    self.assertTrue(await asyncio.to_thread(process.ready.wait, 2))
                _files.cancel_dialog(owners[0])
                self.assertIsNone(await asyncio.wait_for(asyncio.shield(tasks[0]), 2))
                self.assertTrue(processes["0"].killed)
                self.assertFalse(processes["1"].killed)
                self.assertFalse(tasks[1].done())
                processes["1"].finish()
                self.assertEqual(
                    await asyncio.wait_for(asyncio.shield(tasks[1]), 2),
                    Path(os.path.abspath("chosen.txt")),
                )
            finally:
                for process in processes.values():
                    process.finish()
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)


class DialogSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_added_suffix_is_accepted_in_the_native_dialog_before_returning(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            selected = Path(folder) / "notes"
            destination = selected.with_suffix(".txt")
            destination.write_text("keep existing contents", encoding="utf-8")
            with (
                patch.object(_files, "dialog_available", return_value=True),
                patch.object(_files, "plan_dialog", side_effect=plan) as planner,
                patch.object(_files, "_execute", side_effect=[
                    (0, str(selected).encode("utf-8"), b"", False),
                    (0, str(destination).encode("utf-8"), b"", False),
                ]) as execute,
            ):
                result = await _files.choose_path(True, "notes", extensions=(".txt",))
            self.assertEqual(result, destination)
            self.assertEqual(execute.await_count, 2)
            followup = planner.call_args_list[1].args[0]
            self.assertEqual(followup.suggested_name, "notes.txt")
            self.assertEqual(followup.initial_directory, folder)
            self.assertTrue(followup.save)
            self.assertEqual(destination.read_text(encoding="utf-8"), "keep existing contents")

    async def test_cancelling_the_suffixed_save_does_not_return_the_unconfirmed_path(self) -> None:
        selected = Path(os.path.abspath("notes"))
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", side_effect=plan),
            patch.object(_files, "_execute", side_effect=[
                (0, str(selected).encode("utf-8"), b"", False),
                (0, b"", b"", False),
            ]) as execute,
        ):
            result = await _files.choose_path(True, "notes", extensions=(".txt",))
        self.assertIsNone(result)
        self.assertEqual(execute.await_count, 2)


class DialogPlatformTests(unittest.TestCase):
    def test_mac_all_files_does_not_leave_the_open_panel_restricted(self) -> None:
        launch = _files.plan_dialog(
            prepare_path_dialog(extensions=(".txt",), all_files=True),
            platform="darwin", which=lambda tool: "/usr/bin/osascript",
        )
        assert launch.stdin is not None
        script = launch.stdin.decode("utf-8")
        self.assertIn("and allowAny is false then", script)
        self.assertIn("panel's setAllowedFileTypes:typeList", script)
        self.assertEqual(launch.env["PYSUAL_ALL_FILES"], "1")

    def test_kdialog_option_terminator_precedes_a_filename_starting_with_dash(self) -> None:
        request = prepare_path_dialog(save=True, suggested_name="--help")
        launch = _files.plan_dialog(
            request, platform="linux", home="/home/user",
            which=lambda tool: "/usr/bin/kdialog" if tool == "kdialog" else None,
        )
        self.assertEqual(
            launch.argv,
            ("/usr/bin/kdialog", "--getsavefilename", "--", "/home/user/--help"),
        )

    def test_kdialog_filters_use_qt_syntax_and_include_extensionless_files(self) -> None:
        launch = _files.plan_dialog(
            prepare_path_dialog(extensions=(".txt", ".md"), all_files=True),
            platform="linux", home="/home/user",
            which=lambda tool: "/usr/bin/kdialog" if tool == "kdialog" else None,
        )
        self.assertEqual(launch.argv[-1], "Allowed files (*.txt *.md);;All files (*)")

    def test_windows_dialog_supports_compound_extensions(self) -> None:
        launch = _files.plan_dialog(
            prepare_path_dialog(save=True, suggested_name="archive", extensions=(".tar.gz",)),
            platform="win32", which=lambda tool: "powershell.exe",
        )
        script = base64.b64decode(launch.argv[-1]).decode("utf-16le")
        self.assertIn("$dialog.SupportMultiDottedExtensions = $true", script)
        self.assertEqual(launch.env["PYSUAL_EXTENSIONS"], ".tar.gz")


class DialogOutputTests(unittest.TestCase):
    def test_result_preserves_spaces_and_unicode(self) -> None:
        chosen = Path(os.path.abspath("  ni\N{LATIN SMALL LETTER N WITH TILDE}o.txt"))
        self.assertEqual(
            _files.interpret_dialog("zenity", 0, str(chosen).encode("utf-8") + b"\n", b""),
            chosen,
        )

    def test_unusable_and_remote_paths_are_rejected(self) -> None:
        for output in (
            b"file://other-host/share/file.txt\n",
            b"smb://server/share\n",
            b"https://example.com/file.txt\n",
            b"file:///tmp/file.txt?query\n",
            b"file:///tmp/file.txt#fragment\n",
            b"file:///tmp/file%ff.txt\n",
            b"bad\x00name\n",
            b"one\ntwo\n",
        ):
            with self.subTest(output=output), self.assertRaises(CapabilityError):
                _files.interpret_dialog("zenity", 0, output, b"")
