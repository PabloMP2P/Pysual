"""Catalog discovery and typed construction do not require container aliases."""

import inspect
import unittest

from pysual import (
    App, Button, Container, Control, Label, TabControl, TabPage, prop,
    register_control, registered_controls,
)
from pysual import _registry
from pysual.errors import BindingError, LifecycleError, SchemaError


class CatalogBadge(Label):
    count: int = prop(default=0)


class CatalogRegistryTests(unittest.TestCase):
    def setUp(self):
        with _registry._registry_lock:
            self._registrations = _registry._registrations.copy()
            self._aliases = {
                name: value for name, value in vars(Container).items()
                if isinstance(value, _registry._Factory)
            }

    def tearDown(self):
        with _registry._registry_lock:
            for name, value in tuple(vars(Container).items()):
                if isinstance(value, _registry._Factory) and name not in self._aliases:
                    delattr(Container, name)
            for name, value in self._aliases.items():
                setattr(Container, name, value)
            _registry._registrations.clear()
            _registry._registrations.update(self._registrations)

    def test_catalog_only_registration_leaves_child_names_available(self):
        result = register_control(CatalogBadge, name="catalog_badge", factory=False)
        self.assertIs(result, CatalogBadge)
        self.assertIs(registered_controls()["catalog_badge"], CatalogBadge)
        self.assertNotIn("catalog_badge", registered_controls(factories_only=True))
        self.assertNotIn("catalog_badge", vars(Container))
        parent = Container()
        self.addCleanup(parent.destroy)
        parent.catalog_badge = parent.create(CatalogBadge, text="Ready", count=3)
        self.assertIs(type(parent.catalog_badge), CatalogBadge)
        self.assertIs(parent.catalog_badge.parent, parent)
        self.assertEqual(parent.catalog_badge.name, "catalog_badge")
        self.assertEqual(parent.catalog_badge.count, 3)

    def test_catalog_only_names_can_match_framework_members(self):
        register_control(CatalogBadge, name="build", factory=False, authorable=False)
        self.assertIs(registered_controls()["build"], CatalogBadge)
        self.assertNotIn("build", registered_controls(authorable_only=True))
        self.assertNotIn("build", registered_controls(factories_only=True))
        with self.assertRaisesRegex(BindingError, "collides"):
            register_control(CatalogBadge, name="build")
        self.assertNotIn("build", registered_controls(authorable_only=True))
        self.assertNotIn("build", registered_controls(factories_only=True))
        self.assertTrue(callable(App.build))

    def test_legacy_factory_registration_remains_concrete_and_idempotent(self):
        register_control(CatalogBadge, name="catalog_alias")
        original = vars(Container)["catalog_alias"]
        register_control(CatalogBadge, name="catalog_alias", authorable=False)
        self.assertIs(vars(Container)["catalog_alias"], original)
        self.assertNotIn("catalog_alias", registered_controls(authorable_only=True))
        self.assertIs(registered_controls(factories_only=True)["catalog_alias"], CatalogBadge)
        parent = Container()
        self.addCleanup(parent.destroy)
        child = parent.catalog_alias(count=4)
        self.assertIs(type(child), CatalogBadge)
        self.assertIs(child.parent, parent)
        signature = inspect.signature(parent.catalog_alias)
        self.assertIs(signature.return_annotation, CatalogBadge)
        self.assertIs(signature.parameters["count"].annotation, int)
        self.assertEqual(signature.parameters["count"].default, 0)
        self.assertEqual(signature.parameters["count"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIs(signature.parameters["text"].annotation, str)
        self.assertIs(inspect.signature(parent.catalog_alias), signature)
        other = Container()
        self.addCleanup(other.destroy)
        self.assertIs(inspect.signature(other.catalog_alias), signature)
        other_child = other.catalog_alias(count=5)
        self.assertIs(other_child.parent, other)
        self.assertEqual(child.count, 4)
        self.assertEqual(other_child.count, 5)

    def test_snapshots_and_duplicate_catalog_names_are_independent_of_aliases(self):
        register_control(CatalogBadge, name="catalog_entry", factory=False)
        snapshot = registered_controls()
        with self.assertRaises(TypeError):
            snapshot["another"] = Button
        with self.assertRaisesRegex(BindingError, "already registered"):
            register_control(Button, name="catalog_entry", factory=False)
        register_control(CatalogBadge, name="catalog_second", factory=False)
        self.assertNotIn("catalog_second", snapshot)
        self.assertIs(snapshot["catalog_entry"], CatalogBadge)

    def test_removing_an_alias_keeps_discovery_and_reregistration_restores_it(self):
        register_control(CatalogBadge, name="catalog_removed")
        delattr(Container, "catalog_removed")
        self.assertIs(registered_controls()["catalog_removed"], CatalogBadge)
        self.assertNotIn("catalog_removed", registered_controls(factories_only=True))
        register_control(CatalogBadge, name="catalog_removed")
        self.assertIn("catalog_removed", registered_controls(factories_only=True))
        register_control(CatalogBadge, name="catalog_removed", factory=False)
        self.assertNotIn("catalog_removed", vars(Container))
        self.assertIs(registered_controls()["catalog_removed"], CatalogBadge)

    def test_factory_policy_change_does_not_remove_a_replaced_member(self):
        register_control(CatalogBadge, name="catalog_replaced")
        sentinel = object()
        Container.catalog_replaced = sentinel
        self.addCleanup(delattr, Container, "catalog_replaced")
        register_control(CatalogBadge, name="catalog_replaced", factory=False)
        self.assertIs(Container.catalog_replaced, sentinel)
        with self.assertRaisesRegex(BindingError, "collides"):
            register_control(CatalogBadge, name="catalog_replaced")
        self.assertNotIn("catalog_replaced", registered_controls(factories_only=True))

    def test_unregistered_descriptors_do_not_become_catalog_entries(self):
        Container.catalog_unregistered = _registry._Factory(CatalogBadge)
        self.assertNotIn("catalog_unregistered", registered_controls())
        self.assertNotIn("catalog_unregistered", registered_controls(factories_only=True))

    def test_validation_rejects_bad_types_flags_and_names_without_registration(self):
        before = dict(registered_controls())
        for control_type in (App, object, object()):
            with self.subTest(control_type=control_type), self.assertRaises(SchemaError):
                register_control(control_type, name="catalog_invalid", factory=False)
        for properties in (
            {"factory": 1}, {"factory": None}, {"authorable": 1},
        ):
            with self.subTest(properties=properties), self.assertRaises(TypeError):
                register_control(CatalogBadge, name="catalog_invalid", **properties)
        for name in ("_private", "with", "two words", "", 1):
            with self.subTest(name=name), self.assertRaises(BindingError):
                register_control(CatalogBadge, name=name, factory=False)
        self.assertEqual(dict(registered_controls()), before)


class GenericCreationTests(unittest.TestCase):
    def test_unregistered_custom_constructor_runs_before_attachment_and_keeps_type(self):
        trace = []

        class Custom(Control):
            def __init__(self, caption: str, *, width: float = 10):
                super().__init__(width=width)
                self._caption = caption
                trace.append("constructed")

            def on_attached(self):
                trace.append((self._caption, self.width, self.parent))

        parent = Container()
        self.addCleanup(parent.destroy)
        control = parent.create(Custom, "Hello", width=20)
        self.assertIs(type(control), Custom)
        self.assertEqual(trace, ["constructed", ("Hello", 20, parent)])
        self.assertEqual(parent.children, (control,))
        self.assertIsNone(control.name)

    def test_parent_override_and_invalid_types_reject_before_constructing(self):
        trace = []

        class Custom(Control):
            def __init__(self, **properties):
                trace.append("constructed")
                super().__init__(**properties)

        parent = Container()
        self.addCleanup(parent.destroy)
        for target in (parent, None):
            with self.assertRaisesRegex(TypeError, "direct constructor"):
                parent.create(Custom, parent=target)
            with self.assertRaisesRegex(TypeError, "direct constructor"):
                parent.button(parent=target)
        for control_type in (App, object, lambda: Button()):
            with self.assertRaisesRegex(TypeError, "Control subclass"):
                parent.create(control_type)
        self.assertEqual(trace, [])
        self.assertEqual(parent.children, ())

    def test_failed_adoption_disposes_the_new_control_for_both_creation_paths(self):
        controls = []

        class Fragile(Control):
            def _initialize(self):
                super()._initialize()
                controls.append(self)

            def on_attached(self):
                raise ValueError("attachment failed")

        parent = Container()
        self.addCleanup(parent.destroy)
        with self.assertRaisesRegex(ValueError, "attachment failed"):
            parent.create(Fragile)
        self.assertTrue(controls[0]._disposed)
        self.assertEqual(parent.children, ())
        # A legacy alias uses the same ownership transaction.
        alias = _registry._Factory(Fragile).__get__(parent)
        with self.assertRaisesRegex(ValueError, "attachment failed"):
            alias()
        self.assertTrue(controls[1]._disposed)
        self.assertEqual(parent.children, ())

    def test_container_subclass_restrictions_and_disposed_parent_are_preserved(self):
        parent = TabControl()
        self.addCleanup(parent.destroy)
        child = parent.create(TabPage)
        self.assertIs(child.parent, parent)
        with self.assertRaisesRegex(TypeError, "TabPage"):
            parent.create(Button)
        self.assertEqual(parent.children, (child,))
        parent.destroy()
        with self.assertRaises(LifecycleError):
            parent.create(TabPage)
