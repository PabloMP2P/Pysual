"""Unicode regeneration is offline, version-pinned and preserves checked-in data."""

import gzip
from pathlib import Path
import subprocess
import sys

import pytest

from tools import _unicode_data
from tools.generate_terminal_unicode import terminal_data
from pysual import _terminal_data
from pysual.backends._term_cells import safe_text, text_cells

ROOT = Path(__file__).resolve().parents[1]
GENERATORS = (
    ("generate_graphemes.py", "src/pysual/_grapheme_data.py", "--output"),
    ("generate_terminal_unicode.py", "native/pt_unicode.h", "--output"),
    ("generate_terminal_unicode.py", "src/pysual/_terminal_data.py", "--python-output"),
)


def run_generator(name, directory, *arguments):
    return subprocess.run(
        [sys.executable, "-I", str(ROOT / "tools" / name), *map(str, arguments)],
        cwd=directory, capture_output=True, text=True, timeout=20,
    )


def output_arguments(name, directory, option, output):
    if name == "generate_terminal_unicode.py":
        other = "--python-output" if option == "--output" else "--output"
        return [other, directory / "other-table", option, output]
    return [option, output]


@pytest.mark.parametrize("name,table,option", GENERATORS)
def test_regeneration_matches_checked_in_bytes_from_unrelated_directory(tmp_path, name, table, option):
    result = run_generator(name, tmp_path, "--check")
    assert result.returncode == 0, result.stdout + result.stderr
    output = tmp_path / "regenerated"
    result = run_generator(name, tmp_path, *output_arguments(name, tmp_path, option, output))
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_bytes() == (ROOT / table).read_bytes()


@pytest.mark.parametrize("name,table,option", GENERATORS)
def test_check_rejects_missing_or_stale_output_without_writing(tmp_path, name, table, option):
    output = tmp_path / "table"
    # Prepare the companion output so a failure concerns the selected table.
    arguments = output_arguments(name, tmp_path, option, output)
    result = run_generator(name, tmp_path, *arguments)
    assert result.returncode == 0, result.stdout + result.stderr
    output.unlink()
    result = run_generator(name, tmp_path, "--check", *arguments)
    assert result.returncode == 1 and "missing or stale" in result.stderr
    assert not output.exists()
    stale = (ROOT / table).read_bytes() + b"stale\n"
    output.write_bytes(stale)
    result = run_generator(name, tmp_path, "--check", *arguments)
    assert result.returncode == 1 and "missing or stale" in result.stderr
    assert output.read_bytes() == stale


def test_modified_unicode_input_is_rejected_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(_unicode_data, "DATA", tmp_path)
    (tmp_path / "15.1.0-UnicodeData.txt.gz").write_bytes(gzip.compress(b"modified source"))
    with pytest.raises(ValueError, match="checksum mismatch"):
        _unicode_data.read_ucd("15.1.0", "UnicodeData.txt")


def test_terminal_baseline_uses_pinned_properties_and_full_case_mappings():
    flags, rows = terminal_data()
    # U+2FFC became wide in 15.1; U+1FAE9 remains unassigned in that baseline.
    # Running on Python with another Unicode version must not change either.
    assert flags[0x2FFC] == 8 and flags[0x1FAE9] == 0
    assert flags[0x0301] == 4 and flags[0x200D] == 5 and flags[0xD800] == 2
    cases = {code: (lower, upper, is_upper) for code, lower, upper, is_upper in rows}
    assert cases[0x00DF] == ("\u00df", "SS", 0)
    assert cases[0x0130] == ("i\u0307", "\u0130", 1)
    assert cases[0x01C5] == ("\u01c6", "\u01c4", 0)  # titlecase is not uppercase
    assert cases[0x1D400] == ("\U0001d400", "\U0001d400", 1)


def test_python_terminal_uses_the_complete_shared_width_and_category_baseline():
    flags, _ = terminal_data()
    assert _terminal_data.UNICODE_VERSION == _unicode_data.TERMINAL_VERSION
    ends = (*_terminal_data.STARTS[1:], len(flags))
    for start, end, value in zip(_terminal_data.STARTS, ends, _terminal_data.FLAGS):
        assert flags[start:end] == bytes([value]) * (end - start)
    assert list(text_cells("\u2630\U0001fae9\U0001fa89\U0001fa8f\U0001f680")) == [
        ("\u2630", 1), ("\U0001fae9", 1), ("\U0001fa89", 1), ("\U0001fa8f", 1), ("\U0001f680", 2),
    ]
    assert safe_text("a\x1b\u202e\ud800\n\t\u200d") == "a\ufffd\n\t\u200d"
    assert safe_text("A\0B") == "AB"
