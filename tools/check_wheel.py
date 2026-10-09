"""Install a built wheel into a fresh venv and exercise it outside the checkout.

python tools/check_wheel.py --kind pure
python tools/check_wheel.py --kind native

Native Linux/macOS wheels need the documented system SDL libraries. Their loader
paths are preserved; source imports and native-helper overrides are removed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import venv

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("pure", "native"), required=True)
    parser.add_argument("--wheel", type=Path, help="Exact wheel; defaults to the matching wheel in dist")
    options = parser.parse_args()
    wheels = [options.wheel.resolve()] if options.wheel else [
        path for path in (ROOT / "dist").glob("*.whl")
        if path.name.endswith("-none-any.whl") == (options.kind == "pure")]
    if len(wheels) != 1 or not wheels[0].is_file():
        parser.error(f"Expected exactly one {options.kind} wheel, found {wheels}; use --wheel")
    evidence = ROOT / ".build/wheel-probes" / options.kind
    evidence.mkdir(parents=True, exist_ok=True)
    result_path = evidence / "result.json"
    result_path.unlink(missing_ok=True)  # A failed rerun must not retain success.
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYSUAL_HOST", "PYSUAL_TERMINAL"):
        environment.pop(name, None)
    notices = {}
    if options.kind == "native" and os.name == "nt":
        manifest = json.loads((ROOT / "native/notices/manifest.json").read_text(encoding="utf-8"))
        notices = {item["file"]: item["sha256"] for item in manifest["files"]}
    environment["PYSUAL_PROBE_NATIVE_NOTICES"] = json.dumps(notices)
    with tempfile.TemporaryDirectory(prefix="pysual-installed-wheel-") as temporary:
        directory = Path(temporary)
        venv.EnvBuilder(with_pip=True).create(directory / "venv")
        python = directory / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        probe = directory / "wheel_probe.py"
        shutil.copy2(ROOT / "tools/wheel_probe.py", probe)
        with (evidence / "process.log").open("w", encoding="utf-8") as log:
            log.write(f"Wheel: {wheels[0]}\n")
            log.flush()
            subprocess.run([str(python), "-I", "-m", "pip", "install", "--no-index", "--no-deps", str(wheels[0])],
                           cwd=directory, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=120)
            try:
                subprocess.run([str(python), "-I", str(probe), options.kind], cwd=directory, env=environment,
                               stdout=log, stderr=subprocess.STDOUT, check=True, timeout=60)
            finally:
                if (directory / "frame.bmp").is_file():
                    shutil.copy2(directory / "frame.bmp", evidence / "frame.bmp")
        print((evidence / "process.log").read_text(encoding="utf-8"), end="")
    distributions = [wheels[0]]
    sdist = wheels[0].with_name("-".join(wheels[0].name.split("-")[:2]) + ".tar.gz")
    if options.kind == "pure" and sdist.is_file():
        distributions.append(sdist)
    result_path.write_text(json.dumps({
        "success": True, "kind": options.kind,
        "commit": os.environ.get("GITHUB_SHA"),
        "python": platform.python_version(), "platform": platform.platform(),
        "wheel_tested": wheels[0].name,
        "distributions": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in distributions},
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
