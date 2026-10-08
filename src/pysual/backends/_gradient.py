"""Exact, bounded color tables for the shared device-aligned gradient bands."""

from functools import lru_cache


@lru_cache(maxsize=256)
def gradient_colors(first, last, count):
    def channels(color):
        value = color[1:] + ("ff" if len(color) == 7 else "")
        return tuple(int(value[i : i + 2], 16) for i in range(0, 8, 2))

    pairs = tuple(zip(channels(first), channels(last)))
    return tuple(
        tuple(round(a + (b - a) * (i / max(1, count - 1))) for a, b in pairs)
        for i in range(count)
    )
