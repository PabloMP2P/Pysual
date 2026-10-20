"""One invalidation contract for built-in and custom painting.

Revisions account for private, public and descendant changes. The remaining
inputs describe the drawing environment; no control-specific dependency list
or implementation audit participates in cache validity.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

from .host import text_row_height

if TYPE_CHECKING:
    from .controls import Control
    from .geometry import Rect
    from .host import Host
    from .theme import Theme


class PaintInputs(NamedTuple):
    state: int
    bounds: Rect
    clip: Rect
    paint_clip: Rect
    theme: Theme
    enabled: bool
    hover: bool
    pressed: bool
    focused: bool
    layout: object
    scale: float
    resources: int


def measurement_uses_resources(control: Control) -> bool:
    """Resource independence is explicit, never inferred from a control type."""
    return control._values["measure_resources"] is not False


def paint_inputs(
    control: Control, host: Host, focus: Control | None, theme: Theme | None,
) -> PaintInputs:
    """Capture validity before drawing, including propagated descendant dirt.

    Keep this pre-draw revision even when a callback invalidates during drawing:
    that newer state still needs another frame, rather than a relabeled cache.
    """
    revision = control._paint_revision
    return PaintInputs(
        revision,
        control._rect, control._clip, control._paint_clip,
        control._fast_theme() if theme is None else theme,
        control._committed_enabled(), control._hover, control._pressed,
        control is focus,
        text_row_height(host, 0.0),
        getattr(host, "render_scale", 1.0), getattr(host, "resource_revision", 0),
    )
