"""Portable drawing primitives shared by Painter and native frame helpers."""

from functools import lru_cache
from math import ceil, floor

from .geometry import Rect


@lru_cache(maxsize=2048)
def mix_colors(first, second, fraction):
    def channels(color):
        color = color[1:]
        if len(color) == 6:
            color += "ff"
        return tuple(int(color[i : i + 2], 16) for i in range(0, 8, 2))

    return "#" + "".join(
        f"{round(a + (b - a) * fraction):02x}"
        for a, b in zip(channels(first), channels(second))
    )


def gradient_fallback(host, rect, first, last, axis, radius, border_width, clip):
    """Paint bounded device-aligned bands in host coordinates, restoring clip."""
    scale = getattr(host, "render_scale", 1)
    horizontal = axis == "horizontal"
    start = rect.x if horizontal else rect.y
    length = rect.width if horizontal else rect.height
    low, high = floor(start * scale), ceil((start + length) * scale)
    count = min(96, high - low)
    # A raw Host caller may have no explicit clip. Cover the entire rounded
    # shape, including its device-rounded edges; the target bounds still clip.
    area = clip if clip is not None else Rect(
        floor(rect.x * scale) / scale,
        floor(rect.y * scale) / scale,
        (ceil(rect.right * scale) - floor(rect.x * scale)) / scale,
        (ceil(rect.bottom * scale) - floor(rect.y * scale)) / scale,
    )
    try:
        for i in range(count):
            a = low + (high - low) * i // count
            b = low + (high - low) * (i + 1) // count
            band = (
                Rect(a / scale, area.y, (b - a) / scale, area.height)
                if horizontal
                else Rect(area.x, a / scale, area.width, (b - a) / scale)
            )
            host.clip(area.intersect(band))
            color = mix_colors(first, last, i / max(1, count - 1))
            host.rect(
                rect, "" if border_width else color, radius,
                color if border_width else "", border_width,
            )
    finally:
        host.clip(clip)
