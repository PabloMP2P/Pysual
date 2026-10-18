"""Validation metadata must not keep temporary annotations alive."""

import gc
import weakref
from dataclasses import make_dataclass

import pytest

from pysual.errors import SchemaError
from pysual.schema import Field, Property, valid_type


def test_standalone_validation_releases_dynamic_record_types():
    references = []
    for index in range(1000):
        record = make_dataclass(f"Record{index}", [("value", int)], frozen=True)
        assert valid_type(record(1), record)
        references.append(weakref.ref(record))
    del record
    gc.collect()

    assert all(reference() is None for reference in references)


def test_standalone_validation_releases_fresh_generic_aliases():
    references = []
    for _ in range(1000):
        annotation = tuple[int, ...]
        assert valid_type((1, 2), annotation)
        references.append(weakref.ref(annotation))
    del annotation
    gc.collect()

    assert all(reference() is None for reference in references)


def test_field_checker_lifetime_follows_its_field():
    record = make_dataclass("Record", [("value", int)], frozen=True)
    field = Field("record", record, Property(default=None, persist=False))
    field.validate(record(1))
    reference = weakref.ref(record)
    del record
    gc.collect()
    assert reference() is not None

    del field
    gc.collect()
    assert reference() is None


def test_frozen_field_names_use_identity_for_equal_unhashable_classes():
    class EqualType(type):
        __hash__ = None

        def __eq__(self, other):
            return isinstance(other, EqualType)

    class Base(metaclass=EqualType):
        pass

    first = make_dataclass("First", [("first", int)], bases=(Base,), frozen=True)
    second = make_dataclass("Second", [("second", int)], bases=(Base,), frozen=True)
    assert first == second
    assert valid_type(first(1), first)
    assert valid_type(second(2), second)
    with pytest.raises(SchemaError, match="only immutable values"):
        valid_type(second([]), second)
