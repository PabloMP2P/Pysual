"""Immutable logical geometry shared by layout, input, and adapters."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Rect:
    x: float = 0
    y: float = 0
    width: float = 0
    height: float = 0

    @property
    def right(self):
        return self.x + self.width

    @property
    def bottom(self):
        return self.y + self.height

    def contains(self, x, y):
        return self.x <= x < self.right and self.y <= y < self.bottom

    def inset(self, amount):
        return Rect(
            self.x + amount,
            self.y + amount,
            max(0, self.width - amount * 2),
            max(0, self.height - amount * 2),
        )

    def intersect(self, other):
        x, y = max(self.x, other.x), max(self.y, other.y)
        return Rect(
            x,
            y,
            max(0, min(self.right, other.right) - x),
            max(0, min(self.bottom, other.bottom) - y),
        )
