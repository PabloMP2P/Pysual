"""Build contracts: native fallback, isolated stages, and publication behavior."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest
import pysual.cli as builder


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = tmp_path / "src/pysual"
    (source / "assets").mkdir(parents=True)
    (source / "assets/font.ttf").write_bytes(b"font")
    (tmp_path / "LICENSE").write_text("license", encoding="utf-8")
    script = tmp_path / "app.py"
    script.write_text("print('app')\n", encoding="utf-8")
    python_notice = tmp_path / "python-license.txt"
    python_notice.write_text("PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\nComplete notice fixture\n", encoding="utf-8")
    monkeypatch.setattr(builder, "_python_license_path", lambda override=None: Path(override) if override is not None else python_notice)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder, "SOURCE", tmp_path / "src")
    monkeypatch.setattr(builder.importlib.util, "find_spec", lambda name: object())
    return tmp_path, script, source


def freeze_success(command, **kwargs):
    assert "PyInstaller" in command
    destination = Path(command[command.index("--distpath") + 1])
    destination.mkdir(parents=True)
    (destination / ("pysual_app.exe" if os.name == "nt" else "pysual_app")).write_bytes(b"complete executable")
    return subprocess.CompletedProcess(command, 0)


@pytest.mark.parametrize("terminal", ["auto", "python"])
def test_terminal_freezes_without_compiler_or_host(project, monkeypatch, terminal):
    root, script, _ = project
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        hook = Path(command[command.index("--runtime-hook") + 1]).read_text(encoding="utf-8")
        assert "backend_override('terminal')" in hook
        assert f"PYSUAL_TERMINAL'] = '{terminal}'" in hook
        return freeze_success(command, **kwargs)
    monkeypatch.setattr(builder.subprocess, "run", run)
    output = root / "dist/app.exe"
    assert builder.build_executable(script, output, backend="terminal", terminal=terminal) == output
    assert output.read_bytes() == b"complete executable"
    assert len(commands) == 1
    assert "--add-binary" not in commands[0]
    assert not list(output.parent.glob(".pysual-build-*"))


def test_native_dependencies_are_binaries_and_auto_terminal_can_use_host(project, monkeypatch):
    root, script, source = project
    native = source / "bin"
    native.mkdir()
    (native / ("pysual-host.exe" if os.name == "nt" else "pysual-host")).write_bytes(b"host")
    for name in ("SDL3.dll", "libSDL3.so.0", "libSDL3.dylib", "SDL-LICENSE.txt"):
        (native / name).write_bytes(b"asset")
    captured = []
    def run(command, **kwargs):
        captured.extend(command)
        return freeze_success(command, **kwargs)
    monkeypatch.setattr(builder.subprocess, "run", run)
    builder.build_executable(script, root / "app.exe", backend="terminal")
    for index, item in enumerate(captured):
        if "pysual/bin" in item:
            assert captured[index - 1] == ("--add-data" if "LICENSE" in item else "--add-binary")


def test_failed_freeze_preserves_previous_output(project, monkeypatch):
    root, script, _ = project
    output = root / "app.exe"
    output.write_bytes(b"previous")
    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(builder.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        builder.build_executable(script, output, backend="terminal")
    assert output.read_bytes() == b"previous"
    assert not list(root.glob(".pysual-build-*"))


def test_requested_c_terminal_builds_without_sdl(project, monkeypatch):
    root, script, source = project
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if "--terminal-only" in command:
            native = source / "bin"
            native.mkdir()
            (native / ("pysual-host.exe" if os.name == "nt" else "pysual-host")).write_bytes(b"host")
            return subprocess.CompletedProcess(command, 0)
        return freeze_success(command, **kwargs)
    monkeypatch.setattr(builder.subprocess, "run", run)
    builder.build_executable(script, root / "app.exe", backend="terminal", terminal="c")
    assert len(commands) == 2 and "--terminal-only" in commands[0]


def test_window_rebuilds_terminal_only_native_host(project, monkeypatch):
    root, script, source = project
    native = source / "bin"
    native.mkdir()
    (native / ("pysual-host.exe" if os.name == "nt" else "pysual-host")).write_bytes(b"terminal host")
    marker = native / "native-build.json"
    marker.write_text('{"window":false}', encoding="utf-8")
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if "PyInstaller" not in command:
            assert "--terminal-only" not in command
            marker.write_text('{"window":true}', encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)
        return freeze_success(command, **kwargs)
    monkeypatch.setattr(builder.subprocess, "run", run)
    builder.build_executable(script, root / "app.exe", backend="window")
    assert len(commands) == 2


@pytest.mark.parametrize("include", ["../outside", "/outside"])
def test_invalid_assets_fail_before_native_build(project, monkeypatch, include):
    root, script, _ = project
    monkeypatch.setattr(builder.subprocess, "run", lambda *_a, **_k: pytest.fail("must validate before compiling"))
    with pytest.raises(ValueError):
        builder.build_executable(script, root / "app.exe", includes=[include])


def test_included_directories_and_files_keep_entry_script_relative_paths(project, monkeypatch):
    root, script, _ = project
    (root / "assets/nested").mkdir(parents=True)
    (root / "assets/nested/message.txt").write_text("directory", encoding="utf-8")
    (root / "settings.txt").write_text("file", encoding="utf-8")
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return freeze_success(command, **kwargs)
    monkeypatch.setattr(builder.subprocess, "run", run)
    builder.build_executable(script, root / "app.exe", backend="terminal", terminal="python",
                             includes=("assets", "assets/nested/message.txt", "settings.txt"))
    command = commands[0]
    data = [command[index + 1] for index, item in enumerate(command) if item == "--add-data"]
    assert f"{root / 'assets'}{os.pathsep}assets" in data
    assert f"{root / 'assets/nested/message.txt'}{os.pathsep}{Path('assets/nested')}" in data
    assert f"{root / 'settings.txt'}{os.pathsep}." in data


def test_run_selects_target_without_autoconfig(project, monkeypatch):
    _, script, _ = project
    calls = []
    monkeypatch.setattr(builder.subprocess, "call", lambda command, **kwargs: calls.append((command, kwargs)) or 0)
    assert builder.main(["run", str(script), "--backend", "terminal", "--terminal", "python"]) == 0
    command, options = calls[0]
    assert command[:2] == [sys.executable, "-c"]
    assert "backend_override(target)" in command[2]
    assert "sys.path.insert(0,str(Path(script).parent))" in command[2]
    assert options["env"]["PYSUAL_TERMINAL"] == "python"


def test_package_frontend_runs_from_checkout(project, monkeypatch):
    root, _, _ = project
    calls = []
    monkeypatch.setattr(builder.subprocess, "call", lambda command, **kwargs: calls.append((command, kwargs)) or 0)
    assert builder.main(["package", "--wheel"]) == 0
    command, options = calls[0]
    assert command[:3] == [sys.executable, "-m", "build"]
    assert command[-1] == "--wheel"
    assert options["cwd"] == root
    assert str(root) in command


@pytest.mark.parametrize("arguments", [[], ["hello world", "café λ", "--backend", "web", "--", ""]])
def test_run_passes_application_arguments_and_preserves_backend_override(tmp_path, arguments):
    result = tmp_path / "arguments.json"
    script = tmp_path / "entry script.py"
    script.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "from pysual.backends import resolve_backend\n"
        f"Path({str(result)!r}).write_text(json.dumps({{"
        "'argv': sys.argv, 'backend': resolve_backend('window'), "
        "'terminal': os.environ['PYSUAL_TERMINAL']}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    assert builder.main(["run", str(script), "--backend", "terminal", "--terminal", "python", "--", *arguments]) == 0
    assert json.loads(result.read_text(encoding="utf-8")) == {
        "argv": [str(script.resolve()), *arguments], "backend": "terminal", "terminal": "python",
    }


def test_run_still_rejects_unknown_runner_options(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "_run_script", lambda *_: pytest.fail("must validate runner options"))
    with pytest.raises(SystemExit) as caught:
        builder.main(["run", str(tmp_path / "app.py"), "--unknown", "--", "--app-option"])
    assert caught.value.code == 2


def test_default_package_builds_native_wheel_directly_from_checkout(project, monkeypatch):
    calls = []
    monkeypatch.setattr(builder.subprocess, "call", lambda command, **kwargs: calls.append(command) or 0)
    assert builder.main(["package"]) == 0
    assert calls[0][-2:] == ["--wheel", "--sdist"]


def test_build_subcommand_is_optional(project, monkeypatch):
    root, script, _ = project
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        return freeze_success(command, **kwargs)
    monkeypatch.setattr(builder.subprocess, "run", run)
    output = root / "dist/app.exe"
    assert builder.main(["build", str(script), "--backend", "terminal", "--terminal", "python",
                         "-o", str(output)]) == 0
    assert output.read_bytes() == b"complete executable"
    assert len(commands) == 1


def test_checkout_module_entry_and_packaging_frontend_are_distinct():
    checkout = Path(__file__).resolve().parents[1]
    module = subprocess.run([sys.executable, "-m", "pysual", "--help"], capture_output=True, text=True,
                            cwd=checkout, env={**os.environ, "PYTHONPATH": str(checkout / "src")})
    assert module.returncode == 0
    assert "--backend {window,terminal,web}" in module.stdout
    # The build extra is optional; test name resolution without requiring it.
    frontend = subprocess.run([sys.executable, "-c",
                              "import importlib.util; s=importlib.util.find_spec('build'); "
                              "print(s.origin if s else 'not installed')"],
                             capture_output=True, text=True, cwd=checkout)
    assert frontend.returncode == 0
    assert str(checkout / "build.py") not in frontend.stdout


def test_main_module_import_does_not_run_cli():
    imported = subprocess.run([sys.executable, "-c", "import pysual.__main__; print('imported')"],
                              capture_output=True, text=True)
    assert imported.returncode == 0
    assert imported.stdout.strip() == "imported"


def test_commands_needing_the_checkout_explain_it_outside_one(monkeypatch, capsys):
    monkeypatch.setattr(builder, "ROOT", None)
    assert builder.main(["native"]) == 1
    assert builder.main(["package"]) == 1
    assert "repository checkout" in capsys.readouterr().err


@pytest.mark.parametrize("relative", ["LICENSE.txt", "LICENSE", "lib/pythonVERSION/LICENSE.txt"])
def test_python_notice_discovery_uses_base_interpreter(tmp_path, monkeypatch, relative):
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    notice = tmp_path / relative.replace("VERSION", version)
    notice.parent.mkdir(parents=True, exist_ok=True)
    notice.write_text("PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2\n", encoding="utf-8")
    monkeypatch.setattr(builder.sys, "base_prefix", str(tmp_path))
    assert builder._python_license_path() == notice


def test_python_notice_override_and_missing_notice(tmp_path, monkeypatch):
    monkeypatch.setattr(builder.sys, "base_prefix", str(tmp_path / "interpreter"))
    with pytest.raises(RuntimeError, match="--python-license"):
        builder._python_license_path()
    override = tmp_path / "vendor-notice.txt"
    override.write_text("Unrelated license\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="--python-license"):
        builder._python_license_path(override)
    override.write_text("PSF LICENSE AGREEMENT FOR PYTHON 3.11.0\n", encoding="utf-8")
    assert builder._python_license_path(override) == override


def test_frozen_notice_has_distinct_name_and_preserves_complete_content(project, monkeypatch):
    root, script, _ = project
    notice = root / "custom-notice.txt"
    contents = b"Full interpreter notice\nIncluding bundled component notices\n"
    notice.write_bytes(contents)
    def run(command, **kwargs):
        data = [command[index + 1] for index, part in enumerate(command) if part == "--add-data"]
        entry = next(item for item in data if "CPython-LICENSE.txt" in item)
        staged, destination = entry.rsplit(os.pathsep, 1)
        assert destination == "pysual/licenses"
        assert Path(staged).name == "CPython-LICENSE.txt"
        assert Path(staged).read_bytes() == contents
        return freeze_success(command, **kwargs)
    monkeypatch.setattr(builder.subprocess, "run", run)
    builder.build_executable(script, root / "app.exe", backend="terminal", terminal="python", python_license=notice)


def test_cli_passes_python_notice_override(project, monkeypatch):
    root, script, _ = project
    captured = {}
    def build(script, output, **kwargs):
        captured.update(kwargs)
        return output
    monkeypatch.setattr(builder, "build_executable", build)
    notice = root / "python-license.txt"
    assert builder.main(["build", str(script), "--backend", "terminal", "--python-license", str(notice)]) == 0
    assert captured["python_license"] == notice


@pytest.mark.parametrize("arguments", [["run"], ["build", "--backend", "web"]])
def test_python_notice_option_is_only_for_executable_builds(project, arguments):
    root, script, _ = project
    with pytest.raises(SystemExit):
        builder.main([*arguments, str(script), "--python-license", str(root / "python-license.txt")])
