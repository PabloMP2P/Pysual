"""Committed property storage.

``__getitem__`` is intentionally not overridden. The sibling stub types the
known keys, and reads stay on ``dict``'s C implementation.
"""

from __future__ import annotations

from collections.abc import Mapping


class _ValueBag(dict[str, object]):
    """Heterogeneous property dict. Known keys are typed by ``_value_bag.pyi``."""


def _replaced_bag(current: Mapping[str, object], updates: Mapping[str, object]) -> _ValueBag:
    """Return one new bag. The previous mapping is left unchanged."""
    bag = _ValueBag(current)
    bag.update(updates)
    return bag
