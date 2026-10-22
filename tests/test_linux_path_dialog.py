"""Linux chooser arguments preserve the public suffix and cancellation contracts."""

import asyncio
import fnmatch
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

from pysual.backends import _files
from pysual.host import CapabilityError, PathDialog


def find_zenity(name):
    return "/usr/bin/zenity" if name == "zenity" else None


class LinuxDialogPlanTests(unittest.TestCase):
    def test_zenity_disables_legacy_console_transcoding_for_utf8_python(self):
        with (
            patch.dict(os.environ, {"LC_ALL": "C", "CHARSET": "ASCII"}, clear=True),
            patch.object(sys, "getfilesystemencoding", return_value="utf-8"),
        ):
            launch = _files.plan_dialog(
                PathDialog(False, False, "", "", None), platform="linux", which=find_zenity,
            )
        self.assertEqual(launch.env["CHARSET"], "UTF-8")

    def test_zenity_preserves_non_utf8_python_and_explicit_filename_encodings(self):
        for encoding, environment in (
            ("iso8859-1", {}),
            ("utf-8", {"G_FILENAME_ENCODING": "@locale"}),
            ("utf-8", {"G_BROKEN_FILENAMES": "1"}),
        ):
            with (
                self.subTest(encoding=encoding, environment=environment),
                patch.dict(os.environ, environment, clear=True),
                patch.object(sys, "getfilesystemencoding", return_value=encoding),
            ):
                launch = _files.plan_dialog(
                    PathDialog(False, False, "", "", None), platform="linux", which=find_zenity,
                )
                self.assertNotIn("CHARSET", launch.env)

    def test_gtk_filter_accepts_mixed_case_suffixes_without_widening_the_filter(self):
        request = PathDialog(False, False, "", "", None, (".txt", ".tar.gz", ".7z"))
        launch = _files.plan_dialog(request, platform="linux", which=find_zenity)
        argument = next(item for item in launch.argv if item.startswith("--file-filter="))
        patterns = argument.split(" | ", 1)[1].split()
        for name in ("notes.txt", "notes.TXT", "notes.tXt", "archive.Tar.GZ", "data.7Z"):
            with self.subTest(name=name):
                self.assertTrue(any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns))
        for name in ("notes.tx", "notes.txt.bak", "archive.gz", "notes.py", "README"):
            with self.subTest(name=name):
                self.assertFalse(any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns))

    def test_case_insensitive_gtk_filter_keeps_the_all_files_choice(self):
        request = PathDialog(True, False, "notes", "Save", "/tmp", (".txt",), True)
        launch = _files.plan_dialog(request, platform="linux", which=find_zenity)
        self.assertIn("--file-filter=All files | *", launch.argv)
        self.assertIn("--save", launch.argv)
        self.assertIn("--confirm-overwrite", launch.argv)
        self.assertIn("--filename=/tmp/notes", launch.argv)


class LinuxDialogExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_inherited_zenity_exit_overrides_cannot_change_the_dialog_protocol(self):
        launch = _files.plan_dialog(
            PathDialog(False, False, "", "", None), platform="linux", which=find_zenity,
        )
        inherited = {
            "ZENITY_OK": "50", "ZENITY_CANCEL": "0", "ZENITY_ESC": "0",
            "ZENITY_EXTRA": "0", "ZENITY_ERROR": "0", "ZENITY_TIMEOUT": "0",
            "DIALOG_OK": "50", "DIALOG_CANCEL": "0", "DIALOG_ERROR": "0",
        }
        process = AsyncMock()
        process.returncode = int(launch.env["ZENITY_CANCEL"])
        process.communicate.return_value = (b"", b"Gtk-WARNING: mapped without a parent")
        with (
            patch.dict(os.environ, inherited),
            patch.object(_files, "_spawn", new=AsyncMock(return_value=process)) as spawn,
        ):
            code, output, error, killed = await _files._execute(
                launch, _files._ActiveDialog(asyncio.get_running_loop(), None),
            )
        environment = spawn.call_args.args[1]
        self.assertEqual(environment["ZENITY_OK"], "0")
        for key in ("ZENITY_CANCEL", "ZENITY_ESC", "ZENITY_EXTRA"):
            self.assertEqual(environment[key], "10")
        self.assertNotEqual(environment["ZENITY_ERROR"], "0")
        self.assertNotEqual(environment["ZENITY_TIMEOUT"], "0")
        self.assertFalse(killed)
        self.assertIsNone(_files.interpret_dialog("zenity", code, output, error))

    async def test_gtk_startup_failure_is_not_treated_as_user_cancellation(self):
        with self.assertRaisesRegex(CapabilityError, "cannot open display"):
            _files.interpret_dialog("zenity", 1, b"", b"Gtk-WARNING: cannot open display: :99")

