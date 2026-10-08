"""Measure cold RGB PNG decoding; fixture creation and disk I/O are not timed.

Run with the dev dependencies installed: python tools/benchmark_png.py
Compare the same dimensions, repeats and environment before/after a change.
Use --pattern noise to check decoding of less compressible images as well.
"""

import argparse
import io
import json
from pathlib import Path
import platform
import random
import statistics
import sys
from time import perf_counter

from PIL import Image, __version__ as pillow_version

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pysual._png import decode_png


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=positive_int, default=1024)
    parser.add_argument("--height", type=positive_int, default=768)
    parser.add_argument("--repeats", type=positive_int, default=5)
    parser.add_argument("--pattern", choices=("gradient", "noise"), default="gradient")
    args = parser.parse_args()
    rgb = (
        random.Random(7).randbytes(args.width * args.height * 3)
        if args.pattern == "noise" else bytes(
            channel
            for y in range(args.height)
            for x in range(args.width)
            for channel in (x % 256, y % 256, (x + y) % 256)
        )
    )
    fixture = Image.frombytes("RGB", (args.width, args.height), rgb)
    buffer = io.BytesIO()
    fixture.save(buffer, format="PNG")
    encoded = buffer.getvalue()
    expected = (args.width, args.height, fixture.convert("RGBA").tobytes())
    samples = []
    for _ in range(args.repeats):
        started = perf_counter()
        decoded = decode_png(encoded)
        samples.append((perf_counter() - started) * 1000)
        if decoded != expected:
            raise AssertionError("Decoded pixels differ from the source image")
    print(json.dumps({
        "python": platform.python_version(), "platform": platform.platform(),
        "pillow": pillow_version, "pattern": args.pattern,
        "width": args.width, "height": args.height,
        "png_bytes": len(encoded), "samples_ms": samples,
        "median_ms": statistics.median(samples), "pixel_exact": True,
    }, indent=2))


if __name__ == "__main__":
    main()
