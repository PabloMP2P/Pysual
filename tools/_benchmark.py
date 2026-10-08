"""Environment facts shared by the standalone benchmark commands."""

from datetime import datetime, timezone
from pathlib import Path
import platform
import subprocess

from pysual import __version__


def metadata():
    root = Path(__file__).resolve().parents[1]
    revision = dirty = None
    try:
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            stderr=subprocess.DEVNULL, timeout=5,
        ).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True,
            stderr=subprocess.DEVNULL, timeout=5,
        ).strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {
        "schema_version": 1,
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "revision": revision, "working_tree_dirty": dirty,
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(), "machine": platform.machine(),
        "pysual": __version__, "clock": "time.perf_counter",
    }
