"""Literal declarations retain the existing validation and persistence contract."""

from dataclasses import replace
import inspect
import json
from typing import Literal

import pytest

from pysual import Container, Label, prop
from pysual.errors import SchemaError


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
