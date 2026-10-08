"""Typed immutable property metadata, validation, and one mutation path."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields, is_dataclass, replace
from difflib import get_close_matches
from enum import IntFlag, auto
from functools import lru_cache
from math import isfinite
from types import MappingProxyType, UnionType
from typing import (
    Any,
    ClassVar,
    Literal,
    Mapping,
    Union,
    dataclass_transform,
    get_args,
    get_origin,
    get_type_hints,
)

from .errors import LifecycleError, SchemaError
from ._engine import (
    call,
    check_origin,
    check_ui_thread,
    critical_phase,
    is_ui_thread,
    ui_method,
    ui_operation,
)

_UNVALIDATED = object()
_PERSISTENT_RECORDS: set[type] = set()
# 200,000 scalar grid cells + three visited values per row + outer collection.
_MAX_RECORD_VALUES = 300_000
_RECORD_NAMES = frozenset(
    (
        "pysual.tree.TreeNode",
        "pysual.data_grid.GridColumn",
        "pysual.data_grid.GridRow",
        "pysual.charts.ChartSeries",
        "pysual.charts.ChartSlice",
    )
)


def _persist_record(cls):
    """Opt in only the five built-in data records; JSON never loads a class."""
    if f"{cls.__module__}.{cls.__qualname__}" not in _RECORD_NAMES:
        raise SchemaError("Only built-in control data has a record codec")
    _PERSISTENT_RECORDS.add(cls)
    return cls


@lru_cache(maxsize=5)
def _record_annotations(cls):
    # Resolve recursive TreeNode annotations after its module has been loaded.
    return get_type_hints(cls)


def _has_record(annotation):
    return annotation in _PERSISTENT_RECORDS or any(
        _has_record(arg) for arg in get_args(annotation) if arg is not Ellipsis
    )


def _record_data(value, annotation, *, decode):
    """Bounded, schema-directed JSON records with ordinary constructor validation."""
    visited = 0

    def convert(value, annotation, depth=0):
        nonlocal visited
        if depth > 24 or visited >= _MAX_RECORD_VALUES:
            raise ValueError(
                "Control data codec allows at most 24 nested levels and "
                f"{_MAX_RECORD_VALUES:,} values"
            )
        origin, args = get_origin(annotation), get_args(annotation)
        if origin in (Union, UnionType):
            start = visited
            for choice in args:
                visited = start
                try:
                    return convert(value, choice, depth)
                except (ValueError, TypeError):
                    pass
            raise TypeError("Control data does not match its declared value type")
        visited += 1
        if origin is tuple:
            if not isinstance(value, list if decode else tuple):
                raise TypeError("Control data collections must be JSON arrays")
            repeated = len(args) == 2 and args[1] is Ellipsis
            if not repeated and len(value) != len(args):
                raise ValueError("Control data tuple has the wrong number of values")
            items = [
                convert(item, args[0] if repeated else args[index], depth + 1)
                for index, item in enumerate(value)
            ]
            return tuple(items) if decode else items
        if annotation in _PERSISTENT_RECORDS:
            annotations = _record_annotations(annotation)
            if decode:
                if not isinstance(value, dict) or value.keys() - annotations.keys():
                    raise ValueError(f"Expected {annotation.__name__} fields only")
                return annotation(
                    **{
                        name: convert(item, annotations[name], depth + 1)
                        for name, item in value.items()
                    }
                )
            if type(value) is not annotation:
                raise TypeError(f"Expected built-in {annotation.__name__}")
            return {
                name: convert(getattr(value, name), kind, depth + 1)
                for name, kind in annotations.items()
            }
        if origin is Literal:
            valid = any(type(value) is type(item) and value == item for item in args)
        else:
            valid = valid_type(value, annotation)
        if not valid:
            raise TypeError("Control data does not match its declared value type")
        if isinstance(value, str) and len(value) > 4096:
            raise ValueError("Control data strings must be at most 4096 characters")
        return value

    return convert(value, annotation)


class Dirty(IntFlag):
    NONE = 0
    MEASURE = auto()
    ARRANGE = auto()
    PAINT = auto()
    HIT_TEST = auto()


# Hot paths combine these as plain ints. Dirty is rebuilt only at the public boundary.
_LAYOUT_MASK = int(Dirty.MEASURE | Dirty.ARRANGE)
_VISUAL_MASK = int(Dirty.MEASURE | Dirty.ARRANGE | Dirty.PAINT)
_HIT_MASK = int(Dirty.HIT_TEST)


def _mask(affects) -> int:
    return int(affects)


def unknown_name(name: str, choices: Iterable[str]) -> str:
    matches = get_close_matches(name, choices, n=1)
    return f"Unknown property {name!r}" + (
        f"; did you mean {matches[0]!r}?" if matches else ""
    )


@dataclass(frozen=True)
class Property:
    default: object
    affects: Dirty = Dirty.PAINT
    minimum: float | None = None
    doc: str = ""
    choices: tuple = ()
    changed: str | None = None
    persist: bool = True


def prop(
    *,
    default: object,
    affects: Dirty = Dirty.PAINT,
    minimum: float | None = None,
    doc: str = "",
    choices: tuple = (),
    changed: str | None = None,
    persist: bool = True,
) -> Any:
    """A typing field specifier; Any is confined to declaration syntax."""
    return Property(default, affects, minimum, doc, choices, changed, persist)


def _init_argument(
    *, alias: str, default: object = _UNVALIDATED, kw_only: bool = True,
) -> Any:
    """Typing-only constructor input, independent of persistent property fields."""
    return default


def _immutable(value):
    if value is None or type(value) in (str, bool, int, float):
        return True
    if isinstance(value, tuple):
        return all(_immutable(v) for v in value)
    if (
        is_dataclass(value)
        and not isinstance(value, type)
        and getattr(type(value), "__dataclass_params__").frozen
    ):
        return all(_immutable(getattr(value, f.name)) for f in fields(value))
    return False


def valid_type(value, annotation):
    if type(value) is float and not isfinite(value):
        raise ValueError("Property numbers must be finite")
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        return any(valid_type(value, t) for t in args)
    if origin is tuple:
        if not isinstance(value, tuple):
            return False
        if len(args) == 2 and args[1] is Ellipsis:
            return all(valid_type(v, args[0]) for v in value)
        return len(args) == len(value) and all(
            valid_type(v, t) for v, t in zip(value, args)
        )
    if annotation in (str, bool, int, float, type(None)):
        return type(value) is annotation or (annotation is float and type(value) is int)
    if (
        isinstance(annotation, type)
        and is_dataclass(annotation)
        and getattr(annotation, "__dataclass_params__").frozen
    ):
        if not _immutable(value):
            raise SchemaError("Property dataclasses must contain only immutable values")
        return isinstance(value, annotation)
    raise SchemaError(f"Unsupported property type {annotation!r}; use immutable values")


@dataclass(frozen=True)
class Field:
    name: str
    annotation: object
    definition: Property

    @property
    def serializable(self) -> bool:
        def supported(annotation):
            return (
                annotation in _PERSISTENT_RECORDS
                or annotation in (str, bool, int, float, type(None))
                or (
                    get_origin(annotation) in (tuple, Union, UnionType)
                    and all(t is Ellipsis or supported(t) for t in get_args(annotation))
                )
            )

        return self.definition.persist and supported(self.annotation)

    def encode(self, value):
        """Encode only supported immutable JSON values, never arbitrary objects."""
        if not self.serializable:
            raise ValueError(f"{self.name} does not have a persistence codec")
        if _has_record(self.annotation):
            result = _record_data(value, self.annotation, decode=False)
            self.validate(value)
            return result
        self.validate(value)

        def convert(v):
            return [convert(x) for x in v] if isinstance(v, tuple) else v

        return convert(value)

    def decode(self, value):
        if not self.serializable:
            raise ValueError(f"{self.name} does not have a persistence codec")
        if _has_record(self.annotation):
            result = _record_data(value, self.annotation, decode=True)
            self.validate(result)
            return result

        def convert(v):
            return tuple(convert(x) for x in v) if isinstance(v, list) else v

        result = convert(value)
        self.validate(result)
        return result

    def validate(self, value: object, *, previous: object = _UNVALIDATED) -> None:
        # Unchanged immutable tuple entries were already validated by this field.
        # Reuse that proof by identity; retain no cache or second copy of the data.
        args = get_args(self.annotation)
        if value is previous:
            valid = True
        elif (
            isinstance(value, tuple)
            and isinstance(previous, tuple)
            and get_origin(self.annotation) is tuple
            and len(args) == 2
            and args[1] is Ellipsis
        ):
            valid = all(
                (i < len(previous) and item is previous[i]) or valid_type(item, args[0])
                for i, item in enumerate(value)
            )
        else:
            valid = valid_type(value, self.annotation)
        if not valid:
            raise TypeError(
                f"{self.name}: expected {self.annotation!r}, got {type(value).__name__}"
            )
        if self.definition.choices and value not in self.definition.choices:
            raise ValueError(f"{self.name}: expected one of {self.definition.choices}")
        if self.definition.minimum is not None and value is not None:
            if type(value) not in (int, float):
                raise SchemaError(f"{self.name}: minimum requires a numeric property")
            if value < self.definition.minimum:  # type: ignore[operator]
                raise ValueError(
                    f"{self.name}: value must be >= {self.definition.minimum}"
                )


def _declared_field(name, annotation, definition):
    """Lower finite typing choices to the existing runtime schema contract.

    Keeping primitive runtime annotations preserves persistence, inherited
    declarations and the scalar access path. Literal is the sole choice list.
    Legacy primitive annotations with explicit choices remain supported.
    """
    if get_origin(annotation) is Literal:
        choices = get_args(annotation)
        kinds = tuple(dict.fromkeys(type(choice) for choice in choices))
        if (
            not kinds
            or any(kind not in (str, int, bool, type(None)) for kind in kinds)
            or (bool in kinds and int in kinds)
        ):
            raise SchemaError("Literal properties require str, int, bool or None choices; do not mix bool and int")
        if definition.choices:
            raise SchemaError("Declare Literal choices in the annotation, not choices=")
        annotation = kinds[0]
        for kind in kinds[1:]:
            annotation = annotation | kind
        definition = replace(definition, choices=choices)
    return Field(name, annotation, definition)


def _plain_live(obj) -> bool:
    """Decide whether a worker thread may read a committed value directly.

    Property values are immutable and `_values` is only replaced or updated by
    the owner, so one dict read is atomic. Classes that customize attribute
    access or liveness are dispatched to the owner instead. Lifetime and origin
    checks mirror `_check_live` using plain attribute reads only.
    """
    cls = type(obj)
    if cls._check_live is not SchemaObject._check_live or "_check_live" in obj.__dict__:
        return False
    node = obj
    while node is not None:
        if type(node).__getattribute__ is not object.__getattribute__:
            return False
        node = node.__dict__.get("_parent")
    check_origin(obj)
    if obj._disposed:
        raise LifecycleError("Object has been disposed")
    return True


class _Slot:
    def __init__(self, field: Field):
        self.field = field

    @ui_operation
    def __get__(self, obj: SchemaObject | None, owner: type | None = None) -> object:
        if obj is None:
            return self.field.definition.default
        if not is_ui_thread():
            if _plain_live(obj):
                return obj._values[self.field.name]
            return call(self.__get__, obj, owner)
        obj._check_live()
        return obj._values[self.field.name]

    @ui_operation
    def __set__(self, obj: SchemaObject, value: object) -> None:
        if not is_ui_thread():
            return call(self.__set__, obj, value)
        obj._check_live()
        old = obj._values[self.field.name]
        with critical_phase("validation"):
            self.field.validate(value, previous=old)
            obj._validate_live_update(self.field.name, value)
            obj._validate_update(self.field.name, value)
        if old != value:
            obj._values[self.field.name] = value
            obj._record_dirty(self.field.definition.affects)
            obj._changed(self.field, old, value)


@dataclass_transform(
    kw_only_default=True, eq_default=False, field_specifiers=(prop, _init_argument)
)
class SchemaObject:
    _schema: ClassVar[Mapping[str, Field]] = MappingProxyType({})

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        fields: dict[str, Field] = {}
        for base in cls.__bases__:
            for name, field in getattr(base, "_schema", {}).items():
                if name in fields and fields[name] != field:
                    raise SchemaError(f"Conflicting inherited field {name!r}")
                fields[name] = field
        declarations = {k: v for k, v in vars(cls).items() if isinstance(v, Property)}
        annotations = get_type_hints(cls)
        for name, definition in declarations.items():
            if name.startswith("_") or name not in cls.__annotations__:
                raise SchemaError(f"Property {name!r} needs a public annotated name")
            if name not in fields and any(name in vars(b) for b in cls.__mro__[1:]):
                raise SchemaError(f"Property {name!r} collides with a framework member")
            field = _declared_field(name, annotations[name], definition)
            if name in fields and fields[name].annotation != field.annotation:
                raise SchemaError(f"Inherited property {name!r} cannot change type")
            field.validate(definition.default)
            fields[name] = field
            setattr(cls, name, _Slot(field))
        for name in fields.keys() - declarations.keys():
            if name in vars(cls) or name in vars(cls).get("__annotations__", {}):
                raise SchemaError(
                    f"Override {name!r} with prop(), not a plain attribute"
                )
        cls._schema = MappingProxyType(fields)

    def __init__(self, **properties: object) -> None:
        check_ui_thread()
        unknown = properties.keys() - self._schema.keys()
        if unknown:
            raise TypeError(unknown_name(sorted(unknown)[0], self._schema))
        values = {
            name: properties.get(name, field.definition.default)
            for name, field in self._schema.items()
        }
        for name, value in values.items():
            self._schema[name].validate(value)
        self._values = values
        self._dirty = 0
        self._disposed = False
        self._initialize()
        for name, value in values.items():
            self._validate_live_update(name, value)
            self._validate_update(name, value)

    def _initialize(self) -> None:
        pass

    def _prepare_update(self, values: Mapping[str, object]) -> Mapping[str, object]:
        return values

    def _validation_copy(self, values: Mapping[str, object]):
        """Build an unmounted candidate view for the existing validation hook."""
        candidate = object.__new__(type(self))
        candidate.__dict__.update(self.__dict__)
        candidate._values = {**self._values, **values}
        return candidate

    def _validate_candidate(self, values: Mapping[str, object]) -> None:
        self._check_live()
        unknown = values.keys() - self._schema.keys()
        if unknown:
            raise TypeError(unknown_name(sorted(unknown)[0], self._schema))
        with critical_phase("validation"):
            for name, value in values.items():
                self._schema[name].validate(value, previous=self._values[name])
            for name, value in values.items():
                self._validate_live_update(name, value)
            candidate = self._validation_copy(values)
            for name, value in values.items():
                candidate._validate_update(name, value)

    @ui_method
    def update(self, **properties: object) -> None:
        """Validate and commit properties together; callbacks see the complete result."""
        properties = dict(self._prepare_update(properties))
        self._validate_candidate(properties)
        self._apply_validated(properties)

    def _apply_validated(self, values: Mapping[str, object]) -> None:
        """Commit preflighted dependent fields before ordinary notifications.

        Internal callers must validate cross-field invariants and prepare derived
        state first. Field validation still happens here, before any mutation;
        `_changed` sees the complete values and retains normal dirty/event rules.
        """
        self._check_live()
        changes = []
        for name, value in values.items():
            field = self._schema[name]
            old = self._values[name]
            field.validate(value, previous=old)
            if old != value:
                changes.append((field, old, value))
        if not changes:
            return
        self._values = {**self._values, **values}
        for field, old, value in changes:
            self._record_dirty(field.definition.affects)
            self._changed(field, old, value)

    def _record_dirty(self, affects: Dirty) -> None:
        """Record committed property dirt before overridable notifications."""
        self._dirty = int(self._dirty) | _mask(affects)

    def _validate_live_update(self, name, value) -> None:
        """Check identity/lifecycle restrictions against the committed object.

        Unlike `_validate_update`, this hook never runs on a batch candidate.
        Keep coordinated value invariants in `_validate_update` instead.
        """
        pass

    def _validate_update(self, name, value) -> None:
        pass

    def _changed(self, field, old, value) -> None:
        pass

    def _check_live(self) -> None:
        check_ui_thread()
        check_origin(self)
        if self._disposed:
            raise LifecycleError("Object has been disposed")

    @classmethod
    def properties(cls) -> Mapping[str, Field]:
        return cls._schema

    @property
    @ui_method
    def dirty(self) -> Dirty:
        self._check_live()
        value = self._dirty
        return value if type(value) is Dirty else Dirty(int(value))
