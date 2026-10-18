"""Lifecycle for disposable derived state, separate from persistent model state."""

from collections.abc import Callable
from typing import ClassVar, cast


__all__ = ("_TransientStateMixin",)


_ObjectState = dict[str, object] | tuple[dict[str, object] | None, dict[str, object]] | None


class _TransientStateMixin:
    """Omit declared caches on copies and restore each with a fresh default.

    Declarations belong to the class that owns the cache and are combined across
    the MRO. Unknown dictionary entries and subclass slots remain ordinary model
    state. Restoration deliberately does not run constructors or ``_initialize``:
    those may build children, register callbacks, or change user model state.
    """

    _transient_fields: ClassVar[dict[str, Callable[[], object]]] = {}
    _transient_obsolete: ClassVar[tuple[str, ...]] = ()

    def _transient_definitions(self) -> tuple[dict[str, Callable[[], object]], set[str]]:
        defaults: dict[str, Callable[[], object]] = {}
        obsolete: set[str] = set()
        for cls in reversed(type(self).__mro__):
            defaults.update(vars(cls).get("_transient_fields", {}))
            obsolete.update(vars(cls).get("_transient_obsolete", ()))
        return defaults, obsolete

    def __getstate__(self) -> _ObjectState:
        state = cast(_ObjectState, object.__getstate__(self))
        defaults, obsolete = self._transient_definitions()
        omitted = defaults.keys() | obsolete

        def persistent(values: dict[str, object] | None) -> dict[str, object] | None:
            if values is None:
                return None
            return {name: value for name, value in values.items() if name not in omitted}

        if isinstance(state, tuple):
            return persistent(state[0]), persistent(state[1]) or {}
        return persistent(state)

    def __setstate__(self, state: _ObjectState) -> None:
        defaults, obsolete = self._transient_definitions()
        omitted = defaults.keys() | obsolete
        dictionary, slots = state if isinstance(state, tuple) else (state, {})
        if dictionary is not None:
            self.__dict__.update(
                (name, value) for name, value in dictionary.items() if name not in omitted
            )
        for name, value in slots.items():
            if name not in omitted:
                setattr(self, name, value)
        for name, factory in defaults.items():
            setattr(self, name, factory())
