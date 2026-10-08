"""Shared viewport metrics, scrollbar drawing and pointer gestures."""

from dataclasses import dataclass

from .geometry import Rect

SCROLLBAR_SIZE = 12


def clamp_scroll(offset, total, viewport):
    return max(0, min(offset, max(0, total - viewport)))


def can_scroll(offset, limit, delta):
    """Accept fractions while there is overflow in the requested direction."""
    return (delta < 0 and offset > 0) or (delta > 0 and offset < limit)


def thumb_metrics(length, total, viewport, offset, minimum=20):
    """Return (track-relative start, size), or None when scrolling is unnecessary."""
    if length <= 0 or total <= viewport or viewport <= 0:
        return None
    size = min(length, max(minimum, length * viewport / total))
    start = (length - size) * clamp_scroll(offset, total, viewport) / (total - viewport)
    return start, size


def scroll_from_thumb(
    point, track_start, track_length, thumb_length, grab_offset, total, viewport
):
    """Map an absolute pointer position to a bounded content offset."""
    travel = track_length - thumb_length
    if travel <= 0:
        return 0
    return clamp_scroll(
        (point - track_start - grab_offset) / travel * max(0, total - viewport),
        total,
        viewport,
    )


def typeahead_prefix(prefix, last_at, key, now):
    """Keep a short search prefix; repeated letters cycle matches."""
    stored = (prefix if now - last_at < 0.75 else "") + key.casefold()
    return stored, key.casefold() if len(set(stored)) == 1 else stored


@dataclass(frozen=True)
class Scrollbar:
    axis: int
    track: Rect
    thumb: Rect
    total: float
    viewport: float
    offset: float

    def __iter__(self):
        # Keep the existing ScrollArea geometry inspection shape.
        return iter((self.axis, self.track, self.thumb))


def scrollbar(track, total, viewport, offset, axis=1):
    metrics = thumb_metrics(track.width if axis == 0 else track.height,
                            total, viewport, offset)
    if metrics is None or track.width <= 0 or track.height <= 0:
        return None
    start, length = metrics
    thumb = (Rect(track.x + start, track.y, length, track.height) if axis == 0
             else Rect(track.x, track.y + start, track.width, length))
    return Scrollbar(axis, track, thumb, total, viewport, offset)


def handle_scrollbars(event, bars, drag):
    """Return handled, (pointer, axis, grab) capture, and changed axis offsets.

    Tracks page once on primary press; thumbs retain the original grab offset.
    Only the matching pointer can move or release a captured thumb.
    """
    if event.kind in ("blur", "pointer_cancel"):
        return drag is not None, None, {}
    if event.kind == "pointer_up" and drag is not None:
        return True, None if event.pointer_id == drag[0] else drag, {}
    if event.kind == "pointer_move" and drag is not None:
        pointer, axis, grab = drag
        if event.pointer_id != pointer:
            return True, drag, {}
        for bar in bars:
            if bar.axis == axis:
                horizontal = axis == 0
                value = scroll_from_thumb(
                    event.x if horizontal else event.y,
                    bar.track.x if horizontal else bar.track.y,
                    bar.track.width if horizontal else bar.track.height,
                    bar.thumb.width if horizontal else bar.thumb.height,
                    grab, bar.total, bar.viewport,
                )
                return True, drag, {axis: value}
        return True, None, {}
    if event.kind == "pointer_down" and event.button == 1:
        for bar in bars:
            if not bar.track.contains(event.x, event.y):
                continue
            point = event.x if bar.axis == 0 else event.y
            start = bar.thumb.x if bar.axis == 0 else bar.thumb.y
            if bar.thumb.contains(event.x, event.y):
                return True, (event.pointer_id, bar.axis, point - start), {}
            value = clamp_scroll(bar.offset + bar.viewport * (-1 if point < start else 1),
                                 bar.total, bar.viewport)
            return True, None, {bar.axis: value}
    return False, drag, {}


def paint_scrollbars(painter, bars):
    """Paint the same themed track and thumb in each control's local coordinates."""
    for bar in bars:
        for box, part in ((bar.track, "track"), (bar.thumb, "thumb")):
            painter.surface(Rect(box.x - painter.bounds.x, box.y - painter.bounds.y,
                                 box.width, box.height), painter.style(part))
