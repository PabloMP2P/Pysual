"""Offline SVG bundles preserve Python, assets, notices and atomic publication."""

import base64
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from pysual.bundle import PYODIDE_VERSION, RUNTIME_FILES, build_web
from pysual.backends.web import WebHost
from pysual.backends._web_native import LiveSVGHost


class WebBuildTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.script = self.root / "counter.py"
        self.script.write_text(
            "from pysual import Window, Button\n"
            "class Counter(Window):\n"
            "    def build(self): self.action_button = Button(text='Count')\n"
            "    def action_button_on_click(self, event): self.action_button.text = 'Python callback'\n"
            "Counter().run_blocking()\n", encoding="utf-8")
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        for name in RUNTIME_FILES:
            (self.runtime / name).write_bytes(b"runtime fixture")
        (self.runtime / "package.json").write_text(json.dumps({"version": PYODIDE_VERSION}))
        self.output = self.root / "counter.html"

    def test_svg_bundle_contains_real_python_and_all_runtime_modules(self):
        (self.root / "helper.py").write_bytes(b"VALUE = 42\n")
        build_web(self.script, self.output, self.runtime, includes=("helper.py",))
        page = self.output.read_text(encoding="utf-8")
        self.assertIn('<svg id="app"', page)
        self.assertNotIn("<canvas", page)
        self.assertIn("runPythonAsync", page)
        self.assertIn("backend_override('web')", page)
        self.assertIn("pysual-notices", page)
        self.assertIn("Mozilla Public License", page)
        payload = json.loads(re.search(
            r'<script id="pysual-bundle" type="application/json">(.*?)</script>',
            page, re.S).group(1))
        self.assertEqual(payload["schema"], "pysual/1")
        files = {key: base64.b64decode(value) for key, value in payload["files"].items()}
        self.assertEqual(files["app.py"], self.script.read_bytes())
        self.assertTrue({"host.js", "svg.js", "input.js", "services.js"} <= files.keys())
        self.assertTrue({"pyodide/" + name for name in RUNTIME_FILES} <= files.keys())
        self.assertIn(b"createSVGClient", files["host.js"])
        with zipfile.ZipFile(io.BytesIO(files["pysual.zip"])) as archive:
            self.assertEqual(archive.read("helper.py"), b"VALUE = 42\n")
            self.assertIn("pysual/backends/_web_pyodide.py", archive.namelist())
            self.assertIn("pysual/assets/DejaVuSans.ttf", archive.namelist())
            self.assertIn("pysual/web/notices/Pyodide-LICENSE.txt", archive.namelist())
            self.assertIn("PYSUAL-LICENSE.txt", archive.namelist())
            self.assertFalse(any(name.endswith((".exe", ".dll", ".pyc")) for name in archive.namelist()))

    def test_local_runtime_does_not_download_and_build_is_deterministic(self):
        with patch("pysual.bundle.fetch_runtime", side_effect=AssertionError("network")):
            build_web(self.script, self.output, self.runtime)
            first = self.output.read_bytes()
            build_web(self.script, self.output, self.runtime)
        self.assertEqual(self.output.read_bytes(), first)

    def test_complete_runtime_notice_inventory_is_in_html_and_zip(self):
        build_web(self.script, self.output, self.runtime)
        page = self.output.read_text(encoding="utf-8")
        notices = re.search(r'<script id="pysual-notices" type="text/plain">(.*?)</script>',
                            page, re.S).group(1)
        # The compiled _random module's notice must accompany the interpreter.
        for text in ("Makoto Matsumoto", "Takuji Nishimura", "Redistributions in binary form",
                     "Emscripten authors", "HACL* Contributors"):
            self.assertIn(text, notices)
        payload = json.loads(re.search(
            r'<script id="pysual-bundle" type="application/json">(.*?)</script>', page, re.S).group(1))
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["files"]["pysual.zip"]))) as archive:
            prefix = "pysual/web/notices/"
            manifest = json.loads(archive.read(prefix + "manifest.json"))
            self.assertEqual(manifest["pyodide_version"], PYODIDE_VERSION)
            for name, entry in manifest["files"].items():
                data = archive.read(prefix + name)
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry["sha256"])
                self.assertIn(data.decode("utf-8"), notices)

    def test_truncated_runtime_notice_preserves_existing_output(self):
        import pysual.bundle as bundle

        package = self.root / "package"
        shutil.copytree(Path(bundle.__file__).parent / "web/notices", package / "web/notices")
        (package / "web/notices/CPython-LICENSE.txt").write_text("truncated license")
        self.output.write_bytes(b"previous publication")
        with patch.object(bundle, "__file__", str(package / "bundle.py")):
            with self.assertRaisesRegex(ValueError, "notice checksum mismatch"):
                build_web(self.script, self.output, self.runtime)
        self.assertEqual(self.output.read_bytes(), b"previous publication")

    def test_failed_validation_preserves_existing_output(self):
        self.output.write_text("old artifact")
        (self.runtime / "package.json").write_text('{"version":"wrong"}')
        with self.assertRaisesRegex(ValueError, "0.29.0"):
            build_web(self.script, self.output, self.runtime)
        self.assertEqual(self.output.read_text(), "old artifact")

    def test_reserved_and_outside_includes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "inside"):
            build_web(self.script, self.output, self.runtime, includes=("../outside.py",))
        (self.root / "app.py").write_text("reserved")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            build_web(self.script, self.output, self.runtime, includes=("app.py",))
        with self.assertRaisesRegex(ValueError, "runtime"):
            build_web(self.script, self.runtime / "app.html", self.runtime)
        (self.root / "assets").mkdir()
        with self.assertRaisesRegex(ValueError, "included"):
            build_web(self.script, self.root / "assets/app.html", self.runtime,
                      includes=("assets",))

    def test_one_public_factory_selects_current_python_environment(self):
        with patch("pysual.backends.web.sys.platform", "win32"):
            self.assertIsInstance(WebHost(open_browser=False), LiveSVGHost)
        with patch("pysual.backends.web.sys.platform", "emscripten"), patch(
            "pysual.backends._web_pyodide.BundledSVGHost", return_value="browser"):
            self.assertEqual(WebHost(), "browser")
            with self.assertRaises(TypeError):
                WebHost(port=1234)


if __name__ == "__main__":
    unittest.main()
