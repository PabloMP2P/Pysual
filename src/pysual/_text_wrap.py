"""Source-preserving soft line breaks shared by labels and text editors."""

from bisect import bisect_right
from collections.abc import Callable
from math import isfinite

from .graphemes import boundaries


def wrap_line(
    line: str, width: float, measure: Callable[[int, int], float], *,
    break_on_whitespace: bool = True,
) -> list[tuple[int, int]]:
    """Partition one logical line into measured, grapheme-safe source spans.

    ``measure(start, end)`` returns the displayed width of that source slice;
    callers can therefore account for font shaping, tab stops or masking.
    Whitespace is preserved, with breaks preferred after whitespace. Trailing
    separators stay with a fitting word even if they extend past ``width``;
    the caller clips them instead of letting them split the following word.
    A single cluster wider than the viewport also occupies one clipped row.
    Empty lines occupy one row. Masked text disables whitespace breaks because
    its separators are displayed as ordinary visible glyphs.
    """
    if not isfinite(width):
        raise ValueError("wrap width must be finite")
    if not line:
        return [(0, 0)]
    width = max(0.0, width)
    offsets = boundaries(line)
    breaks = ([index for index in range(1, len(offsets))
               if line[offsets[index - 1]:offsets[index]].isspace()]
              if break_on_whitespace else [])
    last = len(offsets) - 1
    rows: list[tuple[int, int]] = []
    start = 0
    while start < last:
        # Probe near this row first. Searching the complete remaining suffix
        # for every narrow row makes long unbroken input quadratic to measure.
        low, high = start, start + 1
        while measure(offsets[start], offsets[high]) <= width:
            low = high
            if high == last:
                break
            high = min(last, start + (high - start) * 2)
        while high - low > 1:
            middle = (low + high) // 2
            if measure(offsets[start], offsets[middle]) <= width:
                low = middle
            else:
                high = middle
        stop = max(start + 1, low)
        if break_on_whitespace and stop < last:
            if (line[offsets[stop]:offsets[stop + 1]].isspace()
                    and not line[offsets[start]:offsets[stop]].isspace()):
                # The fitted prefix ends at a word or inside its separators.
                # Consume this tail once, including tabs, without measuring
                # successively longer whitespace suffixes.
                while stop < last and line[offsets[stop]:offsets[stop + 1]].isspace():
                    stop += 1
            else:
                previous = bisect_right(breaks, stop) - 1
                if previous >= 0:
                    candidate = breaks[previous]
                    if (candidate > start
                            and not line[offsets[start]:offsets[candidate]].isspace()):
                        stop = candidate
        rows.append((offsets[start], offsets[stop]))
        start = stop
    return rows
