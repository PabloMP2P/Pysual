"""Check real consumers against checkout types, including expected rejections.

Run after installing the dev extra: python tools/check_typing.py
This checks the public authoring API, not whole-package type completeness.
"""

from collections import Counter
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if importlib.util.find_spec("pyright") is None:
        raise SystemExit("Install the dev extra to run the Pyright consumer checks")
    fixtures = sorted((ROOT / "tests/typing").glob("*.py"))
    expected = Counter()
    for fixture in fixtures:
        for line, text in enumerate(fixture.read_text(encoding="utf-8").splitlines()):
            marker = re.search(r"# error: (\w+)\s*$", text)
            if marker:
                expected[(fixture.name, line, marker[1])] += 1
    if not fixtures or not expected:
        raise SystemExit("Typing fixtures must include valid cases and marked rejections")
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--project", str(ROOT / "tests/typing"),
         "--pythonpath", sys.executable, "--outputjson"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(result.stdout + result.stderr)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise SystemExit(result.stdout + result.stderr) from None
    if report["summary"]["filesAnalyzed"] != len(fixtures):
        raise SystemExit("Pyright did not analyze every consumer fixture\n" + result.stdout + result.stderr)
    diagnostics = [item for item in report["generalDiagnostics"]
                   if item["severity"] != "information"]
    actual = Counter((Path(item["file"]).name, item["range"]["start"]["line"],
                      item.get("rule", "")) for item in diagnostics)
    if actual != expected:
        print(json.dumps(diagnostics, indent=2))
        raise SystemExit(f"Missing rejections: {expected - actual}\nUnexpected diagnostics: {actual - expected}")
    print(f"Pyright {report['version']}: valid consumers passed; {sum(expected.values())} expected errors verified")


if __name__ == "__main__":
    main()
