"""Default extended grapheme clusters, Unicode 17.0 (UAX #29).

Offsets are Python string indices. Segmentation does not promise font shaping,
bidirectional rendering or platform IME support.
"""

from bisect import bisect_right
from functools import lru_cache
from ._grapheme_data import GCB, INCB, PICTOGRAPHIC

_GCB_STARTS = tuple(row[0] for row in GCB)
_INCB_STARTS = tuple(row[0] for row in INCB)
_EP_STARTS = tuple(row[0] for row in PICTOGRAPHIC)
_CONTROL = frozenset(("Control", "CR", "LF"))


def _lookup(code, table, starts):
    i = bisect_right(starts, code) - 1
    return table[i][2] if i >= 0 and code <= table[i][1] else "Other"


@lru_cache(maxsize=4096)
def _properties(char):
    code = ord(char)
    return (
        _lookup(code, GCB, _GCB_STARTS),
        _lookup(code, INCB, _INCB_STARTS),
        _lookup(code, PICTOGRAPHIC, _EP_STARTS) == "EP",
    )


def _simple_break(left, right):
    if left == "CR" and right == "LF":
        return False
    if left in _CONTROL or right in _CONTROL:
        return True
    if left == "L" and right in ("L", "V", "LV", "LVT"):
        return False
    if left in ("LV", "V") and right in ("V", "T"):
        return False
    if left in ("LVT", "T") and right == "T":
        return False
    if right in ("Extend", "ZWJ", "SpacingMark") or left == "Prepend":
        return False
    return None


def boundaries(text: str) -> list[int]:
    """Return all cluster boundaries in one pass, including 0 and len(text)."""
    result = [0]
    previous = "Other"
    regional = emoji = indic = 0
    for index, char in enumerate(text):
        gcb, incb, pictographic = _properties(char)
        split = _simple_break(previous, gcb)
        if split is None:
            split = not (
                (incb == "Consonant" and indic == 2)
                or (pictographic and emoji == 2)
                or (gcb == "Regional_Indicator" and regional % 2)
            )
        if index and split:
            result.append(index)
        regional = regional + 1 if gcb == "Regional_Indicator" else 0
        emoji = (
            1
            if pictographic
            else 1
            if emoji == 1 and gcb == "Extend"
            else 2
            if emoji == 1 and gcb == "ZWJ"
            else 0
        )
        indic = (
            1
            if incb == "Consonant"
            else 2
            if indic and incb == "Linker"
            else indic
            if incb == "Extend"
            else 0
        )
        previous = gcb
    if text:
        result.append(len(text))
    return result


def is_boundary(text: str, index: int) -> bool:
    """Test a boundary locally; contextual rules inspect only their prefix run."""
    if index <= 0 or index >= len(text):
        return True
    left, _, _ = _properties(text[index - 1])
    right, incb, pictographic = _properties(text[index])
    split = _simple_break(left, right)
    if split is not None:
        return split
    if incb == "Consonant":
        cursor, linker = index - 1, False
        while cursor >= 0:
            prop = _properties(text[cursor])[1]
            if prop not in ("Extend", "Linker"):
                if prop == "Consonant" and linker:
                    return False
                break
            linker |= prop == "Linker"
            cursor -= 1
    if pictographic and left == "ZWJ":
        cursor = index - 2
        while cursor >= 0 and _properties(text[cursor])[0] == "Extend":
            cursor -= 1
        if cursor >= 0 and _properties(text[cursor])[2]:
            return False
    if left == right == "Regional_Indicator":
        cursor = index - 1
        while cursor >= 0 and _properties(text[cursor])[0] == "Regional_Indicator":
            cursor -= 1
        return (index - 1 - cursor) % 2 == 0
    return True


def previous_boundary(text: str, index: int) -> int:
    index = max(0, index - 1)
    while index and not is_boundary(text, index):
        index -= 1
    return index


def next_boundary(text: str, index: int) -> int:
    index = min(len(text), index + 1)
    while index < len(text) and not is_boundary(text, index):
        index += 1
    return index


def floor_boundary(text: str, index: int) -> int:
    return index if is_boundary(text, index) else previous_boundary(text, index)
