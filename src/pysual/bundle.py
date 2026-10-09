"""Build one offline HTML application containing Python, Pysual and Pyodide."""

from __future__ import annotations

import base64
import ast
import hashlib
import html
import io
from importlib.metadata import distribution, PackageNotFoundError
import json
import os
from pathlib import Path
import tarfile
import tempfile
from urllib.request import urlopen
import zipfile

from ._build_assets import include_files

PYODIDE_VERSION = "0.29.0"
PYODIDE_URL = f"https://registry.npmjs.org/pyodide/-/pyodide-{PYODIDE_VERSION}.tgz"
PYODIDE_SHA512 = "ObIvsTmcrxAWKg+FT1GjfSdDmQc5CabnYe/nn5BCuhr9BVVITeQ24DBdZuG5B2tIiAZ9YonBpnDB7cmHZyd2Rw=="
RUNTIME_FILES = ("pyodide.mjs", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip",
                 "pyodide-lock.json", "package.json")


def _validate_runtime(runtime: Path) -> Path:
    for name in RUNTIME_FILES:
        if not (runtime / name).is_file():
            raise ValueError(f"Incomplete Pyodide runtime: missing {runtime / name}")
    metadata = json.loads((runtime / "package.json").read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("version") != PYODIDE_VERSION:
        raise ValueError(f"Web builds require the Pyodide {PYODIDE_VERSION} npm runtime")
    return runtime


def runtime_cache() -> Path:
    """Use the user's cache, keeping downloaded runtimes outside source trees."""
    if os.name == "nt":
        cache = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache / "pysual" / f"pyodide-{PYODIDE_VERSION}"


def fetch_runtime(destination: Path | None = None) -> Path:
    """Fetch the pinned official distribution once; verify before writing files."""
    destination = Path(destination) if destination is not None else runtime_cache()
    try:
        return _validate_runtime(destination)
    except (ValueError, OSError):
        pass
    with urlopen(PYODIDE_URL, timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("Pyodide download exceeds the expected distribution size")
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if actual != PYODIDE_SHA512:
        raise ValueError("Pyodide checksum mismatch; downloaded runtime was not installed")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for name in RUNTIME_FILES:
            member = archive.getmember("package/" + name)
            if not member.isfile():
                raise ValueError(f"Runtime member must be a regular file: {name}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Missing runtime member: {name}")
            _atomic_write(destination / name, stream.read())
    return _validate_runtime(destination)


def _atomic_write(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pysual-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _zip_file(archive, name: str, data: bytes):
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compresslevel=9)


def build_web(script: Path, output: Path, runtime: Path | None = None, *, includes=()) -> Path:
    """Embed the entry script and optional relative files into a self-contained HTML.

    A supplied runtime directory avoids all build-time network access. Without
    one, the exact pinned runtime is downloaded to the user's cache on first use.
    Application code runs unchanged; Python event handlers stay Python.
    """
    script, output = Path(script).resolve(), Path(output).resolve()
    if not script.is_file() or script.suffix != ".py":
        raise ValueError("The web entry point must be an existing .py file")
    ast.parse(script.read_text(encoding="utf-8-sig"), filename=str(script))
    if output.suffix.lower() != ".html" or output == script:
        raise ValueError("Web output must be a separate .html file")
    runtime = _validate_runtime(Path(runtime).resolve()) if runtime is not None else fetch_runtime()
    if output.is_relative_to(runtime):
        raise ValueError("Web output must be outside the runtime directory")
    package = Path(__file__).parent
    entries = {"app.py": script.read_bytes()}
    for item in includes:
        _, paths = include_files(script.parent, item, output)
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            name = path.relative_to(script.parent).as_posix()
            if name in entries or name.split("/")[0] == "pysual" or path.resolve() == output:
                raise ValueError(f"Included path conflicts with the bundle: {name}")
            entries[name] = path.read_bytes()
    for path in sorted(package.rglob("*")):
        if path.is_file() and (path.suffix == ".py" or path.name == "py.typed"
                               or "assets" in path.relative_to(package).parts
                               or "notices" in path.relative_to(package).parts):
            entries["pysual/" + path.relative_to(package).as_posix()] = path.read_bytes()
    license_path = package.parents[1] / "LICENSE"
    if license_path.is_file():
        entries["PYSUAL-LICENSE.txt"] = license_path.read_bytes()
    else:
        try:
            installed = distribution("pysual")
            for path in installed.files or ():
                if str(path).endswith(".dist-info/licenses/LICENSE"):
                    entries["PYSUAL-LICENSE.txt"] = installed.locate_file(path).read_bytes()
                    break
        except PackageNotFoundError:
            pass
    archive_bytes = io.BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        for name, data in sorted(entries.items()):
            _zip_file(archive, name, data)
    files = {"pysual.zip": archive_bytes.getvalue(), "app.py": script.read_bytes()}
    for name in ("host.js", "svg.js", "input.js", "services.js"):
        files[name] = (package / "web" / name).read_bytes()
    for name in RUNTIME_FILES:
        files["pyodide/" + name] = (runtime / name).read_bytes()
    bundle = json.dumps({"schema": "pysual/1", "files": {
        name: base64.b64encode(data).decode("ascii") for name, data in sorted(files.items())
    }}, separators=(",", ":")).replace("<", "\\u003c")
    bootstrap = (package / "web" / "standalone.js").read_text(encoding="utf-8")
    notice_names = ("WEB-NOTICES.txt", "Pyodide-LICENSE.txt", "CPython-LICENSE.txt")
    notices = "\n\n".join((package / "web" / "notices" / name).read_text(encoding="utf-8") for name in notice_names)
    notices += "\n\n" + (package / "assets" / "DejaVu-LICENSE.txt").read_text(encoding="utf-8")
    notices += "\n\n" + (package / "assets" / "UNICODE-LICENSE.txt").read_text(encoding="utf-8")
    if "PYSUAL-LICENSE.txt" in entries:
        notices += "\n\n" + entries["PYSUAL-LICENSE.txt"].decode("utf-8")
    notices = notices.replace("</script", "<\\/script")
    page = (
        '<!doctype html>\n<html lang="en"><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        '<title>' + html.escape(script.stem) + '</title>\n'
        '<style>html,body{margin:0;height:100%;overflow:hidden;background:#111827;color:#e5e7eb;'
        'font:15px system-ui}#app{display:block;width:100%;height:100%;outline:none;touch-action:none;user-select:none}'
        '#status{position:absolute;inset:24px;white-space:pre-wrap;pointer-events:none}</style>\n'
        '<svg id="app" xmlns="http://www.w3.org/2000/svg" tabindex="0" role="application" aria-label="Pysual application"></svg>'
        '<div id="status" role="status">Loading Python…</div>\n'
        '<script id="pysual-notices" type="text/plain">\n' + notices + '\n</script>\n'
        '<script id="pysual-bundle" type="application/json">' + bundle + '</script>\n'
        '<script type="module">\n' + bootstrap + '\n</script></html>\n'
    )
    _atomic_write(output, page.encode("utf-8"))
    return output
