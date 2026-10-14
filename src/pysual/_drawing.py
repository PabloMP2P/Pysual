"""Portable drawing primitives shared by Painter and native frame helpers."""

from functools import lru_cache
from math import ceil, floor

from .geometry import Rect
from .host import Host


@lru_cache(maxsize=2048)
def mix_colors(first: str, second: str, fraction: float) -> str:
    def channels(color: str) -> tuple[int, int, int, int]:
        color = color[1:]
        if len(color) == 6:
            color += "ff"
        return (
            int(color[0:2], 16),
            int(color[2:4], 16),
            int(color[4:6], 16),
            int(color[6:8], 16),
        )

    return "#" + "".join(
        f"{round(a + (b - a) * fraction):02x}"
        for a, b in zip(channels(first), channels(second))
    )


def gradient_fallback(
    host: Host,
    rect: Rect,
    first: str,
    last: str,
    axis: str,
    radius: float,
    border_width: float,
    clip: Rect | None,
) -> None:
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
