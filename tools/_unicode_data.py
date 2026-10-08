"""Pinned, offline Unicode inputs shared by the table generators.

The gzip files contain complete upstream text, including notices where present.
Hashes cover the uncompressed bytes, so archive metadata is immaterial.
"""

import argparse
import gzip
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(__file__).with_name("unicode_data")
GRAPHEME_VERSION = "17.0.0"
TERMINAL_VERSION = "15.1.0"
# Source URLs are https://www.unicode.org/Public/<version>/ucd/<relative path>.
SOURCES = {
    ("17.0.0", "auxiliary/GraphemeBreakProperty.txt"): "d6b51d1d2ae5c33b451b7ed994b48f1f4dc62b2272a5831e7fd418514a6bae89",
    ("17.0.0", "emoji/emoji-data.txt"): "2cb2bb9455cda83e8481541ecf5b6dfda66a3bb89efa3fa7c5297eccf607b72b",
    ("17.0.0", "DerivedCoreProperties.txt"): "24c7fed1195c482faaefd5c1e7eb821c5ee1fb6de07ecdbaa64b56a99da22c08",
    ("15.1.0", "UnicodeData.txt"): "2fc713e6a31a87c4850a37fe2caffa4218180fadb5de86b43a143ddb4581fb86",
    ("15.1.0", "EastAsianWidth.txt"): "b08191401dc125f4e84ef262a95754faae6b737c79538e17ea9664a63434e94e",
    ("15.1.0", "SpecialCasing.txt"): "55a477efd933a52cd27e6a9bf70265bb2d8814af31aab07767abc8eb421f27ef",
    ("15.1.0", "DerivedCoreProperties.txt"): "f55d0db69123431a7317868725b1fcbf1eab6b265d756d1bd7f0f6d9f9ee108b",
}


def read_ucd(version, name):
    path = DATA / f"{version}-{Path(name).name}.gz"
    data = gzip.decompress(path.read_bytes())
    if hashlib.sha256(data).hexdigest() != SOURCES[version, name]:
        raise ValueError(f"Unicode input checksum mismatch: {path}")
    return data.decode("utf-8")


def records(text):
    """Read semicolon-separated UCD records, retaining empty fields."""
    for line in text.splitlines():
        body = line.partition("#")[0].strip()
        if body:
            yield tuple(field.strip() for field in body.split(";"))


def code_range(value):
    first, _, last = value.partition("..")
    return int(first, 16), int(last or first, 16)


def merge_ranges(rows):
    """Coalesce adjacent equal properties without changing any code point."""
    merged = []
    for first, last, value in sorted(rows):
        if merged and first == merged[-1][1] + 1 and value == merged[-1][2]:
            merged[-1] = merged[-1][0], last, value
        else:
            merged.append((first, last, value))
    return merged


def grapheme_tables():
    tables = {"GCB": [], "PICTOGRAPHIC": [], "INCB": []}
    for row in records(read_ucd(GRAPHEME_VERSION, "auxiliary/GraphemeBreakProperty.txt")):
        tables["GCB"].append((*code_range(row[0]), row[1]))
    for row in records(read_ucd(GRAPHEME_VERSION, "emoji/emoji-data.txt")):
        if row[1] == "Extended_Pictographic":
            tables["PICTOGRAPHIC"].append((*code_range(row[0]), "EP"))
    for row in records(read_ucd(GRAPHEME_VERSION, "DerivedCoreProperties.txt")):
        if row[1] == "InCB":
            tables["INCB"].append((*code_range(row[0]), row[2]))
    return {name: merge_ranges(rows) for name, rows in tables.items()}


def generate_cli(render, output, description, *, additional_outputs=None):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--check", action="store_true", help="Fail if the output is missing or stale; do not write")
    parser.add_argument("--output", type=Path, default=output, help="Destination file (defaults to the checked-in table)")
    for name, (_, path) in (additional_outputs or {}).items():
        parser.add_argument(f"--{name}-output", type=Path, default=path, help=f"Destination for the {name} table")
    options = parser.parse_args()
    try:
        outputs = [(options.output, render().encode("utf-8"))]
        outputs.extend((getattr(options, f"{name}_output"), renderer().encode("utf-8"))
                       for name, (renderer, _) in (additional_outputs or {}).items())
        for path, data in outputs:
            if options.check:
                if not path.is_file() or path.read_bytes() != data:
                    parser.exit(1, f"Unicode table is missing or stale: {path}\nRun this generator without --check.\n")
            else:
                path.write_bytes(data)
    except (OSError, ValueError) as error:
        parser.exit(1, f"Unicode generation failed: {error}\n")
    for path, _ in outputs:
        print(f"{'Checked' if options.check else 'Generated'} {path}")
    return 0
