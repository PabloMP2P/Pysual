"""Wheel tags track the actual presence of the standalone native host."""
from pathlib import Path
import runpy
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from types import SimpleNamespace

import pytest
import setuptools

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("has_native", [False, True])
def test_wheel_tag_and_purity_match_native_content(tmp_path, monkeypatch, has_native):
    setup = tmp_path / "setup.py"
    setup.write_text((ROOT / "setup.py").read_text(encoding="utf-8"), encoding="utf-8")
    native = tmp_path / "src/pysual/bin"
    native.mkdir(parents=True)
    # A stray DLL/license must not turn a pure wheel into a native distribution.
    (native / "SDL3.dll").write_bytes(b"stale dependency")
    if has_native:
        (native / ("pysual-host.exe" if sys.platform == "win32" else "pysual-host")).write_bytes(b"host")
    captured = {}
    monkeypatch.setattr(setuptools, "setup", lambda **kwargs: captured.update(kwargs))
    runpy.run_path(str(setup))
    if sys.platform == "darwin":
        monkeypatch.setitem(captured["cmdclass"]["bdist_wheel"].get_tag.__globals__,
                            "macos_native_platform", lambda _path: "macosx_26_0_arm64")
    distribution = captured["distclass"]({"name": "pysual", "version": "0.1.0a1"})
    wheel = captured["cmdclass"]["bdist_wheel"](distribution)
    wheel.ensure_finalized()
    assert wheel.root_is_pure is not has_native
    python, abi, platform = wheel.get_tag()
    assert python == "py3" and abi == "none"
    assert (platform != "any") is has_native


@pytest.fixture
def setup_namespace(monkeypatch):
    monkeypatch.setattr(setuptools, "setup", lambda **_kwargs: None)
    return runpy.run_path(str(ROOT / "setup.py"))


def macho_runner(architectures, commands):
    def run(arguments, **_kwargs):
        if arguments[:2] == ["lipo", "-archs"]:
            return " ".join(architectures) + "\n"
        assert arguments[0] == "otool" and arguments[1] == "-arch" and arguments[3] == "-l"
        return commands[arguments[2]]
    return run


def build_version(minimum, platform="1"):
    return f"Load command 9\n      cmd LC_BUILD_VERSION\n  cmdsize 32\n platform {platform}\n    minos {minimum}\n      sdk 26.0\n   ntools 1\n"


@pytest.mark.parametrize("architectures,commands,expected", [
    (["arm64"], {"arm64": build_version("26.0")}, "macosx_26_0_arm64"),
    (["x86_64"], {"x86_64": "Load command 8\n      cmd LC_VERSION_MIN_MACOSX\n  cmdsize 16\n  version 10.15\n      sdk 15.2\n"}, "macosx_10_15_x86_64"),
    (["arm64"], {"arm64": build_version("15.0", "MACOS")}, "macosx_15_0_arm64"),
    (["arm64", "x86_64"], {"arm64": build_version("14.0"), "x86_64": build_version("13.0")}, "macosx_14_0_universal2"),
    (["x86_64"], {"x86_64": build_version("10.13")}, "macosx_10_13_x86_64"),
])
def test_macos_tag_uses_actual_slices_and_minimums(setup_namespace, architectures, commands, expected):
    tag = setup_namespace["macos_native_platform"](Path("pysual-host"), runner=macho_runner(architectures, commands))
    assert tag == expected


@pytest.mark.parametrize("architectures,commands,error", [
    (["arm64e"], {}, "architectures"),
    ([], {}, "architectures"),
    (["arm64"], {"arm64": "Load command 0\n cmd LC_UUID\n uuid abc\n"}, "no macOS minimum"),
    (["arm64"], {"arm64": build_version("26.0", "2")}, "macOS platform"),
    (["arm64"], {"arm64": build_version("unknown")}, "minimum macOS"),
    (["arm64"], {"arm64": build_version("26.0.1")}, "cannot be represented"),
    (["arm64"], {"arm64": build_version("26.6")}, "whole-major target"),
])
def test_macos_tag_rejects_unverifiable_metadata(setup_namespace, architectures, commands, error):
    with pytest.raises(RuntimeError, match=error):
        setup_namespace["macos_native_platform"](Path("pysual-host"), runner=macho_runner(architectures, commands))


def test_macos_tag_reports_missing_inspection_tools(setup_namespace):
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("lipo")
    with pytest.raises(RuntimeError, match="Xcode command-line tools"):
        setup_namespace["macos_native_platform"](Path("pysual-host"), runner=unavailable)


def test_macos_wheel_does_not_inherit_interpreter_universal_tag(setup_namespace, monkeypatch):
    wheel_type = setup_namespace["PysualWheel"]
    namespace = wheel_type.get_tag.__globals__
    monkeypatch.setitem(namespace, "HAS_NATIVE", True)
    monkeypatch.setitem(namespace, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setitem(namespace, "macos_native_platform", lambda _path: "macosx_26_0_arm64")
    distribution = setuptools.Distribution({"name": "pysual", "version": "0.1.0a1"})
    assert wheel_type(distribution).get_tag() == ("py3", "none", "macosx_26_0_arm64")


def test_project_installs_only_library_packages():
    import tomllib
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = config["project"]
    assert project["name"] == "pysual"
    assert not project.get("dependencies")
    find = config["tool"]["setuptools"]["packages"]["find"]
    assert find["include"] == ["pysual", "pysual.*"]
    data = config["tool"]["setuptools"]["package-data"]["pysual"]
    assert "assets/*" in data and "web/notices/*.txt" in data


def test_sdist_contains_readme_theme_comparison(tmp_path):
    # Exercise the real packaging rules in an isolated minimal checkout.
    source = tmp_path / "source"
    for relative in ("setup.py", "pyproject.toml", "MANIFEST.in", "README.md", "LICENSE",
                     "src/pysual/__init__.py", "docs/theme-families.png"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    result = subprocess.run(
        [sys.executable, "setup.py", "sdist"], cwd=source,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with tarfile.open(next((source / "dist").glob("*.tar.gz"))) as archive:
        root = archive.getnames()[0].split("/")[0]
        readme = archive.extractfile(f"{root}/README.md").read()
        assert b"(https://github.com/PabloMP2P/Pysual/blob/main/docs/theme-families.png)" in readme
        image = archive.extractfile(f"{root}/docs/theme-families.png").read()
        assert image == (ROOT / "docs/theme-families.png").read_bytes()


def test_readme_file_links_are_absolute():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)]+)\)", readme)
    assert links
    assert all(link.startswith(("https://", "#")) for link in links), links


def test_repeated_wheel_drops_deleted_module_and_asset(tmp_path):
    source = tmp_path / "source"
    for relative in ("setup.py", "pyproject.toml", "MANIFEST.in", "README.md", "LICENSE"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    package = source / "src/pysual"
    (package / "assets").mkdir(parents=True)
    current = {"__init__.py": b'__version__ = "0.1.0a1"\n',
               "current.py": b'CURRENT = True\n', "py.typed": b"",
               "_factory_types.pyi": b'class Factory: ...\n',
               "assets/font.ttf": b"font fixture", "assets/LICENSE.txt": b"notice fixture"}
    for name, data in current.items():
        (package / name).write_bytes(data)
    obsolete = (package / "obsolete.py", package / "assets/obsolete.txt")
    for path in obsolete:
        path.write_text("obsolete = True\n", encoding="utf-8")
    for iteration in range(2):
        if iteration:
            for path in obsolete:
                path.unlink()
        output = tmp_path / f"wheel-{iteration}"
        result = subprocess.run(
            [sys.executable, "setup.py", "bdist_wheel", "--dist-dir", str(output)], cwd=source,
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        with zipfile.ZipFile(next(output.glob("*.whl"))) as archive:
            for name, data in current.items():
                assert archive.read(f"pysual/{name}") == data
            for path in obsolete:
                member = "pysual/" + path.relative_to(package).as_posix()
                assert (member in archive.namelist()) is (iteration == 0)
