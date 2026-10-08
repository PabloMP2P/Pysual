"""Choice metadata improves factory typing without changing runtime schemas."""

import inspect
from pathlib import Path
import runpy
import subprocess
import sys
from types import ModuleType
from typing import Literal, Union, get_args, get_origin
import unittest
from unittest.mock import patch

from pysual import Container, Control, register_control
from pysual import _registry
from pysual.schema import Field, prop


TOOL = Path(__file__).resolve().parents[1] / "tools/generate_factory_types.py"


class FactoryTypeGeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        generator = runpy.run_path(str(TOOL))
        cls.annotation = staticmethod(generator["field_annotation"])
        cls.generate = staticmethod(generator["generate"])

    def _generate_catalog(self, registrations, modules):
        with patch.dict(self.generate.__globals__, {"registered_controls": lambda **kwargs: registrations}):
            generated = self.generate()
        namespace = {"__package__": "pysual"}
        with patch.dict(sys.modules, modules):
            exec(compile(generated, "<factory signatures>", "exec"), namespace)
        return generated, namespace["FactoryTypes"]

    @staticmethod
    def _catalog_module(name, record_name="Record", control_name="CatalogControl"):
        module = ModuleType(name)
        record = type(record_name, (), {"__module__": name})
        field = Field("records", tuple[record | None, ...], prop(default=()))
        control = type(control_name, (), {
            "__module__": name,
            "properties": classmethod(lambda cls: {"records": field}),
        })
        setattr(module, record_name, record)
        setattr(module, control_name, control)
        return module, record, control

    def test_new_catalog_modules_and_nested_field_types_need_no_import_list(self):
        module, record, control = self._catalog_module("pysual.future_catalog")
        generated, factories = self._generate_catalog(
            {"catalog_control": control}, {module.__name__: module}
        )
        self.assertIn("from .future_catalog import CatalogControl, Record", generated)
        signature = inspect.signature(factories.catalog_control)
        self.assertIs(signature.return_annotation, control)
        self.assertEqual(signature.parameters["records"].annotation, tuple[record | None, ...])

    def test_distinct_modules_can_reuse_control_and_record_names(self):
        first, first_record, first_control = self._catalog_module("pysual.first_catalog")
        second, second_record, second_control = self._catalog_module("pysual.second_catalog")
        generated, factories = self._generate_catalog(
            {"first": first_control, "second": second_control},
            {first.__name__: first, second.__name__: second},
        )
        for factory, control, record in (
            (factories.first, first_control, first_record),
            (factories.second, second_control, second_record),
        ):
            signature = inspect.signature(factory)
            self.assertIs(signature.return_annotation, control)
            self.assertEqual(signature.parameters["records"].annotation, tuple[record | None, ...])

    def test_public_module_aliases_are_preserved_in_generated_imports(self):
        generated = self.generate()
        widget_import = next(line for line in generated.splitlines() if line.startswith("from .widgets import "))
        self.assertIn("TextBox", widget_import)
        self.assertNotIn("from .text import", generated)

    def test_catalog_only_controls_do_not_generate_nonexistent_factory_methods(self):
        class CatalogOnlyFixture(Control):
            pass

        CatalogOnlyFixture.__module__ = "pysual.future_catalog"
        CatalogOnlyFixture.__qualname__ = "CatalogOnlyFixture"
        before = self.generate()
        name = "catalog_only_typing_fixture"
        try:
            register_control(CatalogOnlyFixture, name=name, factory=False)
            self.assertEqual(self.generate(), before)
        finally:
            with _registry._registry_lock:
                _registry._registrations.pop(name, None)

    def test_checked_in_factory_signatures_match_current_schemas(self):
        # A fresh interpreter sees only built-in registrations and also proves
        # the maintenance command works without editable-install path setup.
        result = subprocess.run(
            [sys.executable, "-I", str(TOOL), "--check"],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_builtin_choices_include_the_optional_font_default(self):
        fields = Container.properties()
        self.assertEqual(
            self.annotation(fields["layout"]),
            "Literal['absolute', 'stack', 'grid', 'flow', 'dock']",
        )
        self.assertEqual(
            self.annotation(fields["font_family"]), "Literal[None, 'ui', 'mono']"
        )
        self.assertEqual(self.annotation(fields["width"]), "float | None")

    def test_supported_literals_preserve_exact_types_and_quoted_text(self):
        cases = (
            (str, ("it's", 'say "hi"', "line\nbreak")),
            (int, (-1, 0, 2)),
            (bool, (False, True)),
            (str | None, (None, "ui")),
            (str | None, ("ui", "mono")),
            (Union[str, None], (None, "ui")),
        )
        for annotation, choices in cases:
            with self.subTest(annotation=annotation, choices=choices):
                field = Field(
                    "choice", annotation, prop(default=choices[0], choices=choices)
                )
                # Parse the emitted type to verify quoting and exact bool/int
                # membership, instead of just repeating the string formatter.
                generated = eval(self.annotation(field), {"Literal": Literal})
                self.assertIs(get_origin(generated), Literal)
                self.assertEqual(get_args(generated), choices)
                self.assertEqual(
                    tuple(map(type, get_args(generated))), tuple(map(type, choices))
                )

    def test_unsupported_or_mismatched_choices_keep_the_annotation(self):
        cases = (
            (str, ()),
            (float, (1, 2)),
            (float, (1.0, 2.0)),
            (str | float, ("auto", 1)),
            (str, ("ok", None)),
            (int, (False, True)),
            (bool, (0, 1)),
            (int | bool, (1,)),
            (int | bool, (True,)),
            (str, ([],)),
            (tuple[str, ...], (("a",),)),
        )
        for annotation, choices in cases:
            with self.subTest(annotation=annotation, choices=choices):
                field = Field("choice", annotation, prop(default=None, choices=choices))
                self.assertEqual(
                    self.annotation(field), inspect.formatannotation(annotation)
                )
