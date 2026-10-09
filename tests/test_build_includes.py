"""Include boundaries apply to real redirects before either builder reads assets."""

import base64
import io
import json
import os
from pathlib import Path
import re
import subprocess
import zipfile

import pytest

from pysual.bundle import PYODIDE_VERSION, RUNTIME_FILES, build_web
import pysual.cli as cli


@pytest.fixture
def project(tmp_path):
    app = tmp_path / "application"
    (app / "assets").mkdir(parents=True)
    script = app / "entry.py"
    script.write_text("print('application')\n", encoding="utf-8")
    private = tmp_path / "private"
    private.mkdir()
    (private / "secret.txt").write_text("synthetic outside content", encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    for name in RUNTIME_FILES:
        (runtime / name).write_bytes(b"runtime fixture")
    (runtime / "package.json").write_text(json.dumps({"version": PYODIDE_VERSION}))
    return app, script, private, runtime


@pytest.fixture(params=["symlink", "junction"])
def redirect(request):
    links = []

    def create(link, target):
        if request.param == "junction":
            if os.name != "nt":
                pytest.skip("Windows directory junction")
            result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, text=True)
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError as error:
                pytest.skip(f"Creating symlinks requires platform permission: {error}")
        links.append(link)

    yield create
    for link in reversed(links):
        if request.param == "junction":
            os.rmdir(link)  # Remove the junction itself, never its target.
        else:
            link.unlink()


@pytest.mark.parametrize("include", ["assets", "assets/linked/secret.txt", "assets/linked"])
@pytest.mark.parametrize("backend", ["web", "executable"])
def test_redirects_are_rejected_before_asset_read_and_preserve_output(project, redirect, monkeypatch,
                                                                     include, backend):
    app, script, private, runtime = project
    redirect(app / "assets/linked", private)
    output = app / ("published.html" if backend == "web" else "published.exe")
    output.write_bytes(b"previous publication")
    original = Path.read_bytes

    def guarded_read(path):
        assert not path.resolve().is_relative_to(private), "outside asset must not be read"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    if backend == "web":
        build = lambda: build_web(script, output, runtime, includes=[include])
    else:
        monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _: object())
        monkeypatch.setattr(cli.subprocess, "run", lambda *_a, **_kw: pytest.fail("must reject before freeze"))
        build = lambda: cli.build_executable(script, output, backend="terminal", terminal="python",
                                             includes=[include])
    with pytest.raises(ValueError, match="symlinks and reparse points"):
        build()
    assert output.read_bytes() == b"previous publication"


def test_directory_cycle_is_rejected_before_descent(project, redirect):
    app, script, _, runtime = project
    redirect(app / "assets/loop", app / "assets")
    with pytest.raises(ValueError, match="symlinks and reparse points"):
        build_web(script, app / "published.html", runtime, includes=["assets"])


def test_regular_nested_assets_are_preserved_in_actual_zip(project):
    app, script, _, runtime = project
    (app / "assets/nested").mkdir()
    (app / "assets/nested/message.txt").write_bytes(b"included content")
    output = build_web(script, app / "published.html", runtime, includes=["assets"])
    payload = json.loads(re.search(r'<script id="pysual-bundle" type="application/json">(.*?)</script>',
                                   output.read_text(encoding="utf-8"), re.S).group(1))
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(payload["files"]["pysual.zip"]))) as archive:
        assert archive.read("assets/nested/message.txt") == b"included content"
        assert not any("secret.txt" in name for name in archive.namelist())
