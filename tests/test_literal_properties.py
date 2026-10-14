"""Literal declarations retain the existing validation and persistence contract."""

from dataclasses import dataclass, replace
import inspect
import json
import sys
from typing import Literal

import pytest

from pysual import Container, Label, prop
from pysual.errors import SchemaError


@dataclass(frozen=True)
class Caption:
    text: str


class StatusLabel(Label):
    status: Literal["idle", "busy"] = prop(default="idle")
    attempts: Literal[1, 2, 3] = prop(default=1, minimum=1)
    answer: Literal[None, True] = prop(default=None)


def test_literal_values_preserve_mutation_errors_atomicity_and_json_roundtrip():
    label = StatusLabel(status="busy", attempts=2, answer=True)
    try:
        for name in ("status", "attempts", "answer", "font_family", "dock"):
            field = label.properties()[name]
            value = getattr(label, name)
            assert field.serializable
            assert field.decode(json.loads(json.dumps(field.encode(value)))) == value
        with pytest.raises(ValueError, match="expected one of"):
            label.status = "missing"
        with pytest.raises(TypeError, match="expected"):
            label.attempts = True
        with pytest.raises(ValueError, match="expected one of"):
            label.answer = False
        with pytest.raises(ValueError, match="expected one of"):
            label.update(status="idle", attempts=4)
        assert (label.status, label.attempts, label.answer) == ("busy", 2, True)
        with pytest.raises(ValueError, match="expected one of"):
            label.properties()["status"].decode("missing")
    finally:
        label.destroy()


def test_runtime_metadata_and_legacy_inherited_declarations_stay_compatible():
    layout_field = Container.properties()["layout"]
    assert layout_field.annotation is str
    assert Container.properties()["font_family"].annotation == str | None
    assert inspect.getattr_static(Container, "layout").field is layout_field

    class LegacyPanel(Container):
        layout: str = replace(layout_field.definition, default="stack")
        font_family: str | None = Container.properties()["font_family"].definition

    panel = LegacyPanel(layout="grid", font_family="mono")
    try:
        assert (panel.layout, panel.font_family) == ("grid", "mono")
        with pytest.raises(ValueError, match="expected one of"):
            panel.layout = "missing"
    finally:
        panel.destroy()


def test_literal_defaults_and_ambiguous_declarations_fail_during_class_definition():
    with pytest.raises(ValueError, match="expected one of"):
        class BadDefault(Label):
            status: Literal["idle"] = prop(default="busy")

    with pytest.raises(SchemaError, match="not choices="):
        class DuplicateChoices(Label):
            status: Literal["idle"] = prop(default="idle", choices=("idle",))

    with pytest.raises(SchemaError, match="do not mix bool and int"):
        class AmbiguousChoices(Label):
            status: Literal[True, 0] = prop(default=True)


def test_existing_explicit_choice_metadata_keeps_its_previous_behavior():
    class LegacyChoice(Label):
        status: str = prop(default="idle", choices=("idle", "busy"))

    label = LegacyChoice(status="busy")
    try:
        assert label.status == "busy"
        with pytest.raises(ValueError, match="expected one of"):
            label.status = "missing"
    finally:
        label.destroy()


def test_nested_literals_validate_mutations_and_roundtrip_json():
    class Choices(Label):
        status: Literal["idle", "busy"] | None = prop(default=None)
        flags: tuple[Literal[True], ...] = prop(default=())
        pair: tuple[Literal[1, 2], Literal["left", "right"]] = prop(
            default=(1, "left")
        )

    label = Choices(status="busy", flags=(True,), pair=(2, "right"))
    try:
        for name in ("status", "flags", "pair"):
            field = label.properties()[name]
            value = getattr(label, name)
            assert field.serializable
            assert field.decode(json.loads(json.dumps(field.encode(value)))) == value
        for name, value in (("status", "missing"), ("flags", (1,)), ("pair", (True, "left"))):
            with pytest.raises(TypeError, match="expected"):
                setattr(label, name, value)
            with pytest.raises(TypeError, match="expected"):
                label.properties()[name].decode(value)
        with pytest.raises(TypeError, match="expected"):
            label.update(status="idle", pair=(1, "missing"))
        assert (label.status, label.pair) == ("busy", (2, "right"))
        label.update(status=None, flags=(), pair=(1, "left"))
        assert (label.status, label.flags, label.pair) == (None, (), (1, "left"))
    finally:
        label.destroy()


def test_invalid_nested_literal_is_rejected_even_with_an_empty_default():
    with pytest.raises(SchemaError, match="do not mix bool and int"):
        class AmbiguousItems(Label):
            items: tuple[Literal[True, 1], ...] = prop(default=())

    with pytest.raises(SchemaError, match="Literal properties require"):
        class InvalidItems(Label):
            items: tuple[Literal[b"bytes"], ...] = prop(default=())


def test_schema_resolves_only_property_annotations_and_keeps_inherited_fields():
    class ForwardState(Label):
        _peer: "NotDefinedYet"
        caption: "Caption" = prop(default=Caption("Ready"))

    class Specialized(ForwardState):
        _other: "AlsoNotDefinedYet"
        caption: "Caption" = prop(default=Caption("Specialized"))
        count: "int" = prop(default=0)

    class Inherited(Specialized):
        pass

    label = Inherited(text="Inherited", count=2)
    try:
        assert (label.caption, label.text, label.count) == (
            Caption("Specialized"), "Inherited", 2
        )
        assert Inherited.properties()["caption"].annotation is Caption
        with pytest.raises(TypeError, match="expected"):
            label.caption = "Wrong"
    finally:
        label.destroy()


def test_property_forward_references_use_the_declaring_class_namespace():
    class Aliased(Label):
        Status = Literal["idle", "busy"]
        status: "Status" = prop(default="idle")

    label = Aliased(status="busy")
    try:
        assert label.status == "busy"
        with pytest.raises(ValueError, match="expected one of"):
            label.status = "missing"
    finally:
        label.destroy()


@pytest.mark.skipif(sys.version_info < (3, 14), reason="Native deferred annotations require Python 3.14")
def test_native_deferred_state_annotations_do_not_need_runtime_imports():
    class Deferred(Label):
        _peer: RuntimeUnavailable
        count: int = prop(default=1)

    class Inherited(Deferred):
        _other: AlsoUnavailable

    label = Inherited(count=2)
    try:
        assert label.count == 2
        with pytest.raises(TypeError, match="expected"):
            label.count = "Wrong"
    finally:
        label.destroy()
