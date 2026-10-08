"""Draft content composes with inline hosts and the ordinary popup lifecycle."""

import asyncio
from typing import ClassVar

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost, eventually

from pysual import App, Button, Container, Control, NumericInput, TextBox, prop
from pysual.editing import EditorContent, EditorPopup
from pysual.editors import ColorEditor, NumericEditor
from pysual.errors import LifecycleError
from pysual.events import ChangeEvent, Event
from pysual.host import Input
from pysual.runtime import Runtime


class NameEditor(EditorContent):
    """A third content implementation that needs no changes in EditorPopup."""

    hint = "Enter a name"

    def build(self, value):
        self.entry = TextBox(text=value, height=38)
        self.entry.select_all()

    def read(self):
        name = self.entry.text.strip()
        if not name:
            raise ValueError("A name cannot be empty")
        return name

    def equivalent(self, value, current):
        return value.casefold() == current.casefold()

    def error_message(self, error, owner):
        return "Choose another name."

    def entry_on_key_down(self, event):
        if event.key == "Enter":
            self.submit()


class NamedControl(Control):
    filename: str = prop(default="Original", changed="changed")
    changed: ClassVar[Event[ChangeEvent[str]]] = Event(ChangeEvent)

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "filename" and value == "reserved":
            raise ValueError("This name is reserved by the owner")


class InlineEditorTests(UIOwnerTestCase):
    def test_numeric_color_and_custom_content_mount_as_independent_children(self):
        host = Container(layout="stack")
        self.addCleanup(host.destroy)
        number = NumericEditor(1 / 3, parent=host)
        other = NumericEditor(1 / 3, decimals=4, parent=host)
        color = ColorEditor("#123456", parent=host)
        name = NameEditor("Original", parent=host)
        self.assertEqual(host.children, (number, other, color, name))
        self.assertEqual(number.entry.text, "0.33")
        self.assertEqual(other.entry.text, "0.3333")
        self.assertTrue(number.is_untouched())
        number.entry.replace_selection("0.75")
        self.assertEqual(number.read(), 0.75)
        self.assertFalse(number.is_untouched())
        self.assertTrue(other.is_untouched())
        color.entry.replace_selection("#aabbccdd")
        self.assertEqual(color.read(), "#AABBCCDD")
        self.assertEqual([s.value for s in color._channels_rgba], [170, 187, 204, 221])
        self.assertEqual(name.read(), "Original")
        number.destroy()
        self.assertFalse(other._disposed)
        self.assertEqual(other.entry.text, "0.3333")

    def test_failed_open_disposes_content_and_mounted_content_is_not_adopted(self):
        owner = NamedControl()
        self.addCleanup(owner.destroy)
        content = NameEditor(owner.filename)
        entry = content.entry
        popup = EditorPopup(owner, content, value_property="filename")
        with self.assertRaisesRegex(LifecycleError, "running App"):
            popup.show(owner)
        self.assertTrue(popup._disposed and content._disposed and entry._disposed)

        host = Container()
        self.addCleanup(host.destroy)
        inline = NameEditor("Inline", parent=host)
        with self.assertRaisesRegex(ValueError, "unmounted"):
            EditorPopup(owner, inline, value_property="filename")
        self.assertIs(inline.parent, host)
        self.assertFalse(inline._disposed)

    def test_content_build_failure_disposes_created_children(self):
        created = []

        class BrokenEditor(EditorContent):
            def build(self, value):
                created.append(TextBox(parent=self, text="draft"))
                raise ValueError("Cannot build draft")

        with self.assertRaisesRegex(ValueError, "Cannot build"):
            BrokenEditor("initial")
        self.assertTrue(created[0]._disposed)

    def test_popup_construction_failure_disposes_content_after_attachment_rollback(self):
        class UnattachableEditor(NameEditor):
            def on_attached(self):
                raise ValueError("Cannot attach this draft")

        owner = NamedControl()
        self.addCleanup(owner.destroy)
        content = UnattachableEditor("Original")
        entry = content.entry
        with self.assertRaisesRegex(ValueError, "Cannot attach"):
            EditorPopup(owner, content, value_property="filename")
        self.assertTrue(content._disposed and entry._disposed)

    def test_popup_feedback_construction_failure_disposes_already_adopted_content(self):
        parents = []

        class InvalidHintEditor(NameEditor):
            hint = None

            def on_attached(self):
                parents.append(self.parent)

        owner = NamedControl()
        self.addCleanup(owner.destroy)
        content = InvalidHintEditor("Original")
        entry = content.entry
        with self.assertRaises(TypeError):
            EditorPopup(owner, content, value_property="filename")
        self.assertTrue(content._disposed and entry._disposed)
        self.assertTrue(parents[0]._disposed)

    def test_popup_rejects_nonproperty_targets_without_consuming_content(self):
        owner = NamedControl()
        self.addCleanup(owner.destroy)
        content = NameEditor("Original")
        self.addCleanup(content.destroy)
        for target in ("unknown", "destroy", "_origin", "parent"):
            with self.subTest(target=target):
                with self.assertRaisesRegex(ValueError, "observable owner property"):
                    EditorPopup(owner, content, value_property=target)
                self.assertIsNone(content.parent)
                self.assertFalse(content._disposed)


class EditorPopupCompositionTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App()
        self.owner = NamedControl(parent=self.app, width=180, height=40)
        self.runtime = Runtime(self.app, RecordingHost())
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task
        self.app.destroy()

    def popup(self):
        content = NameEditor(self.owner.filename)
        popup = EditorPopup(self.owner, content, value_property="filename")
        popup.show(self.owner)
        return popup

    async def test_custom_draft_rejection_then_submission_preserves_origin_and_events(self):
        changes = []
        self.owner.changed.connect(lambda event: changes.append(event))
        popup = self.popup()
        for invalid, detail in (("", "cannot be empty"), ("reserved", "reserved")):
            popup.content.entry.select_all()
            popup.content.entry.replace_selection(invalid)
            popup.commit()
            self.assertIs(self.runtime.popup, popup)
            self.assertEqual(popup.content.entry.text, invalid)
            self.assertEqual(popup.message.text, "Choose another name.")
            self.assertIn(detail, popup.message.tooltip)
            self.assertEqual(self.owner.filename, "Original")
            self.assertEqual(self.owner._origin, "program")
        await self.runtime.dispatcher.drain()
        self.assertEqual(changes, [])
        popup.content.entry.select_all()
        popup.content.entry.replace_selection(" Changed ")
        self.runtime.router.process(Input("key_down", key="Enter"))
        await self.runtime.dispatcher.drain()
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(self.owner.filename, "Changed")
        self.assertEqual(self.owner._origin, "program")
        self.assertEqual([(e.old_value, e.new_value, e.origin) for e in changes], [
            ("Original", "Changed", "user"),
        ])

    async def test_escape_and_semantic_equality_do_not_commit(self):
        changes = []
        self.owner.changed.connect(lambda event: changes.append(event))
        popup = self.popup()
        popup.content.entry.replace_selection("Cancelled")
        self.runtime.router.process(Input("key_down", key="Escape"))
        self.assertIsNone(self.runtime.popup)
        self.assertTrue(popup._disposed)
        self.assertEqual(self.owner.filename, "Original")
        popup = self.popup()
        popup.content.entry.replace_selection("original")
        popup.commit()
        await self.runtime.dispatcher.drain()
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(self.owner.filename, "Original")
        self.assertEqual(changes, [])

    async def test_current_owner_bounds_validate_the_draft(self):
        owner = NumericInput(parent=self.app, value=5, maximum=10, width=180, height=40)
        popup = EditorPopup(owner, NumericEditor(owner.value))
        popup.show(owner)
        owner.maximum = 6
        popup.content.entry.replace_selection("8")
        popup.commit()
        self.assertIs(self.runtime.popup, popup)
        self.assertEqual(popup.message.text, "Enter a number at most 6.")
        self.assertEqual(owner.value, 5)

    async def test_explicit_equal_value_still_reaches_owner_validation(self):
        class GuardedNumber(NumericInput):
            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if name == "value" and getattr(self, "_reject_values", False):
                    raise ValueError("Editing is locked")

        owner = GuardedNumber(parent=self.app, value=5, width=180, height=40)
        popup = EditorPopup(owner, NumericEditor(owner.value))
        popup.show(owner)
        owner._reject_values = True
        popup.content.entry.replace_selection("5")
        popup.commit()
        self.assertIs(self.runtime.popup, popup)
        self.assertEqual(popup.message.text, "Editing is locked")
        self.assertEqual(owner._origin, "program")

    async def test_inline_submission_has_no_implicit_owner_commit(self):
        content = NameEditor("Inline", parent=self.app)
        submitted = []
        content.submitted.connect(lambda event: submitted.append(content.read()))
        content.entry.replace_selection("Updated")
        content.submit()
        await self.runtime.dispatcher.drain()
        self.assertEqual(submitted, ["Updated"])
        self.assertEqual(self.owner.filename, "Original")

    async def test_reopening_an_open_popup_does_not_dispose_it(self):
        popup = self.popup()
        with self.assertRaisesRegex(LifecycleError, "fresh Popup"):
            popup.show(self.owner)
        self.assertIs(self.runtime.popup, popup)
        self.assertFalse(popup.content._disposed)

    async def test_distinct_anchor_is_rejected_and_fresh_content_is_disposed(self):
        anchor = Button(parent=self.app, text="Unrelated", top=50, width=180, height=40)
        content = NameEditor(self.owner.filename)
        entry = content.entry
        popup = EditorPopup(self.owner, content, value_property="filename")
        with self.assertRaisesRegex(ValueError, "anchored to its value owner"):
            popup.show(anchor)
        self.assertTrue(popup._disposed and content._disposed and entry._disposed)
        self.assertFalse(anchor._disposed)
        self.assertIs(anchor.parent, self.app)
        self.assertEqual(self.owner.filename, "Original")
        self.assertIsNone(self.runtime.popup)

    async def test_disposing_the_value_owner_closes_its_editor_through_popup_lifecycle(self):
        popup = self.popup()
        content = popup.content
        entry = content.entry
        entry.replace_selection("Uncommitted")
        self.owner.destroy()
        await eventually(lambda: self.runtime.popup is None)
        self.runtime.dispatcher.raise_errors()
        self.assertTrue(popup._disposed and content._disposed and entry._disposed)
