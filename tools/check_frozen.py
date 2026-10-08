"""Build and launch self-contained window, Python-terminal and C-terminal apps."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from pysual.cli import _python_license_path, build_executable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-license", type=Path, help="Full build-interpreter license notice")
    options = parser.parse_args()
    notice = _python_license_path(options.python_license)
    notice_sha256 = hashlib.sha256(notice.read_bytes()).hexdigest()
    native_notices = {}
    if os.name == "nt":
        manifest = json.loads((ROOT / "native/notices/manifest.json").read_text(encoding="utf-8"))
        native_notices = {item["file"]: item["sha256"] for item in manifest["files"]}
    destination = ROOT / ".build/frozen-probes"
    destination.mkdir(parents=True, exist_ok=True)
    fixture = destination / "fixture"
    (fixture / "assets/nested").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "tools/frozen_probe.py", fixture / "frozen_probe.py")
    (fixture / "assets/nested/message.txt").write_text("Included directory: Zoë\n", encoding="utf-8")
    (fixture / "settings.txt").write_text("Included file\n", encoding="utf-8")
    for backend, renderer in (("window", "auto"), ("terminal", "python"), ("terminal", "c")):
        suffix = ".exe" if os.name == "nt" else ""
        executable = destination / f"{backend}-{renderer}{suffix}"
        build_executable(fixture / "frozen_probe.py", executable, backend=backend, terminal=renderer,
                         includes=("assets", "settings.txt"), python_license=notice)
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("PYSUAL_HOST", None)
        environment.pop("LD_LIBRARY_PATH", None)
        environment.pop("DYLD_LIBRARY_PATH", None)
        environment.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
        environment.update(PYSUAL_PROBE_BACKEND=backend, PYSUAL_PROBE_RENDERER=renderer,
                           PYSUAL_PROBE_PYTHON_LICENSE_SHA256=notice_sha256,
                           PYSUAL_PROBE_NATIVE_NOTICES=json.dumps({} if renderer == "python" else native_notices))
        if renderer == "python":
            environment["PYSUAL_HOST"] = str(destination / "missing-native-helper")
        # A separate directory prevents accidental asset access relative to the
        # source checkout, executable, or build working directory.
        with tempfile.TemporaryDirectory(prefix="pysual-frozen-probe-") as temporary:
            evidence = destination / f"{backend}-{renderer}-run"
            evidence.mkdir(exist_ok=True)
            try:
                with (evidence / "process.log").open("w", encoding="utf-8") as log:
                    completed = subprocess.run([str(executable)], cwd=temporary, env=environment,
                                               stdout=log, stderr=subprocess.STDOUT, check=False, timeout=60)
                result_file = Path(temporary) / "result.json"
                result = json.loads(result_file.read_text(encoding="utf-8")) if result_file.is_file() else {"error": "Probe did not write a result"}
                if completed.returncode or not result.get("success"):
                    raise RuntimeError(result)
            finally:
                for path in Path(temporary).iterdir():
                    if path.is_file():
                        shutil.copy2(path, evidence / path.name)
        print(f"Frozen {backend}/{renderer}: read included assets, launched, painted, updated and closed", flush=True)


if __name__ == "__main__":
    main()
