"""Build or run one app for a window, terminal, or browser.

    python -m pysual build examples/hello.py --backend terminal
    python -m pysual build examples/hello.py --backend web
    python -m pysual run examples/hello.py --backend window
    python -m pysual run app.py --backend terminal -- --app-option value
    python -m pysual native          # needs the repository checkout
    python -m pysual package         # needs the repository checkout

``pysual`` is installed as the same command; ``build`` is implied for a script path.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile

from ._build_assets import include_files

PACKAGE = Path(__file__).resolve().parent
# The directory that contains the ``pysual`` package: ``src`` in a checkout.
SOURCE = PACKAGE.parent


def _find_root() -> Path | None:
    """The repository root, when the C sources and build tools are present."""
    candidate = SOURCE.parent
    if (candidate / "tools" / "native.py").is_file() and (candidate / "native" / "host.c").is_file():
        return candidate
    return None


ROOT = _find_root()
_CHECKOUT_REQUIRED = (
    "needs the repository checkout (tools/native.py and native/*.c). "
    "Clone https://github.com/PabloMP2P/Pysual and run the command there, "
    "or install a wheel that already contains the native host."
)


def _native_available(*, window=False):
    executable = SOURCE / "pysual" / "bin" / ("pysual-host.exe" if os.name == "nt" else "pysual-host")
    if not executable.is_file():
        return None
    marker = executable.parent / "native-build.json"
    if window and marker.is_file():
        try:
            if not json.loads(marker.read_text(encoding="utf-8")).get("window", True):
                return None
        except (OSError, ValueError):
            return None
    return executable


def _license_path() -> Path | None:
    """The MIT notice: beside the checkout, or inside the installed metadata."""
    if ROOT is not None and (ROOT / "LICENSE").is_file():
        return ROOT / "LICENSE"
    try:
        from importlib.metadata import PackageNotFoundError, distribution

        installed = distribution("pysual")
    except (ImportError, PackageNotFoundError):
        return None
    for path in installed.files or ():
        if str(path).endswith(".dist-info/licenses/LICENSE"):
            located = Path(str(installed.locate_file(path)))
            return located if located.is_file() else None
    return None


def _python_license_path(override=None) -> Path:
    """Locate the full notice shipped with the interpreter used by PyInstaller."""
    prefix = Path(sys.base_prefix)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = [Path(override).expanduser().resolve()] if override is not None else [
        prefix / "LICENSE.txt", prefix / "LICENSE",
        prefix / "lib" / version / "LICENSE.txt",
    ]
    for path in candidates:
        if path.is_file():
            text = path.read_text(encoding="utf-8-sig")
            if "PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2" in text or "PSF LICENSE AGREEMENT FOR PYTHON" in text:
                return path
    raise RuntimeError(
        "Cannot find the build interpreter's full CPython license notice. "
        "Use --python-license FILE (python_license= for build_executable) with "
        "the complete license supplied by this Python installation."
    )


def build_executable(script, output, *, backend="window", terminal="auto", includes=(), packages=(),
                     python_license=None):
    """Freeze an application using the current platform's Python and PyInstaller.

    Build failures leave an existing output intact. Extra assets are relative to
    the entry script, and retain that relative path inside the executable.
    """
    script, output = Path(script).resolve(), Path(output).resolve()
    if backend not in ("window", "terminal"):
        raise ValueError("Executable backend must be window or terminal")
    if terminal not in ("auto", "c", "python"):
        raise ValueError("Terminal renderer must be auto, c, or python")
    if not script.is_file() or script.suffix != ".py":
        raise FileNotFoundError(script)
    if output == script:
        raise ValueError("Output cannot replace the entry script")
    ast.parse(script.read_text(encoding="utf-8-sig"), filename=str(script))
    if importlib.util.find_spec("PyInstaller") is None:
        raise RuntimeError("Executable builds need PyInstaller. Install with: python -m pip install 'pysual[build]'")
    assets = []
    for include in includes:
        relative = Path(include)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("--include paths must be relative to the entry script, without '..'")
        source, _ = include_files(script.parent, relative, output)
        assets.append((source, str(relative if source.is_dir() else relative.parent)))
    for package in packages:
        if not package or any(not piece.isidentifier() for piece in package.split(".")):
            raise ValueError(f"Invalid package name: {package!r}")
    interpreter_notice = _python_license_path(python_license)
    # Auto terminal builds have no compiler requirement. Include an already
    # built host when available; otherwise the bundled Python renderer is used.
    executable = None if terminal == "python" and backend == "terminal" else _native_available(window=backend == "window")
    if executable is None and (backend == "window" or terminal == "c"):
        if ROOT is None:
            raise RuntimeError(f"Building the native host for a {backend} executable {_CHECKOUT_REQUIRED}")
        command = [sys.executable, str(ROOT / "tools" / "native.py")]
        if backend == "terminal":
            command.append("--terminal-only")
        subprocess.run(command, check=True, cwd=ROOT)
        executable = _native_available(window=backend == "window")
        if executable is None:
            raise RuntimeError("The native build completed without producing the requested renderer")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pysual-build-", dir=output.parent) as temporary:
        stage = Path(temporary)
        hook = stage / "select_backend.py"
        hook.write_text("import os\nimport atexit\nfrom contextlib import ExitStack\n"
                        "import pysual.backends as _backends\n"
                        "from pysual.backends.terminal import terminal_renderer_override\n"
                        f"os.environ['PYSUAL_TERMINAL'] = {terminal!r}\n"
                        "_backends._build_context = ExitStack()\n"
                        f"_backends._build_context.enter_context(_backends.backend_override({backend!r}))\n"
                        f"_backends._build_context.enter_context(terminal_renderer_override({terminal!r}))\n"
                        "atexit.register(_backends._build_context.close)\n", encoding="utf-8")
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
                   "--name", "pysual_app", "--distpath", str(stage / "dist"),
                   "--workpath", str(stage / "work"), "--specpath", str(stage),
                   "--paths", str(SOURCE), "--paths", str(script.parent),
                   "--runtime-hook", str(hook), "--collect-submodules", "pysual",
                   "--add-data", f"{SOURCE / 'pysual' / 'assets'}{os.pathsep}pysual/assets"]
        notice = stage / "CPython-LICENSE.txt"
        shutil.copyfile(interpreter_notice, notice)
        command += ["--add-data", f"{notice}{os.pathsep}pysual/licenses"]
        license_path = _license_path()
        if license_path is not None:
            command += ["--add-data", f"{license_path}{os.pathsep}pysual/licenses"]
        if executable is not None:
            # Binary classification is essential: PyInstaller must discover
            # the host's linked SDL dependencies, including system-installed
            # .so/.dylib libraries on Linux and macOS.
            for native_file in sorted(executable.parent.iterdir()):
                if not native_file.is_file():
                    continue
                name = native_file.name.lower()
                binary = native_file == executable or name.endswith((".dll", ".so", ".dylib")) or ".so." in name
                option = "--add-binary" if binary else "--add-data"
                command += [option, f"{native_file}{os.pathsep}pysual/bin"]
        if backend == "window" and sys.platform in ("win32", "darwin"):
            command.append("--windowed")
        else:
            command.append("--console")
        for source, destination in assets:
            command += ["--add-data", f"{source}{os.pathsep}{destination}"]
        for package in packages:
            command += ["--collect-all", package]
        command.append(str(script))
        environment = os.environ.copy()
        # Give isolated PyInstaller workers the same package as the entry script.
        environment["PYTHONPATH"] = str(SOURCE) + os.pathsep + environment.get("PYTHONPATH", "")
        environment["PYINSTALLER_CONFIG_DIR"] = str(stage / "cache")
        subprocess.run(command, check=True, cwd=ROOT or stage, env=environment)
        artifact = stage / "dist" / ("pysual_app.exe" if os.name == "nt" else "pysual_app")
        if not artifact.is_file():
            raise RuntimeError("PyInstaller finished without producing the executable")
        # os.replace publishes only a complete file and retains executable bits.
        os.replace(artifact, output)
    return output


def _run_script(script: Path, backend: str, terminal: str, arguments=()) -> int:
    environment = os.environ.copy()
    environment["PYSUAL_TERMINAL"] = terminal
    environment["PYTHONPATH"] = str(SOURCE) + os.pathsep + environment.get("PYTHONPATH", "")
    launcher = ("import runpy,sys\nfrom pathlib import Path\nfrom pysual.backends import backend_override\n"
                "from pysual.backends.terminal import terminal_renderer_override\n"
                "script,target,renderer,*arguments = sys.argv[1:]\nsys.argv = [script, *arguments]\nsys.path.insert(0,str(Path(script).parent))\n"
                "with backend_override(target), terminal_renderer_override(renderer):\n"
                "    runpy.run_path(script, run_name='__main__')\n")
    return subprocess.call([sys.executable, "-c", launcher, str(script.resolve()), backend, terminal, *arguments],
                           env=environment)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    program = "pysual"
    if argv and argv[0] == "native":
        if ROOT is None:
            print(f"{program}: the native build {_CHECKOUT_REQUIRED}", file=sys.stderr)
            return 1
        return subprocess.call([sys.executable, str(ROOT / "tools" / "native.py"), *argv[1:]], cwd=ROOT)
    if argv and argv[0] == "package":
        if ROOT is None:
            print(f"{program}: packaging {_CHECKOUT_REQUIRED}", file=sys.stderr)
            return 1
        packages = argparse.ArgumentParser(prog=f"{program} package",
                                           description="Build the library wheel and source distribution")
        packages.add_argument("--wheel", action="store_true")
        packages.add_argument("--sdist", action="store_true")
        options = packages.parse_args(argv[1:])
        command = [sys.executable, "-m", "build", str(ROOT), "--outdir", str(ROOT / "dist")]
        if options.wheel or not options.sdist:
            command.append("--wheel")
        if options.sdist or not options.wheel:
            command.append("--sdist")
        return subprocess.call(command, cwd=ROOT)
    running = bool(argv and argv[0] == "run")
    if running or (argv and argv[0] == "build"):
        argv.pop(0)
    arguments = []
    if running and "--" in argv:
        separator = argv.index("--")
        argv, arguments = argv[:separator], argv[separator + 1:]
    parser = argparse.ArgumentParser(
        prog=program, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("script", type=Path, help="Self-contained Python entry script")
    parser.add_argument("--backend", choices=("window", "terminal", "web"), default="window")
    parser.add_argument("--terminal", choices=("auto", "c", "python"), default="auto",
                        help="Terminal renderer; auto works without a native build (default)")
    parser.add_argument("--output", "-o", type=Path, help="Destination executable or HTML file (default: dist/<script>)")
    parser.add_argument("--python-license", type=Path, help="Full build-interpreter license notice (executable builds)")
    parser.add_argument("--runtime", type=Path, help="Local Pyodide distribution for an offline web build")
    parser.add_argument("--include", action="append", default=[], help="Asset path relative to the entry script (repeatable)")
    parser.add_argument("--package", action="append", default=[], help="Collect an extra installed package (executable builds)")
    options = parser.parse_args(argv)
    if running:
        if options.output or options.runtime or options.include or options.package or options.python_license:
            parser.error("run accepts a script, --backend, --terminal, and application arguments after --")
        return _run_script(options.script, options.backend, options.terminal, arguments)
    suffix = ".html" if options.backend == "web" else ".exe" if os.name == "nt" else ""
    output = options.output or (ROOT or Path.cwd()) / "dist" / (options.script.stem + suffix)
    try:
        if options.backend == "web":
            if options.python_license is not None:
                raise ValueError("--python-license applies only to executable builds")
            if options.package:
                raise ValueError("--package applies to executable builds; include local pure-Python modules with --include")
            from .bundle import build_web

            result = build_web(options.script, output, options.runtime, includes=options.include)
        else:
            if options.runtime is not None:
                raise ValueError("--runtime applies only to web builds")
            result = build_executable(options.script, output, backend=options.backend, terminal=options.terminal,
                                      includes=options.include, packages=options.package, python_license=options.python_license)
    except (OSError, SyntaxError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Build failed: {error}\n")
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
