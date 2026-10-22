"""A system picker returns one exact, absolute filesystem path."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from pysual.backends import _files
from pysual.host import CapabilityError, prepare_path_dialog


class DialogPathProtocolTests(unittest.TestCase):
    def test_posix_framing_removes_only_one_line_feed(self):
        for tool in ("osascript", "zenity", "kdialog"):
            for name in ("name\r", "name\n", "line\nbreak", "tab\tname", "name "):
                with self.subTest(tool=tool, name=name):
                    self.assertEqual(_files._dialog_text(tool, (name + "\n").encode()), name)

    def test_native_results_cannot_silently_resolve_against_python_working_directory(self):
        for tool in ("powershell", "osascript", "zenity", "kdialog"):
            with self.subTest(tool=tool), self.assertRaisesRegex(CapabilityError, "relative path"):
                _files.interpret_dialog(tool, 0, b"relative.txt\n", b"")

    def test_uri_parsing_must_not_silently_remove_control_characters(self):
        for value in ("file:///tmp/a\nb", "file:///tmp/a\rb", "file:///tmp/a\tb"):
            with self.subTest(value=value), self.assertRaises(CapabilityError):
                _files.chosen_path(value)

    def test_zenity_startup_errors_are_distinct_from_user_cancellation(self):
        self.assertIsNone(_files.interpret_dialog("zenity", 10, b"", b""))
        for code in (1, 11, 12, 255):
            with self.subTest(code=code), self.assertRaises(CapabilityError):
                _files.interpret_dialog("zenity", code, b"", b"cannot open display")

    def test_applescript_error_text_cannot_mimic_the_cancellation_error_number(self):
        self.assertIsNone(_files.interpret_dialog("osascript", 1, b"", b"User cancelled. (-128)\n"))
        with self.assertRaises(CapabilityError):
            _files.interpret_dialog("osascript", 1, b"", b"Cannot open folder (-128): permission denied. (-54)\n")


@unittest.skipIf(os.name == "nt", "requires POSIX filename rules")
class PosixSelectedPathTests(unittest.TestCase):
    def test_selected_control_characters_and_spaces_refer_to_the_exact_file(self):
        with tempfile.TemporaryDirectory() as folder:
            for name in ("line\nbreak", "trailing\r", "trailing\n", "tab\tname", "delete\x7f", "  café "):
                selected = Path(folder) / name
                selected.write_text("unchanged", encoding="utf-8")
                for tool in ("osascript", "zenity", "kdialog"):
                    with self.subTest(tool=tool, name=name):
                        actual = _files.interpret_dialog(tool, 0, os.fsencode(selected) + b"\n", b"")
                        self.assertEqual(actual, selected)
                        self.assertEqual(actual.read_text(encoding="utf-8"), "unchanged")

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "requires a Linux filesystem that stores non-UTF-8 names",
    )
    def test_linux_filesystem_bytes_round_trip_without_utf8_replacement(self):
        with tempfile.TemporaryDirectory() as folder:
            raw = os.fsencode(folder) + b"/raw-\xff.txt"
            with open(raw, "wb") as stream:
                stream.write(b"original")
            for tool in ("zenity", "kdialog"):
                with self.subTest(tool=tool):
                    actual = _files.interpret_dialog(tool, 0, raw + b"\n", b"")
                    self.assertEqual(os.fsencode(actual), raw)
                    self.assertEqual(actual.read_bytes(), b"original")

    def test_nul_is_never_a_legal_filesystem_path(self):
        with self.assertRaises(CapabilityError):
            _files.chosen_path("/tmp/bad\0name")

    def test_selected_posix_folder_can_be_used_as_the_next_initial_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "line\nbreak\r"
            directory.mkdir()
            request = prepare_path_dialog(initial_directory=str(directory))
            self.assertEqual(request.initial_directory, str(directory))
