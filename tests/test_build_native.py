"""Native dependency boundaries and compiler environment handling."""
from pathlib import Path
import hashlib
import json
import os
import shutil
from types import SimpleNamespace
import zipfile

import pytest
from tools import fetch_native_deps as deps
from tools import native


def test_verified_archive_uses_cache_without_network(tmp_path, monkeypatch):
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"verified")
    monkeypatch.setattr(deps.urllib.request, "urlopen", lambda *_a, **_k: pytest.fail("unexpected download"))
    deps.download("https://example.invalid/archive", archive, hashlib.sha256(b"verified").hexdigest())


@pytest.mark.parametrize("member", ["../escape", "SDL3-1/../../escape", "other/file", "SDL3-1/C:/escape"])
def test_archive_rejects_paths_before_extracting(tmp_path, member):
    archive = tmp_path / "archive.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("SDL3-1/safe", b"safe")
        stream.writestr(member, b"unsafe")
    destination = tmp_path / "deps"
    destination.mkdir()
    with pytest.raises(ValueError):
        deps.extract(archive, destination, "SDL3-1")
    assert not list(destination.iterdir())


def test_archive_path_rejects_windows_separators(tmp_path):
    with pytest.raises(ValueError):
        deps.checked_path(tmp_path, "SDL3-1\\escape", "SDL3-1")


def test_native_rejects_partial_roots_before_compiler_lookup(monkeypatch):
    monkeypatch.setattr(native, "compiler_environment", lambda *_a: pytest.fail("invalid inputs reached compiler"))
    with pytest.raises(SystemExit):
        native.main(["--sdl-root", "/somewhere"])


def test_pkgconfig_accepts_distribution_aliases(monkeypatch):
    seen = []
    class Result:
        def __init__(self, code):
            self.returncode = code
    def run(command, **kwargs):
        seen.append(command[-1])
        return Result(0 if command[-1] in ("sdl3", "sdl3-ttf", "sdl3-image") else 1)
    monkeypatch.setattr(native.subprocess, "run", run)
    monkeypatch.setattr(native.subprocess, "check_output", lambda *_a, **_k: '-I"/a path/include" -L/lib -lSDL3')
    assert native.pkg_config_flags() == ["-I/a path/include", "-L/lib", "-lSDL3"]
    assert seen == ["sdl3", "SDL3_ttf", "sdl3-ttf", "SDL3_image", "sdl3-image"]


@pytest.mark.parametrize("machine,override,expected", [
    ("arm64", None, "11.0"), ("x86_64", None, "10.15"), ("arm64", "26.0", "26.0"),
])
def test_mac_compiler_receives_explicit_deployment_target(tmp_path, monkeypatch, machine, override, expected):
    monkeypatch.setattr(native, "ROOT", tmp_path)
    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(native, "os", SimpleNamespace(name="posix", replace=os.replace))
    monkeypatch.setattr(native.platform, "machine", lambda: machine)
    environment = {"PATH": "/usr/bin"}
    if override:
        environment["MACOSX_DEPLOYMENT_TARGET"] = override
    monkeypatch.setattr(native, "compiler_environment", lambda _compiler: ("/usr/bin/clang", environment))
    license_path = tmp_path / "native/vendor/cJSON.LICENSE"
    license_path.parent.mkdir(parents=True)
    license_path.write_text("license", encoding="utf-8")
    calls = []
    def compile(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[command.index("-o") + 1]).write_bytes(b"compiled Mach-O placeholder")
    monkeypatch.setattr(native.subprocess, "run", compile)
    assert native.main(["--terminal-only"]) == 0
    assert len(calls) == 1
    assert calls[0][1]["env"]["MACOSX_DEPLOYMENT_TARGET"] == expected
    assert calls[0][0][0] == "/usr/bin/clang"


@pytest.mark.parametrize("target", ["26.6", "26.0.1", "unknown"])
def test_mac_build_rejects_nonrepresentable_deployment_target(target):
    with pytest.raises(RuntimeError, match="deployment|DEPLOYMENT|major"):
        native.macos_deployment_environment({"MACOSX_DEPLOYMENT_TARGET": target})


def test_text_engine_notice_inventory_matches_pinned_sdk():
    directory = native.ROOT / "native/notices"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    release = next(item for item in deps.RELEASES if item[1] == manifest["library"])
    assert manifest["version"] == release[2]
    assert manifest["windows_archive_sha256"] == release[3]["windows"]
    assert {item["component"] for item in manifest["files"]} >= {
        "FreeType", "FreeType BDF driver", "FreeType PCF driver", "HarfBuzz", "PlutoSVG", "PlutoVG",
    }
    for item in manifest["files"]:
        assert hashlib.sha256((directory / item["file"]).read_bytes()).hexdigest() == item["sha256"]
    assert "based in part on the work of the FreeType Team" in (
        directory / "SDL3_ttf-NOTICES.txt"
    ).read_text(encoding="utf-8")


def test_windows_build_publishes_notices_and_terminal_rebuild_removes_them(tmp_path, monkeypatch):
    source = native.ROOT
    shutil.copytree(source / "native/notices", tmp_path / "native/notices")
    fixture_manifest = tmp_path / "native/notices/manifest.json"
    inventory = json.loads(fixture_manifest.read_text(encoding="utf-8"))
    inventory["windows_dll_sha256"] = hashlib.sha256(b"SDK DLL").hexdigest()
    fixture_manifest.write_text(json.dumps(inventory), encoding="utf-8")
    vendor = tmp_path / "native/vendor"
    vendor.mkdir()
    shutil.copy2(source / "native/vendor/cJSON.LICENSE", vendor / "cJSON.LICENSE")
    monkeypatch.setattr(native, "ROOT", tmp_path)
    monkeypatch.setattr(native, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(native, "os", SimpleNamespace(name="nt", replace=os.replace))
    monkeypatch.setattr(native.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(native, "compiler_environment", lambda _compiler: ("cl", {}))

    def compile(command, **kwargs):
        executable = next(arg[4:] for arg in command if arg.startswith("/Fe:"))
        Path(executable).write_bytes(b"compiled host placeholder")

    monkeypatch.setattr(native.subprocess, "run", compile)
    arguments = []
    for option, library in zip(("--sdl-root", "--ttf-root", "--image-root"), native.LIBRARIES):
        root = tmp_path / library
        (root / "lib/x64").mkdir(parents=True)
        (root / "lib/x64" / (library + ".lib")).touch()
        (root / "lib/x64" / (library + ".dll")).write_bytes(b"SDK DLL")
        (root / "LICENSE.txt").write_text("SDK notice", encoding="utf-8")
        arguments.extend((option, str(root)))
    assert native.main(arguments) == 0
    output = tmp_path / "src/pysual/bin"
    manifest = json.loads((source / "native/notices/manifest.json").read_text(encoding="utf-8"))
    marker = json.loads((output / "native-build.json").read_text(encoding="utf-8"))
    for item in manifest["files"]:
        assert item["file"] in marker["files"]
        assert (output / item["file"]).read_bytes() == (source / "native/notices" / item["file"]).read_bytes()
    assert (output / "cJSON-LICENSE.txt").read_bytes() == (vendor / "cJSON.LICENSE").read_bytes()

    # A different custom SDK must not inherit the pinned SDK's inventory.
    (tmp_path / "SDL3_ttf/lib/x64/SDL3_ttf.dll").write_bytes(b"different SDK")
    with pytest.raises(RuntimeError, match="does not match the pinned"):
        native.main(arguments)
    assert (output / "SDL3_ttf.dll").read_bytes() == b"SDK DLL"

    # Reusing the output directory must remove the preceding full build's files.
    assert native.main(["--terminal-only"]) == 0
    assert set(path.name for path in output.iterdir()) == {
        "pysual-host.exe", "cJSON-LICENSE.txt", "native-build.json",
    }
    assert not json.loads((output / "native-build.json").read_text(encoding="utf-8"))["window"]
