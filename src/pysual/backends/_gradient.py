"""Exact, bounded color tables for the shared device-aligned gradient bands."""

from functools import lru_cache


@lru_cache(maxsize=256)
def gradient_colors(
    first: str, last: str, count: int
) -> tuple[tuple[int, int, int, int], ...]:
    def channels(color: str) -> tuple[int, int, int, int]:
        value = color[1:] + ("ff" if len(color) == 7 else "")
        return (
            int(value[0:2], 16),
            int(value[2:4], 16),
            int(value[4:6], 16),
            int(value[6:8], 16),
        )

    start, end = channels(first), channels(last)
    steps = max(1, count - 1)
    return tuple(
        (
            round(start[0] + (end[0] - start[0]) * (i / steps)),
            round(start[1] + (end[1] - start[1]) * (i / steps)),
            round(start[2] + (end[2] - start[2]) * (i / steps)),
            round(start[3] + (end[3] - start[3]) * (i / steps)),
        )
        for i in range(count)
    )
