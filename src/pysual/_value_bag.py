"""Committed property storage and its local visual generation.

``__getitem__`` stays on ``dict``'s C implementation; the sibling stub types
known keys. Item replacements advance a local counter without retaining an
owner. Identity comparison avoids walking large immutable values.
"""

from __future__ import annotations

from collections.abc import Mapping


_MISSING = object()


class _ValueBag(dict[str, object]):
    """Heterogeneous property dict. Known keys are typed by ``_value_bag.pyi``."""

    # Older dict-subclass pickles can bypass __init__ when restoring items.
    _epoch: int = 0

    def __init__(
        self, mapping: Mapping[str, object] | None = None, epoch: int = 0
    ) -> None:
        self._epoch = epoch
        if mapping is None:
            super().__init__()
        else:
            super().__init__(mapping)

    def __setitem__(self, key: str, value: object) -> None:
        if self.get(key, _MISSING) is value:
            return
        super().__setitem__(key, value)
        self._epoch += 1

    def __reduce__(self) -> tuple[type[_ValueBag], tuple[dict[str, object], int]]:
        # Rebuild through dict construction, without replaying tracked writes.
        return type(self), (dict(self), self._epoch)


def _replaced_bag(current: _ValueBag, updates: Mapping[str, object]) -> _ValueBag:
    """Commit one new generation without changing the previous mapping."""
    bag = _ValueBag(current, current._epoch + 1)
    bag.update(updates)
    return bag
