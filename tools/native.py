"""Build the standalone C host, optionally with only its terminal renderer.

Windows SDL dependencies are downloaded from checksum-pinned official releases.
Linux/macOS use installed SDL3, SDL3_ttf, SDL3_image and pkg-config.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
LIBRARIES = ("SDL3", "SDL3_ttf", "SDL3_image")


def macos_deployment_environment(environment):
    """Use an explicit, wheel-representable compiler deployment target."""
    environment = dict(environment)
    target = environment.get("MACOSX_DEPLOYMENT_TARGET")
    if not target:
        target = {"arm64": "11.0", "x86_64": "10.15"}.get(platform.machine().lower())
        if target is None:
            raise RuntimeError("Unknown macOS architecture; set MACOSX_DEPLOYMENT_TARGET explicitly")
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", target)
    if match is None:
        raise RuntimeError("MACOSX_DEPLOYMENT_TARGET must be a major.minor macOS version")
    major, minor, patch = (int(part or 0) for part in match.groups())
    if major < 10 or patch or (major >= 11 and minor):
        raise RuntimeError(
            f"MACOSX_DEPLOYMENT_TARGET={target} cannot be represented accurately by a wheel tag. "
            "For macOS 11 and newer, use a whole-major target such as 11.0 or 26.0; "
            "patch-level deployment targets are unsupported."
        )
    environment["MACOSX_DEPLOYMENT_TARGET"] = target
    return environment


def _gcc():
    candidates = [shutil.which("gcc")]
    if os.name == "nt":
        for root in (Path(os.environ.get("SystemDrive", "C:")) / "msys64", Path.home() / "msys64"):
            candidates.extend(str(root / environment / "bin/gcc.exe") for environment in ("ucrt64", "mingw64"))
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        target = subprocess.check_output([candidate, "-dumpmachine"], text=True).strip()
        if os.name != "nt" or "x86_64" in target and "mingw" in target:
            return candidate
    return None


def compiler_environment(compiler: str | None = None) -> tuple[str, dict[str, str]]:
    """Locate a compiler and initialize MSVC even from an ordinary shell."""
    env = dict(os.environ)
    if not compiler:
        if os.name == "nt":
            try:
                return compiler_environment("cl")
            except (OSError, RuntimeError, subprocess.SubprocessError):
                compiler = _gcc()
        else:
            compiler = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not compiler:
        raise RuntimeError("Install Visual Studio C++ Build Tools or a C11 compiler; use --compiler with its path")
    if Path(compiler).stem.lower() != "cl":
        located = (_gcc() if compiler == "gcc" else shutil.which(compiler))
        if not located:
            raise RuntimeError(f"C compiler not found: {compiler}")
        env["PATH"] = str(Path(located).resolve().parent) + os.pathsep + env.get("PATH", "")
        return located, env
    if os.name != "nt":
        raise RuntimeError("MSVC is only supported on Windows")
    env = {key.upper(): value for key, value in env.items()}
    located = shutil.which(compiler, path=env.get("PATH", ""))
    if located and env.get("INCLUDE") and env.get("LIB"):
        return located, env
    vswhere = Path(env.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    if not vswhere.is_file():
        raise RuntimeError("Install Visual Studio C++ Build Tools, or use --compiler gcc for MinGW-w64")
    install = subprocess.check_output([str(vswhere), "-latest", "-products", "*", "-requires",
                                      "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"],
                                     text=True, encoding="utf-8").strip()
    vcvars = Path(install) / "VC/Auxiliary/Build/vcvars64.bat"
    if not install or not vcvars.is_file():
        raise RuntimeError("Visual Studio x64 C++ toolset was not found")
    command = f'"{env.get("COMSPEC", "cmd.exe")}" /d /u /s /c ""{vcvars}" >nul && set"'
    raw = subprocess.check_output(command, timeout=120, stdin=subprocess.DEVNULL, env=env)
    for line in raw.decode("utf-16le").splitlines():
        key, separator, value = line.partition("=")
        if separator and key:
            env[key.upper()] = value
    located = shutil.which("cl", path=env.get("PATH", ""))
    if not located:
        raise RuntimeError("Visual Studio did not provide cl.exe")
    return located, env


def pkg_config_flags():
    """Accept the names provided by both SDL CMake installs and OS packages."""
    packages = []
    for alternatives in (("sdl3",), ("SDL3_ttf", "sdl3-ttf"), ("SDL3_image", "sdl3-image")):
        found = next((name for name in alternatives
                      if subprocess.run(["pkg-config", "--exists", name], check=False).returncode == 0), None)
        if found is None:
            raise RuntimeError("Install SDL3, SDL3_ttf, SDL3_image development packages and pkg-config, or use --terminal-only")
        packages.append(found)
    return shlex.split(subprocess.check_output(["pkg-config", "--cflags", "--libs", *packages], text=True))


def _copy_gnu_runtime(compiler, executable):
    """Carry direct MinGW runtime dependencies into the native package directory."""
    directory = Path(compiler).resolve().parent
    objdump = directory / "objdump.exe"
    if not objdump.is_file():
        return
    output = subprocess.check_output([str(objdump), "-p", str(executable)], text=True, errors="replace")
    for line in output.splitlines():
        if "DLL Name:" in line:
            name = line.split("DLL Name:", 1)[1].strip()
            source = directory / name
            if source.is_file() and not (executable.parent / name).exists():
                shutil.copy2(source, executable.parent / name)


def _copy_windows_ttf_notices(stage, ttf_root):
    """Carry the pinned SDK's statically linked text-engine notices with its DLL."""
    directory = ROOT / "native/notices"
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    dll = ttf_root / "lib/x64/SDL3_ttf.dll"
    if hashlib.sha256(dll.read_bytes()).hexdigest() != manifest["windows_dll_sha256"]:
        raise RuntimeError(
            f"SDL3_ttf.dll does not match the pinned {manifest['version']} notice inventory. "
            "Use the pinned Windows SDK, or update native/notices for the custom SDK's dependencies."
        )
    for notice in manifest["files"]:
        source = directory / notice["file"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != notice["sha256"]:
            raise RuntimeError(f"Native dependency notice checksum mismatch: {source}")
        shutil.copy2(source, stage / source.name)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "src/pysual/bin", help="Native package directory")
    parser.add_argument("--compiler", help="C compiler name or absolute path; defaults to MSVC/MinGW on Windows, CC/cc elsewhere")
    parser.add_argument("--terminal-only", action="store_true", help="Build the C terminal renderer without SDL dependencies")
    parser.add_argument("--sdl-root", type=Path)
    parser.add_argument("--ttf-root", type=Path)
    parser.add_argument("--image-root", type=Path)
    args = parser.parse_args(argv)
    roots = [item.resolve() for item in (args.sdl_root, args.ttf_root, args.image_root) if item]
    if roots and len(roots) != 3:
        parser.error("Supply --sdl-root, --ttf-root and --image-root together")
    if roots and (args.terminal_only or os.name != "nt"):
        parser.error("Explicit development package roots apply only to full Windows builds")
    if os.name == "nt" and platform.machine().lower() not in ("amd64", "x86_64"):
        raise RuntimeError("The automatic Windows dependency packages target x64; use an x64 Python toolchain")
    compiler, env = compiler_environment(args.compiler)
    if sys.platform == "darwin":
        env = macos_deployment_environment(env)
    msvc = Path(compiler).stem.lower() == "cl"
    includes = [ROOT / "native", ROOT / "native/vendor"]
    sources = [ROOT / "native/host.c", ROOT / "native/terminal_renderer.c", ROOT / "native/vendor/cJSON.c",
               ROOT / "native" / ("window_stub.c" if args.terminal_only else "sdl_renderer.c")]
    definitions = ["CJSON_NESTING_LIMIT=64", "CJSON_HIDE_SYMBOLS"]
    libraries = []
    if not args.terminal_only:
        definitions += ["PYSUAL_WITH_WINDOW", "PT_WITH_SDL_IMAGE"]
        if os.name == "nt":
            if not roots:
                try:
                    from tools.fetch_native_deps import fetch
                except ModuleNotFoundError:
                    from fetch_native_deps import fetch
                roots = fetch("windows", ROOT / ".build/deps")
            includes += [root / "include" for root in roots]
            for root, name in zip(roots, LIBRARIES):
                suffix = ".lib" if msvc else ".dll"
                library = root / "lib/x64" / (name + suffix)
                if not library.is_file():
                    raise RuntimeError(f"Missing development library: {library}")
                libraries.append(str(library))
        else:
            libraries = pkg_config_flags()
    if os.name == "nt":
        libraries.append("Ws2_32.lib" if msvc else "-lws2_32")
    else:
        libraries += ["-lm", "-pthread"]
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / ".build/native"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="compile-", dir=scratch) as temporary:
        stage = Path(temporary)
        executable = stage / ("pysual-host.exe" if os.name == "nt" else "pysual-host")
        if msvc:
            command = [compiler, "/nologo", "/std:c11", "/O2", "/W4", "/utf-8", "/MD",
                       *[f"/D{d}" for d in definitions], *[f"/I{p}" for p in includes],
                       *map(str, sources), f"/Fe:{executable}", "/link", *libraries]
        else:
            command = [compiler, "-std=c11", "-O2", "-Wall", "-Wextra",
                       *[f"-D{d}" for d in definitions], *[f"-I{p}" for p in includes],
                       *map(str, sources), "-o", str(executable), *libraries]
            if os.name == "nt":
                command.append("-static-libgcc")
        subprocess.run(command, cwd=stage, env=env, check=True)
        for root in roots:
            for dll in (root / "lib/x64").glob("*.dll"):
                shutil.copy2(dll, stage / dll.name)
            for notice in root.glob("*LICENSE*"):
                if notice.is_file():
                    shutil.copy2(notice, stage / f"{root.name}-{notice.name}")
        if os.name == "nt" and not args.terminal_only:
            _copy_windows_ttf_notices(stage, roots[1])
        if os.name == "nt" and not msvc:
            _copy_gnu_runtime(compiler, executable)
        shutil.copy2(ROOT / "native/vendor/cJSON.LICENSE", stage / "cJSON-LICENSE.txt")
        marker = stage / "native-build.json"
        artifacts = [path for path in stage.iterdir()
                     if path.suffix.lower() not in (".obj", ".o", ".lib", ".exp")]
        previous = output / marker.name
        previous_files = []
        if previous.is_file():
            try:
                previous_files = json.loads(previous.read_text(encoding="utf-8")).get("files", [])
            except (ValueError, OSError):
                pass
        marker.write_text(json.dumps({"window": not args.terminal_only, "terminal": True,
                                      "platform": sys.platform, "machine": platform.machine(),
                                      "files": sorted(path.name for path in artifacts)}, indent=2) + "\n", encoding="utf-8")
        # Publish assets first, then the host and capability record. A compiler
        # failure leaves every previously installed build untouched.
        for path in artifacts:
            if path == executable:
                continue
            os.replace(path, output / path.name)
        os.replace(executable, output / executable.name)
        os.replace(marker, output / marker.name)
        current_files = {path.name for path in artifacts}
        for name in previous_files:
            if isinstance(name, str) and Path(name).name == name and name not in current_files:
                old_file = (output / name).resolve()
                if old_file.is_relative_to(output) and old_file.is_file():
                    old_file.unlink()
    print(output / executable.name)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Native build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
