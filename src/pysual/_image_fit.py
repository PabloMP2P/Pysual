"""Centered source and destination rectangles, using already decoded dimensions."""

from .geometry import Rect


def image_rects(width, height, rect, fit="stretch"):
    if fit not in ("stretch", "contain", "cover"):
        raise ValueError("Image fit must be stretch, contain or cover")
    source = Rect(0, 0, width, height)
    if fit == "stretch":
        return source, rect
    if min(width, height, rect.width, rect.height) <= 0:
        return Rect(), Rect(rect.x, rect.y, 0, 0)
    scale = (min if fit == "contain" else max)(rect.width / width, rect.height / height)
    if fit == "contain":
        w, h = width * scale, height * scale
        return source, Rect(rect.x + (rect.width - w) / 2,
                            rect.y + (rect.height - h) / 2, w, h)
    w, h = rect.width / scale, rect.height / scale
    return Rect((width - w) / 2, (height - h) / 2, w, h), rect
