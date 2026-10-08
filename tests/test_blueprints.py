"""Secondary declarations preserve ordinary constructor, binding and lifetimes."""

import asyncio
from dataclasses import FrozenInstanceError
import unittest
from _ui_testcase import AsyncUIOwnerTestCase

from pysual import App, Button, Container, Label, prop
from pysual.controls import blueprint
from pysual.errors import BindingError, LifecycleError
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


class BlueprintDataTests(unittest.TestCase):
    def test_instances_materialize_fresh_controls_with_canonical_parenting(self):
        class Form(Container):
            action = blueprint(Button, text="Apply").under("toolbar")
            toolbar = blueprint(Container, layout="stack")

        first, second = Form(), Form()
        try:
            self.assertIsNot(first.action, second.action)
            self.assertEqual(first.action.name, "action")
            self.assertIs(first.action.parent, first.toolbar)
            self.assertEqual(first.toolbar.children, (first.action,))
            self.assertIs(Form.action.control_type, Button)
            self.assertEqual(Form.action.properties, {"text": "Apply"})
            self.assertEqual(Form.action.parent_name, "toolbar")
            first.action.text = "Changed"
            self.assertEqual(second.action.text, "Apply")
            self.assertEqual(first.action.inspect().name, "action")
            self.assertIs(
                first.action.properties()["text"], Button.properties()["text"]
            )
            with self.assertRaises(TypeError):
                Form.action.properties["text"] = "No"
            with self.assertRaises(FrozenInstanceError):
                Form.action.parent_name = None
        finally:
            first.destroy()
            second.destroy()

    def test_inherited_overrides_keep_descriptor_types_and_bound_handlers(self):
        called = []

        class Base(Container):
            action = blueprint(Button, text="Base")

            def action_on_click(self, event):
                called.append(event.source)

        class Derived(Base):
            action = blueprint(Button, text="Derived")
            label_control = blueprint(Label, text="More")

        form = Derived()
        try:
            self.assertEqual(form.action.text, "Derived")
            self.assertEqual(
                [child.name for child in form.children], ["action", "label_control"]
            )
            self.assertEqual(form.action.click._convention, form.action_on_click)
            previous = form.action
            previous.destroy()
            form.action = Button(text="Replacement")
            self.assertEqual(form.action.name, "action")
            form.action.destroy()
            wrong = Label()
            with self.assertRaises(TypeError):
                form.action = wrong
            self.assertIsNone(wrong.parent)
            wrong.destroy()
        finally:
            form.destroy()
        with self.assertRaises(BindingError):

            class Wrong(Base):
                action = 1

    def test_schema_validation_rejects_unknown_mutable_and_live_parent_inputs(self):
        for kwargs in ({"text": []}, {"not_a_property": 1}, {"parent": None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(TypeError):
                blueprint(Button, **kwargs)
        with self.assertRaises(TypeError):
            blueprint(App)
        with self.assertRaises(TypeError):
            blueprint(lambda: Button())
        with self.assertRaises(TypeError):
            blueprint(Button, "not positional")

    def test_cycles_and_noncontainer_parent_fail_before_any_child_allocation(self):
        allocated = []

        class Observed(Container):
            def _initialize(self):
                super()._initialize()
                allocated.append(self)

        class Cycle(Container):
            first = blueprint(Observed).under("second")
            second = blueprint(Observed).under("first")

        class Missing(Container):
            first = blueprint(Observed).under("missing")

        class WrongParent(Container):
            first = blueprint(Observed).under("label_control")
            label_control = blueprint(Label)

        for owner in (Cycle, Missing, WrongParent):
            with self.subTest(owner=owner), self.assertRaises(BindingError):
                owner()
        self.assertEqual(allocated, [])

    def test_failed_materialization_disposes_children_and_releases_app(self):
        created = []

        class Observed(Button):
            def _initialize(self):
                super()._initialize()
                created.append(self)

        class Broken(App):
            good_control = blueprint(Observed)
            invalid_control = blueprint(Button, min_width=100, max_width=20)

        with self.assertRaises(ValueError):
            Broken()
        self.assertTrue(created[0]._disposed)
        replacement = App()
        replacement.destroy()

    def test_collisions_descriptor_reuse_and_early_access_fail_clearly(self):
        with self.assertRaises((BindingError, RuntimeError)):

            class Collision(Container):
                width = blueprint(Button)

        descriptor = blueprint(Button)
        with self.assertRaises((BindingError, RuntimeError)):

            class Reused(Container):
                first = descriptor
                second = descriptor

        class TooEarly(Container):
            action = blueprint(Button)

            def __init__(self):
                super().__init__()
                self.action.text = "Too soon"

        with self.assertRaises(LifecycleError):
            TooEarly()

    def test_schema_metadata_and_materialization_coexist_in_custom_composite(self):
        class Composite(Container):
            caption: str = prop(default="Composite")
            label_control = blueprint(Label)

            def on_attached(self):
                self.label_control.text = self.caption

        app = App()
        try:
            instance = Composite(parent=app, caption="Ordinary schema")
            self.assertEqual(instance.label_control.text, "Ordinary schema")
            self.assertEqual(
                instance.properties()["caption"].encode(instance.caption),
                "Ordinary schema",
            )
            self.assertTrue(instance.inspect().control_type.endswith(".Composite"))
        finally:
            app.destroy()


class BlueprintRuntimeTests(AsyncUIOwnerTestCase):
    async def test_declarations_exist_before_build_and_dispatch_normal_conventions(
        self,
    ):
        observations = []

        class Demo(App):
            action = blueprint(Button, text="Before build")

            def build(self):
                observations.append(self.action.text)
                self.extra = Label(text="Dynamic")

            def action_on_click(self, event):
                observations.append(event.source is self.action)

        app = Demo()
        runtime = Runtime(app, RecordingHost())
        task = asyncio.create_task(runtime.main())
        await eventually(lambda: app.is_open or task.done())
        try:
            if task.done():
                await task
            app.action.activate()
            await eventually(lambda: len(observations) == 2)
            self.assertEqual(observations, ["Before build", True])
            self.assertEqual(app.extra.name, "extra")
        finally:
            runtime._stop.set()
            await task
