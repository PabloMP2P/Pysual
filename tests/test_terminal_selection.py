"""The terminal remains usable when no native binary or graphics library exists."""

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from pysual import Rect
from pysual.backends import BACKEND_NAMES, get_backend, resolve_backend
from pysual.backends._native_client import NativeHostError
from pysual.backends._native_client import native_executable
from pysual.backends.terminal import TerminalHost, terminal, terminal_renderer_override
from _terminal import MemoryTerminal


class TerminalSelectionTests(unittest.TestCase):
    def setUp(self):
        config = patch("pysual._config.current", return_value=SimpleNamespace(terminal_renderer=None))
        config.start()
        self.addCleanup(config.stop)

    def open(self, host):
        self.addCleanup(host.close)
        host.open("terminal selection", 160, 64, False, 1)
        return host

    def test_only_three_backend_ids_are_public(self):
        self.assertEqual(BACKEND_NAMES, ("window", "terminal", "web"))
        self.assertEqual(resolve_backend(), "window")
        for name in ("sdl3", "sdl2", "pygame", "raylib", "term-text", "term-pixels", "web-live", "web-bundle"):
            with self.assertRaises(ValueError):
                get_backend(name)

    def test_python_cells_and_unicode_work_without_native_artifacts(self):
        host = self.open(terminal(renderer="python", hidden=True, executable="does-not-exist"))
        host.begin("#123456")
        host.text("caf\u00e9 \u03bb \u597d", 0, 0, "#ffffff", 16)
        host.rect(Rect(0, 32, 16, 16), "#ff0000")
        host.present()
        snapshot = host.snapshot()
        self.assertEqual((snapshot["columns"], snapshot["rows"]), (20, 4))
        self.assertTrue(snapshot["rows_text"][0].startswith("caf\u00e9 \u03bb \u597d"))
        self.assertEqual(snapshot["cells"][40]["background"], [255, 0, 0])
        self.assertEqual(host.renderer, "python")
        self.assertIsNone(host.fallback_reason)

    def test_auto_falls_back_when_helper_is_missing_and_explains_why(self):
        missing = Path(__file__).resolve().parent / "absent" / uuid4().hex
        host = self.open(terminal(renderer="auto", hidden=True, executable=missing))
        self.assertEqual(host.renderer, "python")
        self.assertIn("missing", host.fallback_reason)
        host.begin("#000000")
        host.text("fallback", 0, 0, "#ffffff", 16)
        host.present()
        self.assertTrue(host.snapshot()["rows_text"][0].startswith("fallback"))

    def test_explicit_c_does_not_silently_fall_back(self):
        host = terminal(renderer="c", hidden=True, executable="does-not-exist")
        self.addCleanup(host.close)
        with self.assertRaisesRegex(NativeHostError, "missing"):
            host.open("explicit C", 160, 64, False, 1)
        self.assertIsNone(host.renderer)

    def test_config_environment_and_explicit_choice_precedence(self):
        with patch.dict(os.environ, {"PYSUAL_TERMINAL": "python"}):
            self.assertEqual(terminal().requested_renderer, "python")
            with patch("pysual._config.current", return_value=SimpleNamespace(terminal_renderer="c")):
                self.assertEqual(terminal().requested_renderer, "c")
                self.assertEqual(terminal(renderer="python").requested_renderer, "python")
        with self.assertRaises(ValueError):
            terminal(renderer="bogus")

    def test_build_override_beats_configuration_and_restores_prior_choice(self):
        with patch("pysual._config.current", return_value=SimpleNamespace(terminal_renderer="c")):
            with terminal_renderer_override("python"):
                host = self.open(terminal(hidden=True, executable="does-not-exist"))
                self.assertEqual(host.requested_renderer, "python")
                self.assertEqual(host.renderer, "python")
                self.assertEqual(terminal(renderer="c").requested_renderer, "c")
                with terminal_renderer_override("auto"):
                    self.assertEqual(terminal().requested_renderer, "auto")
                self.assertEqual(terminal().requested_renderer, "python")
            self.assertEqual(terminal().requested_renderer, "c")

    def test_injected_byte_transport_routes_input_and_restores_modes(self):
        io = MemoryTerminal(columns=20, rows=4)
        host = self.open(terminal(io=io, probe_timeout=0))
        self.assertEqual(host.renderer, "python")
        io.incoming = b"x\x1b[200~hello\x1b[201~"
        events = host.poll()
        self.assertEqual([e.text for e in events if e.kind == "text"], ["x", "hello"])
        host.close()
        self.assertEqual((io.entered, io.exited), (1, 1))
        self.assertIn(b"\x1b[?1049l", b"".join(io.output))

    def test_python_fallback_has_no_optional_runtime_imports(self):
        source = str(Path(__file__).resolve().parents[1] / "src")
        script = "\n".join((
            "import sys", f"sys.path.insert(0, {source!r})",
            "from pysual.backends.terminal import terminal",
            "h=terminal(renderer='python',hidden=True,executable='absent')",
            "h.open('isolated',160,64,False,1)", "h.begin('#000000')", "h.present()", "h.close()",
            "assert not any(n in sys.modules for n in ('sdl3','pygame','pyray','PIL'))",
        ))
        subprocess.run([sys.executable, "-I", "-c", script], check=True, timeout=10)


@unittest.skipUnless(native_executable().is_file(), "Build the C helper for native terminal selection tests")
class NativeTerminalSelectionTests(unittest.TestCase):
    def open(self, renderer):
        host = terminal(renderer=renderer, hidden=True, color="truecolor")
        self.addCleanup(host.close)
        host.open("native selection", 160, 64, False, 1)
        return host

    def test_auto_selects_real_c_and_matches_python_cells(self):
        native, python = self.open("auto"), self.open("python")
        self.assertEqual(native.renderer, "c")
        self.assertIsNone(native.fallback_reason)
        for host in (native, python):
            host.begin("#123456")
            host.text("Hello \u597d e\u0301", 0, 0, "#ffffff", 16)
            host.rect(Rect(8, 32, 80, 16), "#ff440088", 4, "#aaffee", 1)
            host.present()
        self.assertEqual(native.snapshot(), python.snapshot())

    def test_native_scene_errors_never_switch_renderers(self):
        host = self.open("auto")
        host.begin("#123456")
        host.text("retained", 0, 0, "#ffffff", 16)
        host.present()
        before = host.snapshot()
        with self.assertRaises(NativeHostError):
            host._host._request("patch", upsert=[{"id": "bad", "bounds": [0, 0, 10, 10], "commands": [["unknown"]]}])
        self.assertEqual(host.renderer, "c")
        self.assertEqual(host.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
