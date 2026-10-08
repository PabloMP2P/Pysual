"""Ordinary property access stays committed, ordered and owner-safe."""

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import inspect
import threading
import unittest

from pysual import Button, Container, Label, TextBox, Window, prop
from pysual._engine import _origin, call, call_async, capture_origin, get_engine
from pysual.errors import LifecycleError
from pysual.events import Dispatcher
from pysual.presentation import Presentation


class TransparentPropertyTests(unittest.TestCase):
    def keep(self, control):
        self.addCleanup(control.destroy)
        return control

    def test_plain_property_writes_are_owner_requests_and_reads_are_direct(self):
        controls = [self.keep(cls(text="Before")) for cls in (Label, Button)]
        engine = get_engine()
        before = engine.calls_submitted
        for control in controls:
            control.text = "After"
            control.background = "#123456"
            control.width = 120
        written = engine.calls_submitted - before
        self.assertEqual(written, 6)
        for control in controls:
            self.assertEqual(control.text, "After")
            self.assertEqual(control.background, "#123456")
            self.assertEqual(control.width, 120)
        # Reads of committed values never queue a request behind the owner.
        self.assertEqual(engine.calls_submitted - before, written)

    def test_five_hundred_ordinary_assignments_commit_in_order(self):
        parent = self.keep(Container())
        controls = [Label(parent=parent, text="Before") for _ in range(500)]
        engine = get_engine()
        for index, control in enumerate(controls):
            control.text = str(index)
        before = engine.calls_submitted
        self.assertEqual([control.text for control in controls], list(map(str, range(500))))
        self.assertEqual(engine.calls_submitted, before)

    def test_rejected_setters_leave_committed_values_unchanged(self):
        control = self.keep(Label(text="Before", min_width=10, max_width=20))
        with self.assertRaises(TypeError):
            control.text = 123
        with self.assertRaises(ValueError):
            control.min_width = 30
        with self.assertRaises(ValueError):
            control.background = "not a color"
        self.assertEqual(control.text, "Before")
        self.assertEqual((control.min_width, control.max_width), (10, 20))
        self.assertIsNone(control.background)

    def test_custom_validation_changed_hooks_and_computed_getters_keep_owner(self):
        observed = []

        class Custom(Label):
            def _validate_update(self, name, value):
                observed.append(("validate", threading.get_ident()))
                super()._validate_update(name, value)
                if name == "text" and value == "Rejected":
                    raise ValueError("custom rejection")

            def _changed(self, field, old, value):
                observed.append(("changed", threading.get_ident()))
                super()._changed(field, old, value)

            @property
            def text_length(self):
                observed.append(("property", threading.get_ident()))
                return len(self.text)

        control = self.keep(Custom(text="Before"))
        owner = call(threading.get_ident)
        observed.clear()
        control.text = "After"
        self.assertEqual(control.text_length, 5)
        with self.assertRaisesRegex(ValueError, "custom rejection"):
            control.text = "Rejected"
        self.assertEqual(control.text, "After")
        self.assertEqual({kind for kind, _ in observed}, {"validate", "changed", "property"})
        self.assertEqual({ident for _, ident in observed}, {owner})
        self.assertNotEqual(owner, threading.get_ident())

    def test_stateful_text_normalization_and_caret_changes_finish_before_return(self):
        control = self.keep(TextBox(text="one\r\ntwo", multiline=True))
        control.select_all()
        control.multiline = False
        self.assertEqual((control.multiline, control.text), (False, "one two"))
        self.assertEqual(control.selection_range, (0, 7))
        control.text = "x\r\ny"
        self.assertEqual(control.text, "x y")
        self.assertEqual(control.selection_range, (0, 3))

    def test_cooperative_builtin_validator_keeps_mixin_hooks_on_owner(self):
        observed = []

        class ValidationMixin(Label):
            def _validate_update(self, name, value):
                observed.append(threading.get_ident())
                super()._validate_update(name, value)

        class ContainerValidationMixin(ValidationMixin):
            cache_paint: bool = Container.properties()["cache_paint"].definition

        class CustomContainer(Container, ContainerValidationMixin):
            pass

        class ButtonValidationMixin(ValidationMixin):
            # The two bases must agree on the inherited property's declaration.
            focusable: bool = Button.properties()["focusable"].definition

        class CustomButton(Button, ButtonValidationMixin):
            pass

        owner = call(threading.get_ident)
        for control_type in (CustomContainer, CustomButton):
            with self.subTest(control_type=control_type.__name__):
                control = self.keep(control_type(text="Before"))
                observed.clear()
                control.text = "After"
                self.assertEqual(observed, [owner])
                self.assertEqual(control.text, "After")

    def test_cooperative_container_setter_preserves_mixin_normalization(self):
        observed = []

        class SetterMixin(Label):
            cache_paint: bool = Container.properties()["cache_paint"].definition

            def __setattr__(self, name, value):
                if name == "text":
                    observed.append(threading.get_ident())
                    value = value.upper()
                super().__setattr__(name, value)

        class CustomContainer(Container, SetterMixin):
            pass

        control = self.keep(CustomContainer(text="Before"))
        owner = call(threading.get_ident)
        observed.clear()
        control.text = "after"
        self.assertEqual(observed, [owner])
        self.assertEqual(control.text, "AFTER")

    def test_custom_parent_private_setter_stays_on_owner_during_invalidation(self):
        observed = []

        class Parent(Window):
            def __setattr__(self, name, value):
                if name == "_paint_revision":
                    observed.append(threading.get_ident())
                super().__setattr__(name, value)

        parent = self.keep(Parent())
        label = Label(parent=parent, text="Before")
        owner = call(threading.get_ident)
        observed.clear()
        label.text = "After"
        self.assertTrue(observed)
        self.assertEqual(set(observed), {owner})
        self.assertEqual(label.text, "After")

    def test_custom_property_metadata_comparison_stays_on_owner(self):
        observed = []

        class Choice:
            def __eq__(self, value):
                observed.append(threading.get_ident())
                return value in ("Before", "After")

        class Custom(Label):
            code: str = prop(default="Before", choices=(Choice(),))

        control = self.keep(Custom())
        owner = call(threading.get_ident)
        observed.clear()
        control.code = "After"
        self.assertTrue(observed)
        self.assertEqual(set(observed), {owner})
        self.assertEqual(control.code, "After")

    def test_replaced_schema_descriptor_keeps_its_setter_behavior_and_owner(self):
        observed = []

        class Custom(Label):
            pass

        slot = inspect.getattr_static(Custom, "text")

        def set_text(control, value):
            observed.append(threading.get_ident())
            slot.__set__(control, value.upper())

        Custom.text = property(lambda control: slot.__get__(control), set_text)
        control = self.keep(Custom(text="Before"))
        owner = call(threading.get_ident)
        control.text = "after"
        self.assertEqual(observed, [owner])
        self.assertEqual(control.text, "AFTER")

    def test_custom_attribute_access_runs_only_after_owner_fallback(self):
        observed = []

        class Custom(Label):
            def __getattribute__(self, name):
                if name == "_schema":
                    observed.append(threading.get_ident())
                return super().__getattribute__(name)

        control = self.keep(Custom(text="Before"))
        owner = call(threading.get_ident)
        observed.clear()
        control.text = "After"
        self.assertTrue(observed)
        self.assertEqual(set(observed), {owner})
        self.assertEqual(control.text, "After")

    def test_instance_live_check_is_preserved_for_reads_and_writes(self):
        control = self.keep(Label(text="Before"))
        original = control._check_live
        observed = []
        owner = call(threading.get_ident)

        def check_live():
            observed.append(threading.get_ident())
            original()

        call(setattr, control, "_check_live", check_live)
        self.assertEqual(control.text, "Before")
        self.assertEqual(observed, [owner])
        observed.clear()
        control.text = "After"
        self.assertTrue(observed)
        self.assertEqual(set(observed), {owner})

    def test_writes_wait_for_a_synchronous_owner_method_and_reads_see_committed_values(self):
        entered, release = threading.Event(), threading.Event()

        class Custom(Container):
            def change(self):
                self.message.text = "Intermediate"
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release owner")
                self.message.text = "Committed"
                return self.message.text

        parent = self.keep(Custom())
        parent.message = Label(text="Before")
        label = parent.message
        read_started, write_started = threading.Event(), threading.Event()

        def read():
            read_started.set()
            return label.text

        def write():
            write_started.set()
            label.text = "Later"

        with ThreadPoolExecutor(3) as pool:
            change = pool.submit(parent.change)
            try:
                self.assertTrue(entered.wait(1))
                reader, writer = pool.submit(read), pool.submit(write)
                self.assertTrue(read_started.wait(1))
                self.assertTrue(write_started.wait(1))
                # A read is one lookup of the committed value: it returns the
                # value published so far instead of queueing behind the owner.
                self.assertEqual(reader.result(1), "Intermediate")
                with self.assertRaises(FutureTimeout):
                    writer.result(0.03)
            finally:
                release.set()
            self.assertEqual(change.result(2), "Committed")
            writer.result(2)
        self.assertEqual(label.text, "Later")

    def test_each_async_segment_excludes_writes_but_await_releases_them(self):
        entered = [threading.Event(), threading.Event()]
        release = [threading.Event(), threading.Event()]
        awaiting = threading.Event()
        gate = asyncio.Event()

        class Custom(Container):
            async def change(self):
                for index in range(2):
                    self.message.text = f"Intermediate {index}"
                    entered[index].set()
                    if not release[index].wait(3):
                        raise TimeoutError("test did not release owner")
                    self.message.text = f"Committed {index}"
                    if index == 0:
                        awaiting.set()
                        await gate.wait()

        parent = self.keep(Custom())
        parent.message = Label(text="Before")
        label = parent.message

        with ThreadPoolExecutor(2) as pool:
            change = pool.submit(lambda: asyncio.run(parent.change()))
            try:
                for index in range(2):
                    self.assertTrue(entered[index].wait(1))
                    self.assertEqual(
                        pool.submit(lambda: label.text).result(1), f"Intermediate {index}"
                    )
                    started = threading.Event()

                    def write(index=index):
                        started.set()
                        label.text = f"Worker {index}"

                    writer = pool.submit(write)
                    self.assertTrue(started.wait(1))
                    with self.assertRaises(FutureTimeout):
                        writer.result(0.03)
                    release[index].set()
                    writer.result(2)
                    if index == 0:
                        self.assertTrue(awaiting.wait(1))
                        label.text = "Accessible while awaiting"
                        self.assertEqual(label.text, "Accessible while awaiting")
                        get_engine().loop.call_soon_threadsafe(gate.set)
                change.result(2)
            finally:
                for signal in release:
                    signal.set()
                get_engine().loop.call_soon_threadsafe(gate.set)
        self.assertEqual(label.text, "Worker 1")

    def test_plain_event_loop_callback_excludes_external_writes(self):
        control = self.keep(Label(text="Before"))
        entered, release, finished = (threading.Event() for _ in range(3))
        errors = []

        def callback():
            try:
                control.text = "Intermediate"
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test did not release owner")
                control.text = "Committed"
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        get_engine().loop.call_soon_threadsafe(callback)
        with ThreadPoolExecutor(1) as pool:
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(pool.submit(lambda: control.text).result(1), "Intermediate")
                started = threading.Event()

                def write():
                    started.set()
                    control.text = "External"

                writer = pool.submit(write)
                self.assertTrue(started.wait(1))
                with self.assertRaises(FutureTimeout):
                    writer.result(0.03)
            finally:
                release.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual(errors, [])
            writer.result(2)
        self.assertEqual(control.text, "External")

    def test_destroyed_control_rejects_schema_reads_and_writes(self):
        control = Label(text="Before")
        control.destroy()
        with self.assertRaises(LifecycleError):
            _ = control.text
        with self.assertRaises(LifecycleError):
            control.text = "After"

    def test_stale_invocation_rejects_fast_reads_and_writes_on_descendants(self):
        window = self.keep(Window())
        label = Label(parent=window, text="Before")
        call(lambda: setattr(window, "_presentation", Presentation(1)))
        origin = call(capture_origin, window)
        call(lambda: setattr(window, "_presentation", Presentation(2)))
        token = _origin.set(origin)
        try:
            with self.assertRaisesRegex(LifecycleError, "reopened"):
                _ = label.text
            with self.assertRaisesRegex(LifecycleError, "reopened"):
                label.text = "After"
        finally:
            _origin.reset(token)
        self.assertEqual(label.text, "Before")

    def test_changed_callbacks_keep_every_transition_and_run_on_owner(self):
        control = self.keep(TextBox(text="Before"))
        dispatcher = Dispatcher()
        self.addCleanup(lambda: asyncio.run(call_async(dispatcher.shutdown)))
        call(control._attach_dispatcher, dispatcher)
        observed = []
        control.changed.connect(lambda event: observed.append(
            (event.old_value, event.new_value, threading.get_ident())
        ))
        owner = call(threading.get_ident)
        for value in ("One", "Two", "Three"):
            control.text = value
        asyncio.run(call_async(dispatcher.drain))
        self.assertEqual(observed, [
            ("Before", "One", owner),
            ("One", "Two", owner),
            ("Two", "Three", owner),
        ])


if __name__ == "__main__":
    unittest.main()
