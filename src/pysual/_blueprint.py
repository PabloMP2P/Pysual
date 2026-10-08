"""Immutable construction descriptors; live controls remain in their ordinary tree."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import keyword
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Mapping, TypeVar, overload

from .errors import BindingError, LifecycleError

if TYPE_CHECKING:
    from .controls import Container, Control

C = TypeVar("C", bound="Control")


@dataclass(frozen=True, eq=False)
class Blueprint(Generic[C]):
    """A typed per-instance declaration, created with pysual.blueprint()."""

    control_type: type[C]
    _properties: tuple[tuple[str, object], ...]
    parent_name: str | None = None
    _name: str | None = field(default=None, init=False, repr=False)

    @property
    def properties(self) -> Mapping[str, object]:
        return MappingProxyType(dict(self._properties))

    def under(self, parent_name: str) -> Blueprint[C]:
        """Use a Container declaration in the same owner as the visual parent."""
        if (
            not isinstance(parent_name, str)
            or not parent_name.isidentifier()
            or parent_name.startswith("_")
            or keyword.iskeyword(parent_name)
        ):
            raise BindingError("Blueprint parent must be a public declaration name")
        return replace(self, parent_name=parent_name)

    def __set_name__(self, owner: type, name: str) -> None:
        if self._name is not None:
            raise BindingError("Create a fresh blueprint for each class declaration")
        if (
            not name.isidentifier()
            or name.startswith("_")
            or keyword.iskeyword(name)
            or name == owner.__name__
            or any(
                name in vars(base) and not isinstance(vars(base)[name], Blueprint)
                for base in owner.__mro__[1:]
            )
        ):
            raise BindingError(f"Blueprint {name!r} collides with a framework member")
        object.__setattr__(self, "_name", name)

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> Blueprint[C]: ...

    @overload
    def __get__(self, instance: Container, owner: type | None = None) -> C: ...

    def __get__(
        self, instance: Container | None, owner: type | None = None
    ) -> Blueprint[C] | C:
        if instance is None:
            return self
        from ._engine import call, is_ui_thread

        if not is_ui_thread():
            return call(self.__get__, instance, owner)
        instance._check_live()
        assert self._name is not None
        value = vars(instance).get(self._name)
        if not isinstance(value, self.control_type):
            raise LifecycleError(
                f"Blueprint {self._name!r} is available after construction completes"
            )
        return value

    def __set__(self, instance: Container, value: C) -> None:
        if not isinstance(value, self.control_type):
            raise TypeError(
                f"Blueprint {self._name!r} requires {self.control_type.__name__}"
            )
        assert self._name is not None
        vars(instance)[self._name] = value


def declarations(owner_type: type) -> dict[str, Blueprint]:
    """Resolve inherited declarations without evaluating descriptors or user code."""
    result: dict[str, Blueprint] = {}
    for base in reversed(owner_type.__mro__):
        for name, value in vars(base).items():
            if isinstance(value, Blueprint):
                result[name] = value
            elif name in result:
                raise BindingError(
                    f"Override inherited blueprint {name!r} with a blueprint"
                )
    return result
