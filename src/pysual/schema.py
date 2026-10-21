"""Typed immutable property metadata, validation, and one mutation path."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from difflib import get_close_matches
from enum import IntFlag, auto
from functools import cached_property, lru_cache
from math import isfinite
import sys
from types import MappingProxyType, UnionType
from weakref import ReferenceType, ref
from typing import (
    Any,
    ClassVar,
    Literal,
    Mapping,
    Union,
    TypeVar,
    cast,
    dataclass_transform,
    get_args,
    get_origin,
    get_type_hints,
)

if sys.version_info >= (3, 14):
    from annotationlib import Format, get_annotations

from .errors import LifecycleError, SchemaError
from ._value_bag import _ValueBag, _replaced_bag
from ._transient import _TransientStateMixin
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
# DataGrid: 200,000,000 cells + three visited values per 2,000,000 rows
# + the outer collection (206,000,001). The cap stays above that count.
_MAX_RECORD_VALUES = 210_000_000
_RECORD_NAMES = frozenset(
    (
        "pysual.tree.TreeNode",
        "pysual.data_grid.GridColumn",
        "pysual.data_grid.GridRow",
        "pysual.charts.ChartSeries",
        "pysual.charts.ChartSlice",
    )
)


_Record = TypeVar("_Record", bound=type)
_Self = TypeVar("_Self", bound="SchemaObject")


def _persist_record(cls: _Record) -> _Record:
    """Opt in only the five built-in data records; JSON never loads a class."""
    if f"{cls.__module__}.{cls.__qualname__}" not in _RECORD_NAMES:
        raise SchemaError("Only built-in control data has a record codec")
    _PERSISTENT_RECORDS.add(cls)
    return cls


@lru_cache(maxsize=5)
def _record_annotations(cls: type) -> dict[str, Any]:
    # Resolve recursive TreeNode annotations after its module has been loaded.
    return get_type_hints(cls)


def _has_record(annotation: Any) -> bool:
    return annotation in _PERSISTENT_RECORDS or any(
        _has_record(arg) for arg in get_args(annotation) if arg is not Ellipsis
    )


def _record_data(value: Any, annotation: Any, *, decode: bool) -> Any:
    """Bounded, schema-directed JSON records with ordinary constructor validation."""
    visited = 0

    def convert(value: Any, annotation: Any, depth: int = 0) -> Any:
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
            sequence = cast(Sequence[Any], value)
            repeated = len(args) == 2 and args[1] is Ellipsis
            if not repeated and len(sequence) != len(args):
                raise ValueError("Control data tuple has the wrong number of values")
            items = [
                convert(item, args[0] if repeated else args[index], depth + 1)
                for index, item in enumerate(sequence)
            ]
            return tuple(items) if decode else items
        if annotation in _PERSISTENT_RECORDS:
            annotations = _record_annotations(annotation)
            if decode:
                if not isinstance(value, dict):
                    raise ValueError(f"Expected {annotation.__name__} fields only")
                record = cast(dict[Any, Any], value)
                if record.keys() - annotations.keys():
                    raise ValueError(f"Expected {annotation.__name__} fields only")
                return annotation(
                    **{
                        name: convert(item, annotations[name], depth + 1)
                        for name, item in record.items()
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


def _mask(affects: Dirty | int) -> int:
    return int(affects)


def _dynamic_properties(properties: Mapping[str, Any]) -> Any:
    """Forward a property bag into a constructor the schema validates at runtime.

    Static constructors list every field. A dict built from ``_values`` or from
    ``**properties: object`` cannot name those fields ahead of time, so this
    returns ``Any``. Names are checked here; the constructor checks values.
    """
    if any(not isinstance(name, str) for name in properties):
        raise TypeError("Property names must be strings")
    return properties


def expect_flag(value: object) -> bool:
    """Return a schema bool.

    Other truthy values are rejected. On the public property path the schema
    has already checked the field; a hook uses this so the checker sees ``bool``.
    """
    if not isinstance(value, bool):
        raise TypeError("Expected a bool")
    return value


def expect_number(value: object) -> int | float:
    """Return a schema number.

    Integers are accepted for float fields. ``bool`` is not a number. On the
    public property path the schema has already checked the field; a hook uses
    this so the checker sees ``int | float``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("Expected a number")
    return value


def expect_text(value: object) -> str:
    """Return schema text.

    On the public property path the schema has already checked the field; a
    hook uses this so the checker sees ``str``.
    """
    if not isinstance(value, str):
        raise TypeError("Expected text")
    return value


class _Pending:
    """Committed properties plus one uncommitted replacement.

    The view reads the existing bag and overlays the field under test. It does
    not copy the bag: construction validates every field, and a copy per field
    dominated scene build.
    """

    __slots__ = ("_base", "_name", "_replacement")

    def __init__(self, values: Mapping[str, object], name: str, value: object) -> None:
        self._base = values
        self._name = name
        self._replacement = value

    def _get(self, name: str) -> object:
        if name == self._name:
            return self._replacement
        return self._base[name]

    def number(self, name: str) -> int | float:
        return expect_number(self._get(name))

    def optional_number(self, name: str) -> int | float | None:
        value = self._get(name)
        if value is None:
            return None
        return expect_number(value)

    def text(self, name: str) -> str:
        return expect_text(self._get(name))

    def flag(self, name: str) -> bool:
        return expect_flag(self._get(name))

    def number_pair(self, name: str) -> tuple[int | float, int | float]:
        value = self._get(name)
        if not isinstance(value, tuple):
            raise TypeError(f"{name} must be a pair of numbers")
        pair = cast(tuple[object, ...], value)
        if len(pair) != 2:
            raise TypeError(f"{name} must be a pair of numbers")
        return expect_number(pair[0]), expect_number(pair[1])

    def texts(self, name: str) -> tuple[str, ...]:
        value = self._get(name)
        if not isinstance(value, tuple):
            raise TypeError(f"{name} must be a tuple of text")
        items = cast(tuple[object, ...], value)
        if any(not isinstance(item, str) for item in items):
            raise TypeError(f"{name} must be a tuple of text")
        return cast(tuple[str, ...], items)


_T = TypeVar("_T")


def _records(value: object, kind: type[_T]) -> tuple[_T, ...]:
    if not isinstance(value, tuple):
        raise TypeError(f"Expected {kind.__name__} records")
    items = cast(tuple[object, ...], value)
    if any(not isinstance(item, kind) for item in items):
        raise TypeError(f"Expected {kind.__name__} records")
    return cast(tuple[_T, ...], items)


def _sequence(value: object) -> tuple[object, ...]:
    if not isinstance(value, tuple):
        raise TypeError("Expected a tuple")
    return cast(tuple[object, ...], value)


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
    choices: tuple[Any, ...] = ()
    changed: str | None = None
    persist: bool = True


def prop(
    *,
    default: object,
    affects: Dirty = Dirty.PAINT,
    minimum: float | None = None,
    doc: str = "",
    choices: tuple[Any, ...] = (),
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


_FROZEN_FIELDS: dict[
    int, tuple[ReferenceType[type[object]], tuple[str, ...] | None]
] = {}
_SCALAR = 0
_UNION = 1
_LITERAL = 2
_REPEAT = 3
_FIXED = 4
_RECORD = 5
_BAD = 6


def _frozen_fields(kind: type[object]) -> tuple[str, ...] | None:
    """Reuse field names without extending a class's lifetime."""
    identity = id(kind)
    slot = _FROZEN_FIELDS.get(identity)
    if slot is not None and slot[0]() is kind:
        return slot[1]
    names = None
    if is_dataclass(kind) and getattr(kind, "__dataclass_params__").frozen:
        names = tuple(f.name for f in fields(kind))
    def discard(dead: ReferenceType[type[object]]) -> None:
        current = _FROZEN_FIELDS.get(identity)
        if current is not None and current[0] is dead:
            del _FROZEN_FIELDS[identity]

    # WeakKeyDictionary still hashes and compares its keys. Identity keys also
    # support classes with unhashable or value-comparing metaclasses.
    _FROZEN_FIELDS[identity] = (ref(kind, discard), names)
    return names


def _immutable(value: object) -> bool:
    if value is None or type(value) in (str, bool, int, float):
        return True
    if isinstance(value, tuple):
        return all(_immutable(item) for item in cast(tuple[object, ...], value))
    names = _frozen_fields(type(value))
    if names is None:
        return False
    return all(_immutable(getattr(value, name)) for name in names)


def _compile(annotation: Any) -> tuple[object, ...]:
    """Build a field-owned checker; hot checks skip get_origin/get_args."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (Union, UnionType):
        compiled: tuple[object, ...] = (_UNION, tuple(_compile(arg) for arg in args))
    elif origin is Literal:
        compiled = (_LITERAL, args)
    elif origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            compiled = (_REPEAT, _compile(args[0]))
        else:
            compiled = (_FIXED, tuple(_compile(arg) for arg in args))
    elif annotation in (str, bool, int, float, type(None)):
        compiled = (_SCALAR, annotation)
    elif (
        isinstance(annotation, type)
        and is_dataclass(annotation)
        and getattr(annotation, "__dataclass_params__").frozen
    ):
        compiled = (_RECORD, annotation)
    else:
        compiled = (_BAD, cast(object, annotation))
    return compiled


def _accept(value: object, checker: tuple[object, ...]) -> bool:
    if type(value) is float and not isfinite(value):
        raise ValueError("Property numbers must be finite")
    kind = checker[0]
    if kind == _SCALAR:
        expected = checker[1]
        return type(value) is expected or (expected is float and type(value) is int)
    if kind == _UNION:
        return any(_accept(value, option) for option in cast(tuple[tuple[object, ...], ...], checker[1]))
    if kind == _LITERAL:
        # bool and int compare equal in Python, but describe distinct choices.
        return any(
            type(value) is type(choice) and value == choice
            for choice in cast(tuple[object, ...], checker[1])
        )
    if kind == _REPEAT or kind == _FIXED:
        if not isinstance(value, tuple):
            return False
        items = cast(tuple[object, ...], value)
        if kind == _REPEAT:
            item = cast(tuple[object, ...], checker[1])
            return all(_accept(item_value, item) for item_value in items)
        options = cast(tuple[tuple[object, ...], ...], checker[1])
        return len(options) == len(items) and all(
            _accept(item_value, option) for item_value, option in zip(items, options)
        )
    if kind == _RECORD:
        if not _immutable(value):
            raise SchemaError("Property dataclasses must contain only immutable values")
        return isinstance(value, cast(type[object], checker[1]))
    raise SchemaError(f"Unsupported property type {checker[1]!r}; use immutable values")


def valid_type(value: object, annotation: Any) -> bool:
    return _accept(value, _compile(annotation))


@dataclass(frozen=True)
class Field:
    name: str
    annotation: object
    definition: Property

    @cached_property
    def _checker(self) -> tuple[object, ...]:
        return _compile(self.annotation)

    @property
    def serializable(self) -> bool:
        def supported(annotation: Any) -> bool:
            return (
                annotation in _PERSISTENT_RECORDS
                or annotation in (str, bool, int, float, type(None))
                or (
                    get_origin(annotation) is Literal
                    and all(type(value) in (str, bool, int, type(None))
                            for value in get_args(annotation))
                )
                or (
                    get_origin(annotation) in (tuple, Union, UnionType)
                    and all(t is Ellipsis or supported(t) for t in get_args(annotation))
                )
            )

        return self.definition.persist and supported(self.annotation)

    def encode(self, value: object) -> object:
        """Encode only supported immutable JSON values, never arbitrary objects."""
        if not self.serializable:
            raise ValueError(f"{self.name} does not have a persistence codec")
        if _has_record(self.annotation):
            result = _record_data(value, self.annotation, decode=False)
            self.validate(value)
            return result
        self.validate(value)

        def convert(item: object) -> object:
            if not isinstance(item, tuple):
                return item
            return [convert(child) for child in cast(tuple[object, ...], item)]

        return convert(value)

    def decode(self, value: object) -> object:
        if not self.serializable:
            raise ValueError(f"{self.name} does not have a persistence codec")
        if _has_record(self.annotation):
            result = _record_data(value, self.annotation, decode=True)
            self.validate(result)
            return result

        def convert(item: object) -> object:
            if not isinstance(item, list):
                return item
            return tuple(convert(child) for child in cast(list[object], item))

        result = convert(value)
        self.validate(result)
        return result

    def validate(self, value: object, *, previous: object = _UNVALIDATED) -> None:
        # Unchanged immutable tuple entries were already validated by this field.
        # Reuse that proof by identity; retain no cache or second copy of the data.
        checker = self._checker
        if value is previous:
            valid = True
        elif (
            checker[0] == _REPEAT
            and isinstance(value, tuple)
            and isinstance(previous, tuple)
        ):
            current = cast(tuple[object, ...], value)
            prior = cast(tuple[object, ...], previous)
            item = cast(tuple[object, ...], checker[1])
            valid = all(
                (i < len(prior) and entry is prior[i]) or _accept(entry, item)
                for i, entry in enumerate(current)
            )
        else:
            valid = _accept(cast(object, value), checker)
        subject = cast(object, value)
        if not valid:
            raise TypeError(
                f"{self.name}: expected {self.annotation!r}, got {type(subject).__name__}"
            )
        if self.definition.choices and subject not in self.definition.choices:
            raise ValueError(f"{self.name}: expected one of {self.definition.choices}")
        if self.definition.minimum is not None and subject is not None:
            # bool is an int subclass; type() keeps it out of numeric fields.
            if type(subject) is int or type(subject) is float:
                if subject < self.definition.minimum:
                    raise ValueError(
                        f"{self.name}: value must be >= {self.definition.minimum}"
                    )
            else:
                raise SchemaError(f"{self.name}: minimum requires a numeric property")


def _literal_choices(annotation: Any) -> tuple[Any, ...]:
    """Validate choice declarations even inside empty tuples or optional fields."""
    if get_origin(annotation) is Literal:
        choices = get_args(annotation)
        kinds = {cast(type[Any], type(item)) for item in choices}
        if (
            not kinds
            or any(kind not in (str, int, bool, type(None)) for kind in kinds)
            or (bool in kinds and int in kinds)
        ):
            raise SchemaError("Literal properties require str, int, bool or None choices; do not mix bool and int")
        return choices
    for argument in get_args(annotation):
        if argument is not Ellipsis:
            _literal_choices(argument)
    return ()


def _declared_field(name: str, annotation: Any, definition: Property) -> Field:
    """Lower top-level choices while retaining nested Literal constraints.

    Keeping primitive runtime annotations preserves persistence, inherited
    declarations and the scalar access path. Literal is the sole choice list.
    Legacy primitive annotations with explicit choices remain supported.
    """
    choices = _literal_choices(annotation)
    if choices:
        if definition.choices:
            raise SchemaError("Declare Literal choices in the annotation, not choices=")
        kinds: tuple[type[Any], ...] = tuple(
            dict.fromkeys(cast(type[Any], type(item)) for item in choices)
        )
        annotation = kinds[0]
        for kind in kinds[1:]:
            annotation = annotation | kind
        definition = replace(definition, choices=choices)
    return Field(name, annotation, definition)


def _plain_live(obj: SchemaObject) -> bool:
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
    def __init__(self, field: Field) -> None:
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
            obj._record_change(self.field, old, value)
            obj._changed(self.field, old, value)


@dataclass_transform(
    kw_only_default=True, eq_default=False, field_specifiers=(prop, _init_argument)
)
class SchemaObject(_TransientStateMixin):
    _schema: ClassVar[Mapping[str, Field]] = MappingProxyType({})
    # Heterogeneous committed properties. The bag's stub types known keys, and
    # reads use dict's own lookup rather than a cast on every access.
    _values: _ValueBag

    @property
    def _visual_epoch(self) -> int:
        return self._values._epoch

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        fields: dict[str, Field] = {}
        for base in cls.__bases__:
            for name, field in getattr(base, "_schema", {}).items():
                if name in fields and fields[name] != field:
                    raise SchemaError(f"Conflicting inherited field {name!r}")
                fields[name] = field
        declarations = {k: v for k, v in vars(cls).items() if isinstance(v, Property)}
        if sys.version_info >= (3, 14):
            # Native deferred annotations may contain names deliberately absent
            # at runtime. Keep those as forward refs until fields are selected.
            own_annotations = get_annotations(cls, format=Format.FORWARDREF)
        else:
            own_annotations = cls.__annotations__
        for name in declarations:
            if name.startswith("_") or name not in own_annotations:
                raise SchemaError(f"Property {name!r} needs a public annotated name")
        # Runtime schema resolution concerns only this class's declarations.
        # Unrelated state annotations may refer to types that are not imported
        # at runtime; inherited fields already carry resolved annotations.
        annotations: dict[str, Any] = {}
        if declarations:
            declared = type("_PropertyAnnotations", (), {
                "__module__": cls.__module__,
                "__annotations__": {
                    name: own_annotations[name] for name in declarations
                },
            })
            annotations = get_type_hints(declared, localns=dict(vars(cls)))
        for name, definition in declarations.items():
            if name not in fields and any(name in vars(b) for b in cls.__mro__[1:]):
                raise SchemaError(f"Property {name!r} collides with a framework member")
            field = _declared_field(name, annotations[name], definition)
            if name in fields and fields[name].annotation != field.annotation:
                raise SchemaError(f"Inherited property {name!r} cannot change type")
            field.validate(definition.default)
            fields[name] = field
            setattr(cls, name, _Slot(field))
        for name in fields.keys() - declarations.keys():
            if name in vars(cls) or name in own_annotations:
                raise SchemaError(
                    f"Override {name!r} with prop(), not a plain attribute"
                )
        cls._schema = MappingProxyType(fields)

    def __init__(self, **properties: object) -> None:
        check_ui_thread()
        unknown = properties.keys() - self._schema.keys()
        if unknown:
            raise TypeError(unknown_name(sorted(unknown)[0], self._schema))
        values: dict[str, object] = {}
        for name, field in self._schema.items():
            value = properties.get(name, field.definition.default)
            field.validate(value)
            values[name] = value
        bag = _ValueBag(values)
        self._values = bag
        self._dirty = 0
        self._disposed = False
        self._initialize()
        # Initial normalization is part of the initial committed state.
        self._values._epoch = 0
        for name, value in self._values.items():
            self._validate_live_update(name, value)
            self._validate_update(name, value)

    def _initialize(self) -> None:
        pass

    def _prepare_update(self, values: Mapping[str, object]) -> Mapping[str, object]:
        return values

    def _pending(self, name: str, value: object) -> _Pending:
        """Read every field as it would be after assigning ``name``.

        The returned view is not committed. Use it from ``_validate_update``
        when a check spans more than the field being assigned.
        """
        return _Pending(self._values, name, value)

    def _validation_copy(self: _Self, values: Mapping[str, object]) -> _Self:
        """Build an unmounted candidate view for the existing validation hook."""
        candidate = object.__new__(type(self))
        candidate.__dict__.update(self.__dict__)
        candidate._values = _replaced_bag(self._values, values)
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
        changes: list[tuple[Field, object, object]] = []
        for name, value in values.items():
            field = self._schema[name]
            old = self._values[name]
            field.validate(value, previous=old)
            if old != value:
                changes.append((field, old, value))
        if not changes:
            return
        self._values = _replaced_bag(self._values, values)
        self._record_changes(changes)
        # A notification can measure or arrange immediately. All committed
        # fields must have invalidated their dependents before the first hook.
        for field, old, value in changes:
            self._changed(field, old, value)

    def _record_changes(self, changes: list[tuple[Field, object, object]]) -> None:
        """Account for an atomic commit before delivering its notifications."""
        for field, old, value in changes:
            self._record_change(field, old, value)

    def _record_change(self, field: Field, old: object, value: object) -> None:
        """Internal commit accounting, separate from overridable notifications."""
        self._record_dirty(field.definition.affects)

    def _record_dirty(self, affects: Dirty) -> None:
        """Record committed property dirt before overridable notifications."""
        self._dirty = int(self._dirty) | _mask(affects)

    def _validate_live_update(self, name: str, value: object) -> None:
        """Check identity and lifecycle limits against the committed object.

        Unlike ``_validate_update``, this hook never runs on a batch candidate.
        Keep coordinated value invariants in ``_validate_update`` instead.
        ``value`` is ``object`` because one hook receives every property.
        Narrow it with ``isinstance`` before using it.
        """
        pass

    def _validate_update(self, name: str, value: object) -> None:
        pass

    def _changed(self, field: Field, old: object, value: object) -> None:
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
