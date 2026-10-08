"""Family modules preserve consumer imports and support ordinary composition."""

import importlib
import os
from pathlib import Path
import subprocess
import sys
from typing import get_type_hints
import unittest

import pytest

import pysual
from _ui_testcase import AsyncUIOwnerTestCase
from pysual import Container, prop
from pysual.errors import LifecycleError
from pysual.host import Input
from test_library import RecordingHost


_MOVED_CONTROLS = (
    ("Label", "basic", "controls"),
    ("Button", "basic", "controls"),
    ("CheckBox", "actions", "widgets"),
    ("Toggle", "actions", "selection"),
    ("RadioButton", "actions", "selection"),
    ("Slider", "ranges", "widgets"),
    ("ProgressBar", "ranges", "selection"),
    ("ScrollArea", "viewports", "widgets"),
    ("ListView", "lists", "widgets"),
    ("Image", "media", "widgets"),
    ("SubWindow", "windows", "widgets"),
    ("Dropdown", "choices", "selection"),
    ("ComboBox", "choices", "selection"),
)


class ControlFamilyBoundaryTests(unittest.TestCase):
    def test_public_facades_export_identical_types_with_stable_module_names(self):
        for name, family, facade in _MOVED_CONTROLS:
            with self.subTest(control=name):
                public = getattr(pysual, name)
                facade_type = getattr(importlib.import_module(f"pysual.{facade}"), name)
                implementation = getattr(importlib.import_module(f"pysual._controls.{family}"), name)
                self.assertIs(public, facade_type)
                self.assertIs(public, implementation)
                self.assertEqual(public.__module__, f"pysual.{facade}")
        from pysual.selection import _Choices
        from pysual._controls.choices import _Choices as ChoicesImplementation
        self.assertIs(_Choices, ChoicesImplementation)

    def test_consumer_subclasses_resolve_inherited_annotations_after_source_moves(self):
        for name, _, _ in _MOVED_CONTROLS:
            with self.subTest(control=name):
                base = getattr(pysual, name)
                subclass = type(f"Client{name}", (base,), {
                    "__module__": __name__,
                    "__annotations__": {"tag": "str"},
                    "tag": prop(default="client"),
                })
                annotations = get_type_hints(subclass)
                self.assertIs(annotations["tag"], str)
                self.assertIs(annotations["visible"], bool)
                self.assertEqual(subclass.properties()["tag"].definition.default, "client")
                self.assertEqual(subclass.properties().keys(), base.properties().keys() | {"tag"})

    def test_builtin_catalog_has_unique_metadata_and_matches_public_types(self):
        from pysual._catalog import BUILTIN_CONTROLS, BuiltinControl

        historical_names = {
            "label", "button", "panel", "check_box", "slider", "text_box",
            "scroll_area", "list_view", "image", "sub_window", "tree_view",
            "popup", "line_chart", "donut_chart", "data_grid", "menu", "menu_bar",
            "tab_page", "tab_control", "split_pane", "toggle", "radio_button",
            "progress_bar", "dropdown", "combo_box", "numeric_input", "color_picker",
            "group_box", "separator", "hyperlink",
        }
        self.assertLessEqual(historical_names, {entry.name for entry in BUILTIN_CONTROLS})
        self.assertEqual(len({entry.name for entry in BUILTIN_CONTROLS}), len(BUILTIN_CONTROLS))
        self.assertEqual(len({entry.control_type for entry in BUILTIN_CONTROLS}), len(BUILTIN_CONTROLS))
        for entry in BUILTIN_CONTROLS:
            with self.subTest(control=entry.name):
                self.assertTrue(entry.name.isidentifier())
                self.assertTrue(entry.family.isidentifier())
                self.assertIs(getattr(pysual, entry.control_type.__name__), entry.control_type)
                self.assertIs(pysual.registered_controls()[entry.name], entry.control_type)
                if entry.legacy_factory:
                    self.assertIs(getattr(Container, entry.name).control_type, entry.control_type)
        future = BuiltinControl("future_control", pysual.Control, "future")
        self.assertFalse(future.legacy_factory)


@pytest.mark.parametrize("order", (
    ("pysual._controls.choices", "pysual.widgets", "pysual.controls", "pysual.selection"),
    ("pysual._controls.ranges", "pysual.selection", "pysual._controls.actions", "pysual.widgets"),
    ("pysual._catalog", "pysual._registry", "pysual._controls.basic", "pysual.controls"),
))
def test_fresh_import_order_installs_only_catalog_entries_without_starting_engine(order):
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib
import sys
import threading

threads = set(threading.enumerate())
for name in sys.argv[1:]:
    importlib.import_module(name)
import pysual
from pysual import _engine
from pysual._catalog import BUILTIN_CONTROLS, install_builtin_controls

expected = {entry.name: entry.control_type for entry in BUILTIN_CONTROLS}
assert dict(pysual.registered_controls()) == expected
expected_factories = {entry.name: entry.control_type for entry in BUILTIN_CONTROLS
                     if entry.legacy_factory}
assert dict(pysual.registered_controls(factories_only=True)) == expected_factories
install_builtin_controls()
assert dict(pysual.registered_controls()) == expected
assert _engine._instance is None
assert set(threading.enumerate()) == threads
"""
    result = subprocess.run(
        [sys.executable, "-c", script, *order], capture_output=True, text=True,
        timeout=15, cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    assert result.returncode == 0, result.stdout + result.stderr


class CompositeControlTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        from examples.composite_control import Demo

        self.app, self.host = Demo(reduce_motion=True), RecordingHost()
        self.app.run(backend=self.host)
        self.runtime = self.app._runtime

    async def asyncTearDown(self):
        await self.app.destroy_async()

    async def test_instances_keep_child_names_handlers_and_user_edits_independent(self):
        volume, brightness = self.app.volume, self.app.brightness
        self.assertGreater(self.host.frames, 0)
        self.assertIn("One component, two independent instances", self.host.texts)
        for owner in (volume, brightness):
            self.assertEqual(owner.children, (owner.heading, owner.track, owner.number))
            self.assertEqual([child.name for child in owner.children], ["heading", "track", "number"])
            self.assertTrue(all(child.parent is owner for child in owner.children))
        self.assertIsNot(volume.track, brightness.track)
        self.assertIsNot(volume.number, brightness.number)
        self.assertEqual((volume.value, brightness.value), (35, 70))

        self.runtime.router.set_focus(volume.track)
        self.runtime.router.process(Input("key_down", key="ArrowRight"))
        await self.runtime.dispatcher.drain()
        self.assertEqual((volume.value, volume.track.value, volume.number.value), (36, 36, 36))
        self.assertEqual((brightness.value, brightness.track.value, brightness.number.value), (70, 70, 70))

        self.runtime.router.set_focus(brightness.number)
        self.runtime.router.process(Input("key_down", key="ArrowLeft"))
        await self.runtime.dispatcher.drain()
        self.assertEqual((brightness.value, brightness.track.value, brightness.number.value), (69, 69, 69))
        self.assertEqual(volume.value, 36)
        self.assertEqual(self.app.summary.text, "Volume: 36 / Brightness: 69")

    async def test_public_update_synchronizes_children_once_and_invalid_batch_is_atomic(self):
        volume = self.app.volume
        seen = []
        volume.changed.connect(lambda event: seen.append(event.new_value))
        volume.update(caption="Output level", value=62)
        await self.runtime.dispatcher.drain()
        self.assertEqual(seen, [62])
        self.assertEqual((volume.caption, volume.heading.text), ("Output level", "Output level"))
        self.assertEqual((volume.value, volume.track.value, volume.number.value), (62, 62, 62))
        self.assertEqual(self.app.brightness.value, 70)
        for invalid in (-1, 101):
            with self.subTest(value=invalid):
                with self.assertRaises(ValueError):
                    volume.update(caption="Must not commit", value=invalid)
                self.assertEqual((volume.caption, volume.heading.text), ("Output level", "Output level"))
                self.assertEqual((volume.value, volume.track.value, volume.number.value), (62, 62, 62))
        await self.runtime.dispatcher.drain()
        self.assertEqual(seen, [62])

    async def test_child_edits_are_immediate_and_preserve_origins_and_later_writes(self):
        volume = self.app.volume
        seen = []
        volume.changed.connect(lambda event: seen.append(
            (event.old_value, event.new_value, event.origin)))
        for child, expected in ((volume.track, 36), (volume.number, 37)):
            child.focus()
            self.runtime.router.process(Input("key_down", key="ArrowRight"))
            self.assertEqual((volume.value, volume.track.value, volume.number.value),
                             (expected,) * 3)
        # Public notifications have not run yet. They must not overwrite a
        # subsequent assignment or drive another round of synchronization.
        volume.value = 80
        volume.track.value = 64
        self.assertEqual((volume.value, volume.track.value, volume.number.value), (64,) * 3)
        volume.number.update(value=62)
        self.assertEqual((volume.value, volume.track.value, volume.number.value), (62,) * 3)
        self.assertEqual((volume._origin, volume.track._origin, volume.number._origin),
                         ("program",) * 3)
        await self.runtime.dispatcher.drain()
        self.assertEqual((volume.value, volume.track.value, volume.number.value), (62,) * 3)
        self.assertEqual(seen, [(35, 36, "user"), (36, 37, "user"), (37, 80, "program"),
                                (80, 64, "program"), (64, 62, "program")])

    async def test_rejected_child_edits_preserve_both_editors_and_emit_no_changes(self):
        from examples.composite_control import RangeField

        class GuardedRangeField(RangeField):
            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if name == "value" and value == 36:
                    raise ValueError("36 is reserved")

        field = GuardedRangeField(parent=self.app, value=35)
        seen = []
        for control in (field, field.track, field.number):
            control.changed.connect(seen.append)
        for child in (field.track, field.number):
            child.focus()
            with self.assertRaisesRegex(ValueError, "reserved"):
                self.runtime.router.process(Input("key_down", key="ArrowRight"))
            with self.assertRaisesRegex(ValueError, "reserved"):
                child.update(value=36, background="#123456")
            self.assertIsNone(child.background)
            self.assertEqual((field.value, field.track.value, field.number.value), (35,) * 3)
            self.assertEqual((field._origin, field.track._origin, field.number._origin),
                             ("program",) * 3)
        await self.runtime.dispatcher.drain()
        self.assertEqual(seen, [])
        field.number.value = 40
        self.assertEqual((field.value, field.track.value, field.number.value), (40,) * 3)

    async def test_destroying_one_composite_disposes_its_children_and_keeps_other_live(self):
        from examples.composite_control import RangeField

        transient = RangeField(parent=self.app, caption="Temporary", value=20)
        brightness = self.app.brightness
        children = transient.children
        transient.destroy()
        for child in children:
            with self.assertRaises(LifecycleError):
                _ = child.parent
        with self.assertRaises(LifecycleError):
            transient.value = 5
        brightness.update(caption="Screen level", value=83)
        await self.runtime.dispatcher.drain()
        self.assertEqual((brightness.value, brightness.track.value, brightness.number.value), (83, 83, 83))
        self.assertEqual(brightness.heading.text, "Screen level")


@pytest.mark.parametrize("flag,code", (("--help", 0), ("--unknown-option", 2)))
def test_composite_example_parses_arguments_before_opening(flag, code):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, str(root / "examples/composite_control.py"), flag],
        capture_output=True, text=True, timeout=10,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
    )
    assert result.returncode == code
    assert "usage:" in result.stdout + result.stderr
