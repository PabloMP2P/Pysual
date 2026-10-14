"""Check the library, the examples, and public consumers with Pyright.

Run after installing the dev extra: python tools/check_typing.py
The package and examples must typecheck cleanly. Consumer fixtures must pass,
and every marked invalid expression must be rejected.
"""

from collections import Counter
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Strict mode enables these. The library config must not turn them off; a module
# that is not annotated yet opts out in a file comment instead.
_UNKNOWN_RULES = (
    "reportUnknownParameterType",
    "reportUnknownArgumentType",
    "reportUnknownLambdaType",
    "reportUnknownVariableType",
    "reportUnknownMemberType",
    "reportMissingParameterType",
)
_OPT_OUT = (
    "# pyright: "
    + ", ".join(f"{rule}=false" for rule in _UNKNOWN_RULES)
)
# These stay off in the project config. Fully strict modules opt in explicitly.
# Private helpers cross modules, and host/schema code keeps runtime isinstance checks.
_EXTRA_RULES = (
    "reportUnusedCallResult",
    "reportImplicitOverride",
    "reportPrivateUsage",
    "reportUnnecessaryIsInstance",
    "reportUnnecessaryComparison",
    "reportUnnecessaryCast",
    "reportUnusedFunction",
)
_EXEMPT = ()
_STRICT_MODULES = (
    "src/pysual/_engine.py",
    "src/pysual/geometry.py",
)


def _library_sources() -> tuple[str, ...]:
    root = ROOT / "src" / "pysual"
    return tuple(sorted(
        path.relative_to(ROOT).as_posix()
        for path in root.rglob("*.py")
        if "__pycache__" not in path.parts
    ))


def _directives(relative: str) -> str:
    lines = (ROOT / relative).read_text(encoding="utf-8").splitlines()[:12]
    return "\n".join(line for line in lines if line.startswith("# pyright:"))


def _require_classification() -> None:
    """Unknown-type checking is the default. Exemptions must be listed and commented."""
    config = json.loads((ROOT / "pyrightconfig.json").read_text(encoding="utf-8"))
    if config.get("typeCheckingMode") != "strict":
        raise SystemExit("Library Pyright config must use strict mode")
    locked = _UNKNOWN_RULES + ("reportMissingTypeArgument",)
    overridden = [rule for rule in locked if rule in config]
    if overridden:
        raise SystemExit(
            "Library Pyright config must not override "
            + ", ".join(overridden)
            + "; exempt modules opt out in a file comment"
        )
    weakened = [rule for rule in _EXTRA_RULES if config.get(rule) != "none"]
    if weakened:
        raise SystemExit(
            "Library Pyright config must set "
            + ", ".join(weakened)
            + " to none; fully strict modules opt in with a file comment"
        )
    sources = _library_sources()
    exempt = set(_EXEMPT)
    strict = set(_STRICT_MODULES)
    if len(exempt) != len(_EXEMPT) or len(strict) != len(_STRICT_MODULES):
        raise SystemExit("Typing module lists contain duplicates")
    if exempt & strict:
        raise SystemExit("A module cannot both opt out of unknown types and be fully strict")
    missing = (exempt | strict) - set(sources)
    if missing:
        raise SystemExit("Typing module list names missing files: " + ", ".join(sorted(missing)))
    for relative in sources:
        directive = _directives(relative)
        opted_out = _OPT_OUT in (ROOT / relative).read_text(encoding="utf-8").splitlines()
        if "reportMissingTypeArgument=false" in directive:
            raise SystemExit(f"{relative} cannot disable reportMissingTypeArgument")
        if relative in exempt:
            if not opted_out:
                raise SystemExit(
                    f"{relative} is exempt and must opt out with: {_OPT_OUT}"
                )
        elif opted_out or any(f"{rule}=false" in directive for rule in _UNKNOWN_RULES):
            raise SystemExit(
                f"{relative} opts out of unknown types but is not in the exemption list"
            )
        extra = [rule for rule in _EXTRA_RULES if f"{rule}=true" in directive]
        if relative in strict:
            missing_rules = [rule for rule in _EXTRA_RULES if rule not in extra]
            if missing_rules:
                raise SystemExit(
                    f"{relative} must enable {', '.join(missing_rules)} "
                    "in its opening Pyright comment"
                )
        elif extra:
            raise SystemExit(
                f"{relative} enables {', '.join(extra)} but is not a fully strict module"
            )
    checked = len(sources) - len(exempt)
    print(
        f"Typing classification: {checked} modules enforce unknown types; "
        f"{len(exempt)} explicitly exempt"
    )


def _pyright(project: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--project", str(project),
         "--pythonpath", sys.executable, "--outputjson"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90,
    )
    if result.returncode not in (0, 1):
        raise SystemExit(result.stdout + result.stderr)
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise SystemExit(result.stdout + result.stderr) from None


def _configured_sources(project: Path) -> int:
    """Count the Python files selected by a project's include list."""
    config_path = project / "pyrightconfig.json"
    if not config_path.is_file():
        raise SystemExit(f"Missing Pyright project config: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    includes = config.get("include")
    if not includes:
        raise SystemExit(f"{config_path} must name the files to typecheck")
    count = 0
    for raw in includes:
        path = project / raw
        if path.is_dir():
            count += sum(
                1 for file in path.rglob("*")
                if file.suffix in {".py", ".pyi"} and "__pycache__" not in file.parts
            )
        else:
            count += sum(
                1 for file in project.glob(raw)
                if file.is_file() and file.suffix in {".py", ".pyi"}
            )
    if count < 1:
        raise SystemExit(f"{config_path} did not match any Python files")
    return count


def _require_clean(project: Path, label: str) -> None:
    expected = _configured_sources(project)
    report = _pyright(project)
    summary = report["summary"]
    analyzed = summary["filesAnalyzed"]
    if analyzed != expected:
        raise SystemExit(
            f"{label} type check analyzed {analyzed} files; "
            f"the project config matches {expected}"
        )
    if summary["errorCount"] or summary["warningCount"]:
        print(json.dumps(report["generalDiagnostics"], indent=2))
        raise SystemExit(
            f"{label} type check failed: {summary['errorCount']} errors, "
            f"{summary['warningCount']} warnings"
        )
    print(f"Pyright {report['version']}: {analyzed} {label} files typechecked")


def main():
    if importlib.util.find_spec("pyright") is None:
        raise SystemExit("Install the dev extra to run the Pyright consumer checks")
    _require_classification()
    fixtures = sorted((ROOT / "tests/typing").glob("*.py"))
    expected = Counter()
    for fixture in fixtures:
        for line, text in enumerate(fixture.read_text(encoding="utf-8").splitlines()):
            marker = re.search(r"# error: ([\w, ]+)\s*$", text)
            if marker:
                rules = [rule.strip() for rule in marker[1].split(",") if rule.strip()]
                if not rules:
                    raise SystemExit(f"{fixture.name}:{line + 1} has an empty error marker")
                for rule in rules:
                    expected[(fixture.name, line, rule)] += 1
    if not fixtures or not expected:
        raise SystemExit("Typing fixtures must include valid cases and marked rejections")
    fixture_project = ROOT / "tests/typing"
    sources = _configured_sources(fixture_project)
    if sources != len(fixtures):
        raise SystemExit(
            f"Consumer project matches {sources} files; found {len(fixtures)} fixtures"
        )
    report = _pyright(fixture_project)
    if report["summary"]["filesAnalyzed"] != sources:
        raise SystemExit(
            "Pyright did not analyze every consumer fixture\n"
            + json.dumps(report["summary"])
        )
    diagnostics = [item for item in report["generalDiagnostics"]
                   if item["severity"] != "information"]
    actual = Counter((Path(item["file"]).name, item["range"]["start"]["line"],
                      item.get("rule", "")) for item in diagnostics)
    if actual != expected:
        print(json.dumps(diagnostics, indent=2))
        raise SystemExit(f"Missing rejections: {expected - actual}\nUnexpected diagnostics: {actual - expected}")
    print(f"Pyright {report['version']}: valid consumers passed; {sum(expected.values())} expected errors verified")
    _require_clean(ROOT, "library")
    _require_clean(ROOT / "examples", "example")


if __name__ == "__main__":
    main()
