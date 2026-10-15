"""Measure construction through named assignment and anonymous add().

Run in an installed development checkout:
    python tools/benchmark_bindings.py --counts 500 1000 --samples 3

Prints JSON with environment/source identity and raw samples. Work runs on the
UI owner; timings include Label construction and attachment/name binding but
exclude dispatch, root construction, cleanup, rendering and display work.
Save output before and after a change with the same arguments and machine load.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import inspect
import json
from pathlib import Path
import statistics
import time

from _benchmark import metadata

from pysual import Container, Label
import pysual
import pysual.controls as controls
from pysual._engine import call_opening


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _scenario(count: int, named: bool) -> float:
    root = Container()
    try:
        started = time.perf_counter()
        for index in range(count):
            child = Label(text=f"Row {index}")
            if named:
                setattr(root, f"row_{index}", child)
            else:
                root.add(child)
        elapsed = (time.perf_counter() - started) * 1000
        assert len(root.children) == count
        if named:
            assert root.children[-1].name == f"row_{count - 1}"
        return elapsed
    finally:
        root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", nargs="+", type=_positive, default=[100, 500, 1000])
    parser.add_argument("--samples", type=_positive, default=3)
    parser.add_argument("--label", default="", help="Optional label for this saved run")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if Path(controls.__file__).resolve() != (root / "src/pysual/controls.py").resolve():
        parser.error("Install this checkout with pip install -e . or set PYTHONPATH=src")
    result = {
        **metadata(),
        "label": args.label,
        "pysual_source": pysual.__file__,
        "binding_method_source": inspect.getsourcefile(inspect.unwrap(Container.__setattr__)),
        "binding_method_sha256": hashlib.sha256(inspect.getsource(
            inspect.unwrap(Container.__setattr__)).encode("utf-8")).hexdigest(),
        "controls_sha256": hashlib.sha256((root / "src/pysual/controls.py").read_bytes()).hexdigest(),
        "benchmark_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "warmup": {"controls": 20, "samples_per_scenario": 1},
        "samples_per_scenario": args.samples,
        "timing_boundary": "UI-owner Label construction plus add or canonical assignment; no rendering",
        "gc": "enabled; collected before each timed sample",
        "scenarios": [],
    }
    for count in args.counts:
        for named in (False, True):
            call_opening(_scenario, 20, named)
            samples = []
            for _ in range(args.samples):
                gc.collect()
                samples.append(call_opening(_scenario, count, named))
            result["scenarios"].append({
                "controls": count,
                "construction": "named_assignment" if named else "anonymous_add",
                "samples_ms": samples,
                "median_ms": statistics.median(samples),
                "minimum_ms": min(samples),
                "maximum_ms": max(samples),
            })
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
