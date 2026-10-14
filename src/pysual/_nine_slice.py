"""Pure geometry for masks with repeated centers and varying edge samples."""

from collections.abc import Iterator

# 128 ordinary RGBA masks below this size fit the 32 MiB decoded-image budget.
# Smaller masks keep one blit; splitting their spans costs more than it saves.
MIN_COMPACT_PIXELS = 65_536


def nine_slice(
    width: int, height: int, edge_x: int, edge_y: int
) -> tuple[int, int, tuple[int, int, int, int]] | None:
    left = right = min(width // 2, max(0, edge_x))
    top = bottom = min(height // 2, max(0, edge_y))
    compact_width = min(width, left + right + 1)
    compact_height = min(height, top + bottom + 1)
    if min(width, height) <= 0 or (compact_width, compact_height) == (width, height):
        return None
    return compact_width, compact_height, (left, top, right, bottom)


def slice_regions(
    width: int,
    height: int,
    target_width: float,
    target_height: float,
    edges: tuple[int, int, int, int],
) -> Iterator[tuple[tuple[int, int, int, int], tuple[float, float, float, float]]]:
    """Yield source/destination pixel rectangles, preserving original sampling."""
    left, top, right, bottom = edges
    sx, sy = (0, left, width - right, width), (0, top, height - bottom, height)
    xscale = target_width / max(1, round(target_width))
    yscale = target_height / max(1, round(target_height))
    dx = (0, left * xscale, target_width - right * xscale, target_width)
    dy = (0, top * yscale, target_height - bottom * yscale, target_height)
    for row in range(3):
        for col in range(3):
            sw, sh = sx[col + 1] - sx[col], sy[row + 1] - sy[row]
            dw, dh = dx[col + 1] - dx[col], dy[row + 1] - dy[row]
            if min(sw, sh, dw, dh) > 0:
                yield (sx[col], sy[row], sw, sh), (dx[col], dy[row], dw, dh)
