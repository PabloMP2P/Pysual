"""Concrete caption drawing commands, independent of control state."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .geometry import Rect

if TYPE_CHECKING:
    from .painting import Painter


@dataclass(frozen=True, slots=True)
class Caption:
    """A text block and optional leading icon, with fully resolved styling."""

    text: str
    bounds: Rect
    color: str
    size: float
    mono: bool
    align: Literal["left", "center"] = "left"
    ellipsis: bool = True
    line_height: float | None = None
    icon: str = ""
    icon_size: float = 20

    def geometry(self) -> tuple[float, float, float]:
        size = min(self.icon_size, self.bounds.width, self.bounds.height) if self.icon else 0
        prefix = size + (8 if self.text else 0) if self.icon else 0
        return size, prefix, max(0, self.bounds.width - prefix)

    def measurement(self) -> str | None:
        """The full string this concrete drawing operation needs measured."""
        width = self.geometry()[2]
        text = "" if self.ellipsis and width <= 0 else self.text
        return text if (self.ellipsis and width > 0
                        or self.line_height is None or self.align == "center") else None


def paint_caption(
    painter: Painter, caption: Caption, *, text: str | None = None,
    metric: tuple[float, float] | None = None,
) -> None:
    """Draw eagerly, or finish a caption whose measurements were batched."""
    size, prefix, available = caption.geometry()
    family: Literal["ui", "mono"] = "mono" if caption.mono else "ui"
    if text is None:
        text = (painter.elide(caption.text, available, size=caption.size, font_family=family)
                if caption.ellipsis else caption.text)
    if not text:
        prefix = size
    if caption.line_height is None or caption.align == "center":
        width, height = (painter.measure(text, size=caption.size, font_family=family)
                         if metric is None else metric)
    else:
        width, height = 0, caption.line_height
    if caption.line_height is not None:
        height = caption.line_height
    bounds = caption.bounds
    left = bounds.x + (max(0, (bounds.width - width - prefix) / 2)
                       if caption.align == "center" else 0)
    if caption.icon and size > 0:
        painter.icon(caption.icon, left - painter.bounds.x,
                     bounds.y - painter.bounds.y + max(0, (bounds.height - size) / 2),
                     size=size, color=caption.color)
    painter.text(text, left - painter.bounds.x + prefix,
                 bounds.y - painter.bounds.y + max(0, (bounds.height - height) / 2),
                 color=caption.color, size=caption.size, font_family=family)
