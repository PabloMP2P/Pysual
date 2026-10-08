"""Owner-free numeric range behavior shared by bounded value controls.

Controls retain their property schemas, diagnostics, and commit/notification
path. A range is constructed from the current validation candidate, so it is
also safe to use from the shallow copies used for atomic property updates.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NumericRange:
    minimum: float | None
    maximum: float | None

    def contains(self, value: float) -> bool:
        return (
            (self.minimum is None or self.minimum <= value)
            and (self.maximum is None or value <= self.maximum)
        )

    def is_ordered(self, *, strict: bool = False) -> bool:
        if self.minimum is None or self.maximum is None:
            return True
        return (
            self.minimum < self.maximum if strict
            else self.minimum <= self.maximum
        )

    def clamp(self, value: float) -> float:
        if self.minimum is not None:
            value = max(self.minimum, value)
        if self.maximum is not None:
            value = min(self.maximum, value)
        return value
