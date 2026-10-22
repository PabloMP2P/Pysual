"""Compile the shipped AppleScript on macOS without presenting a dialog."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from pysual.backends import _files
from pysual.host import prepare_path_dialog


@unittest.skipUnless(sys.platform == "darwin", "requires the macOS AppleScript compiler")
class MacOSDialogScriptTests(unittest.TestCase):
    def test_system_compiler_accepts_the_complete_dialog_script(self):
        launch = _files.plan_dialog(prepare_path_dialog(), platform="darwin")
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "dialog.applescript"
            assert launch.stdin is not None
            source.write_bytes(launch.stdin)
            result = subprocess.run(
                ["/usr/bin/osacompile", "-o", str(Path(folder) / "dialog.scpt"), str(source)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
