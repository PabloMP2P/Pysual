"""Control catalog and optional, backwards-compatible container factory aliases.

The catalog owns registrations independently of Container's Python namespace.
Imports of the control hierarchy stay inside operations so core controls can
re-export these helpers without a circular initialization dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import keyword
import re
from threading import RLock
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, TypeVar

from ._engine import ui_method
from .errors import BindingError, SchemaError

if TYPE_CHECKING:
    from .controls import Container, Control

C = TypeVar("C", bound="Control")


class _Factory(Generic[C]):
    def __init__(self, control_type: type[C], *, authorable: bool = True):
        self.control_type = control_type
        self.authorable = authorable

    def __get__(self, owner: Container | None, cls: type | None = None):
        if owner is None:
            return self

        def factory(**properties: object) -> C:
            return owner.create(self.control_type, **properties)

        factory.__name__ = self.control_type.__name__
        factory.__doc__ = self.control_type.__doc__
        factory.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
            [
                inspect.Parameter(
                    name,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=field.definition.default,
                    annotation=field.annotation,
                )
                for name, field in self.control_type.properties().items()
            ],
            return_annotation=self.control_type,
        )
        return ui_method(factory)


@dataclass(frozen=True)
class _Registration:
    control_type: type[Control]
    authorable: bool
    factory: _Factory | None


_registrations: dict[str, _Registration] = {}
_registry_lock = RLock()


def _container_classes(cls: type[Container]):
    yield cls
    for child in cls.__subclasses__():
        yield from _container_classes(child)


def register_control(
    control_type: type[C], *, name: str | None = None, authorable: bool = True,
    factory: bool = True,
) -> type[C]:
    """Register a control, optionally adding a named Container factory.

    ``factory=False`` makes a control discoverable without reserving a member
    name on every container. Construct it with ``container.create(ControlType,
    ...)`` or a direct constructor. Existing named factories remain the default.
    Re-registering the same type/name updates its authoring and factory policy.
    """
    from .controls import Container, Control

    if (
        not isinstance(control_type, type)
        or not issubclass(control_type, Control)
        or getattr(control_type, "_is_app", False)
    ):
        raise SchemaError("Only Control subclasses can be registered")
    if type(authorable) is not bool:
        raise TypeError("authorable must be a bool")
    if type(factory) is not bool:
        raise TypeError("factory must be a bool")
    if name is None:
        name = re.sub(
            r"([a-z0-9])([A-Z])", r"\1_\2",
            re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", control_type.__name__),
        ).lower()
    if (
        not isinstance(name, str) or not name.isidentifier()
        or name.startswith("_") or keyword.iskeyword(name)
    ):
        raise BindingError("Factory name must be a public Python identifier")

    with _registry_lock:
        previous = _registrations.get(name)
        if previous is not None and previous.control_type is not control_type:
            raise BindingError(
                f"Control {name!r} is already registered; pass name='another_name'"
            )
        installed = inspect.getattr_static(Container, name, None)
        alias = previous.factory if previous is not None else None
        if factory:
            if installed is not alias or alias is None:
                if any(
                    inspect.getattr_static(cls, name, None) is not None
                    for cls in _container_classes(Container)
                ):
                    raise BindingError(
                        f"Factory {name!r} collides with an existing member; "
                        "pass name='another_name'"
                    )
                alias = _Factory(control_type, authorable=authorable)
                setattr(Container, name, alias)
            else:
                alias.authorable = authorable
        else:
            if alias is not None and installed is alias:
                delattr(Container, name)
            alias = None
        _registrations[name] = _Registration(control_type, authorable, alias)
    return control_type


def registered_controls(
    *, authorable_only: bool = False, factories_only: bool = False,
):
    """Return an immutable catalog snapshot, optionally filtered for tooling.

    ``factories_only`` selects actual named Container aliases, for example when
    generating factory signatures. Removing an alias does not unregister the
    control from the catalog; registering it again restores the alias.
    """
    from .controls import Container

    with _registry_lock:
        return MappingProxyType({
            name: registration.control_type
            for name, registration in _registrations.items()
            if (not authorable_only or registration.authorable)
            and (
                not factories_only
                or registration.factory is not None
                and inspect.getattr_static(Container, name, None) is registration.factory
            )
        })
