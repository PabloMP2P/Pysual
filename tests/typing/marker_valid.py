"""Partial styles remain valid public inputs for surfaces and choice markers."""

from pysual import Painter, Rect, Style


def paint_markers(painter: Painter) -> None:
    bounds = Rect(0, 0, 20, 20)
    partial = Style(fill="#112233", border="#445566", border_width=1)
    painter.marker(bounds, partial)
    painter.marker(bounds, partial, shape="circle", checked=True)
    painter.surface(bounds, partial)
    painter.marker(bounds, painter.style())
    painter.surface(bounds, painter.style())
