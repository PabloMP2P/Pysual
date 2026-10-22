"""Windows choosers use the native host's window instead of a topmost proxy."""

import asyncio
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from pysual.backends import _files
from pysual.backends import _windows_dialog
from pysual.backends._native import NativeServices
from pysual.backends.native import NativeHost, NativeTermTextHost
from pysual.host import CapabilityError, prepare_path_dialog


class OwnedServices(NativeServices):
    def _path_dialog_owner_pid(self):
        return 4321


class WindowsDialogOwnershipTests(unittest.TestCase):
    def test_only_a_live_visible_windows_host_supplies_a_process(self):
        native = NativeHost(hidden=False)
        native._client = SimpleNamespace(diagnostics={"pid": 4321})
        terminal = NativeTermTextHost(hidden=False)
        terminal._client = SimpleNamespace(diagnostics={"pid": 4321})
        with patch("pysual.backends.native.sys.platform", "win32"):
            self.assertEqual(native._path_dialog_owner_pid(), 4321)
            self.assertIsNone(terminal._path_dialog_owner_pid())
            self.assertIsNone(NativeServices()._path_dialog_owner_pid())
            native.hidden = True
            self.assertIsNone(native._path_dialog_owner_pid())
            native.hidden = False
            native._closed = True
            self.assertIsNone(native._path_dialog_owner_pid())
            native._closed = False
            for invalid in (None, "4321", 0, -1, True):
                native._client.diagnostics["pid"] = invalid
                self.assertIsNone(native._path_dialog_owner_pid())
        native._client.diagnostics["pid"] = 4321
        with patch("pysual.backends.native.sys.platform", "linux"):
            self.assertIsNone(native._path_dialog_owner_pid())

    def test_windows_script_uses_the_existing_window_without_a_topmost_proxy(self):
        launch = _files.plan_dialog(
            prepare_path_dialog(), platform="win32", which=lambda _: "powershell.exe",
        )
        script = base64.b64decode(launch.argv[-1]).decode("utf-16le")
        self.assertEqual(launch.env["PYSUAL_OWNER_PID"], "")
        self.assertEqual(launch.env["PYSUAL_OWNER_HWND"], "")
        self.assertIn("PYSUAL_OWNER_HWND", script)
        self.assertIn("GetWindowThreadProcessId", script)
        self.assertIn("IWin32Window", script)
        self.assertIn("$dialog.ShowDialog($owner)", script)
        self.assertNotIn("TopMost", script)
        self.assertNotIn("New-Object System.Windows.Forms.Form", script)
        self.assertNotIn("AssignHandle", script)

    def test_windows_ssh_sessions_do_not_offer_an_invisible_system_picker(self):
        for environment in ({"SSH_CONNECTION": "remote"}, {"SSH_TTY": "tty"}):
            with self.subTest(environment=environment):
                self.assertFalse(_files.dialog_available(
                    platform="win32", which=lambda _: "powershell.exe", environ=environment,
                ))
                self.assertIn("local Windows desktop", _files.unavailable_reason(
                    platform="win32", which=lambda _: "powershell.exe", environ=environment,
                ))


class OwnerWindowTests(unittest.TestCase):
    def test_capture_records_the_exact_handle_and_initial_enabled_state(self):
        api = Mock(find_owner=Mock(return_value=101), process_id=Mock(return_value=4321),
                   enabled=Mock(return_value=True))
        with patch.object(_windows_dialog, "_User32", return_value=api):
            owner = _windows_dialog.capture_owner(4321)
        self.assertEqual((owner.handle, owner.process_id, owner.was_enabled), (101, 4321, True))
        api.find_owner.assert_called_once_with(4321)

    def test_capture_rejects_a_closed_or_replaced_owner(self):
        for handle, process_id in ((0, 0), (101, 0), (101, 9999)):
            with self.subTest(handle=handle, process_id=process_id):
                api = Mock(find_owner=Mock(return_value=handle), process_id=Mock(return_value=process_id))
                with patch.object(_windows_dialog, "_User32", return_value=api):
                    with self.assertRaises(CapabilityError):
                        _windows_dialog.capture_owner(4321)
                api.enabled.assert_not_called()

    def test_restore_reenables_only_our_previously_enabled_owner(self):
        for was_enabled, current_pid, enabled, restore in (
            (True, 4321, False, True),
            (True, 4321, True, False),
            (False, 4321, False, False),
            (True, 0, False, False),
            (True, 9999, False, False),
        ):
            with self.subTest(was_enabled=was_enabled, current_pid=current_pid, enabled=enabled):
                api = Mock(process_id=Mock(return_value=current_pid), enabled=Mock(return_value=enabled))
                owner = _windows_dialog.OwnerWindow(101, 4321, was_enabled, api)
                owner.restore()
                if restore:
                    api.enable.assert_called_once_with(101)
                else:
                    api.enable.assert_not_called()


class WindowsDialogServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_services_pass_ownership_to_every_system_picker(self):
        host = OwnedServices()
        with patch.object(_files, "choose_path", new=AsyncMock(return_value=None)) as picker:
            self.assertIsNone(await host.choose_path())
            self.assertEqual(picker.call_args.kwargs["owner_process_id"], 4321)
            self.assertIsNone(await host.open_text_file())
            self.assertEqual(picker.call_args.kwargs["owner_process_id"], 4321)
            self.assertIsNone(await host.save_text_file("text", "notes.txt", None))
            self.assertEqual(picker.call_args.kwargs["owner_process_id"], 4321)
            self.assertEqual(picker.await_count, 3)

    async def test_process_id_is_data_and_does_not_change_the_script(self):
        launch = _files.plan_dialog(
            prepare_path_dialog(), platform="win32", which=lambda _: "powershell.exe",
        )
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=launch),
            patch.object(_files, "capture_owner", return_value=Mock(handle=101, process_id=4321)) as capture,
            patch.object(_files, "_execute", new=AsyncMock(return_value=(0, b"", b"", False))) as execute,
        ):
            self.assertIsNone(await _files.choose_path(owner_process_id=4321))
        actual = execute.call_args.args[0]
        self.assertEqual(actual.argv, launch.argv)
        self.assertEqual(actual.env["PYSUAL_OWNER_PID"], "4321")
        self.assertEqual(actual.env["PYSUAL_OWNER_HWND"], "101")
        self.assertEqual(launch.env["PYSUAL_OWNER_PID"], "")
        self.assertEqual(launch.env["PYSUAL_OWNER_HWND"], "")
        capture.return_value.restore.assert_called_once_with()

    async def test_task_cancellation_reaps_the_picker_before_reenabling_its_owner(self):
        launch = _files.plan_dialog(
            prepare_path_dialog(), platform="win32", which=lambda _: "powershell.exe",
        )
        entered = asyncio.Event()
        exited = asyncio.Event()
        calls = []

        class Process:
            returncode = None

            async def communicate(self, _stdin):
                entered.set()
                await exited.wait()
                calls.append("reaped")
                return b"", b""

            def kill(self):
                calls.append("killed")
                self.returncode = 1
                exited.set()

        window = Mock(handle=101, process_id=4321, restore=Mock(side_effect=lambda: calls.append("restored")))
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=launch),
            patch.object(_files, "capture_owner", return_value=window),
            patch.object(_files, "_spawn", new=AsyncMock(return_value=Process())),
        ):
            task = asyncio.create_task(_files.choose_path(owner_process_id=4321))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(calls, ["killed", "reaped", "restored"])

    async def test_helper_failure_still_restores_its_owner(self):
        launch = _files.plan_dialog(
            prepare_path_dialog(), platform="win32", which=lambda _: "powershell.exe",
        )
        window = Mock(handle=101, process_id=4321)
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "plan_dialog", return_value=launch),
            patch.object(_files, "capture_owner", return_value=window),
            patch.object(_files, "_execute", new=AsyncMock(side_effect=CapabilityError("failed"))),
        ):
            with self.assertRaises(CapabilityError):
                await _files.choose_path(owner_process_id=4321)
        window.restore.assert_called_once_with()

    async def test_invalid_process_ids_do_not_launch_a_dialog(self):
        with (
            patch.object(_files, "dialog_available", return_value=True),
            patch.object(_files, "_execute", new=AsyncMock()) as execute,
        ):
            for value in (0, -1, True, "4321", 0x80000000):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    await _files.choose_path(owner_process_id=value)
            execute.assert_not_awaited()
