"""The date picker composes an ordinary calendar subtree and editor popup."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost, eventually

from pysual import App, Button, Container, Style, get_theme, theme_names
from pysual.dates import CalendarEditor, DatePicker
from pysual.editing import EditorPopup
from pysual.errors import LifecycleError
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree, resolve_style
from pysual.runtime import Runtime


def day_button(editor, value):
    return next(button for button in editor.days.children if button.tooltip == value)


class DateValueTests(UIOwnerTestCase):
    def keep(self, control):
        self.addCleanup(control.destroy)
        return control

    def test_iso_gregorian_validation_and_atomic_bounds(self):
        picker = self.keep(DatePicker(value="2024-02-29"))
        for invalid in ("2023-02-29", "1900-02-29", "2024-2-29", "20240229",
                        "2024-W09-4", "0000-01-01", "10000-01-01", "2024-13-01", ""):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(ValueError, "ISO date"):
                    picker.value = invalid
                self.assertEqual(picker.value, "2024-02-29")
        with self.assertRaises(TypeError):
            picker.value = 20240229
        picker.update(minimum="2000-01-01", maximum="2000-12-31", value="2000-02-29")
        before = picker.minimum, picker.maximum, picker.value, picker._paint_revision
        for update in (
            {"value": "2001-01-01", "read_only": True},
            {"minimum": "2001-01-01", "maximum": "1999-12-31"},
            {"minimum": "bad", "value": "2000-03-01"},
        ):
            with self.assertRaises(ValueError):
                picker.update(**update)
            self.assertEqual((picker.minimum, picker.maximum, picker.value, picker._paint_revision), before)
            self.assertFalse(picker.read_only)
        picker.update(minimum="9999-12-31", maximum="9999-12-31", value="9999-12-31")
        self.assertEqual(picker.value, "9999-12-31")

    def test_calendar_can_mount_inline_and_instances_keep_independent_drafts(self):
        host = self.keep(Container(layout="stack"))
        first = CalendarEditor("2024-02-29", parent=host)
        second = CalendarEditor("2024-02-29", parent=host)
        self.assertEqual(len(first.days.children), 42)
        self.assertEqual(len([b for b in first.days.children if b.visible]), 29)
        self.assertTrue(day_button(first, "2024-02-29").selected)
        first.select("2024-03-01")
        self.assertEqual(first.read(), "2024-03-01")
        self.assertEqual(second.read(), "2024-02-29")
        self.assertFalse(first.is_untouched())
        self.assertTrue(second.is_untouched())
        self.assertEqual(first.displayed_month, (2024, 3))
        first.destroy()
        self.assertEqual(second.read(), "2024-02-29")

    def test_calendar_navigation_clamps_bounds_and_year_endpoints_without_rebuilding(self):
        editor = self.keep(CalendarEditor(
            "2024-02-15", minimum="2024-02-10", maximum="2024-03-02",
        ))
        children = editor.days.children
        self.assertFalse(editor.previous_button.enabled)
        self.assertFalse(day_button(editor, "2024-02-09").enabled)
        self.assertTrue(day_button(editor, "2024-02-10").enabled)
        editor.navigate(500)
        self.assertEqual(editor.displayed_month, (2024, 3))
        self.assertFalse(editor.next_button.enabled)
        self.assertFalse(day_button(editor, "2024-03-03").enabled)
        self.assertEqual(editor.days.children, children)
        self.assertEqual(editor.read(), "2024-02-15")
        self.assertTrue(editor.is_untouched())
        with self.assertRaises(ValueError):
            editor.select("2024-03-03")
        with self.assertRaises(TypeError):
            editor.navigate(True)

        for initial, direction, expected in (("0001-01-01", -1, (1, 1)),
                                              ("9999-12-31", 1, (9999, 12))):
            with self.subTest(value=initial):
                endpoint = self.keep(CalendarEditor(initial))
                endpoint.navigate(direction)
                self.assertEqual(endpoint.displayed_month, expected)
                self.assertEqual(endpoint.read(), initial)
                self.assertTrue(day_button(endpoint, initial).selected)

    def test_reading_inline_typed_date_preserves_selection_and_undo(self):
        editor = self.keep(CalendarEditor("2024-02-28"))
        editor.entry.replace_selection("1990-10-07")
        before = editor.entry.selection_range, editor.entry.can_undo
        self.assertEqual(editor.read(), "1990-10-07")
        self.assertEqual((editor.entry.selection_range, editor.entry.can_undo), before)
        editor.entry.undo()
        self.assertEqual(editor.read(), "2024-02-28")

    def test_read_only_and_disabled_still_allow_programmatic_updates(self):
        for properties in ({"read_only": True}, {"enabled": False}):
            with self.subTest(properties=properties):
                picker = self.keep(DatePicker(**properties))
                picker.open()
                self.assertIsNone(picker._popup)
                picker.value = "2026-10-06"
                self.assertEqual(picker.value, "2026-10-06")
        active = self.keep(DatePicker())
        with self.assertRaisesRegex(LifecycleError, "running App"):
            active.open()
        self.assertTrue(active._popup._disposed)

    def test_calendar_and_picker_use_portable_painting(self):
        app = self.keep(App(width=430, height=430))
        DatePicker(parent=app, value="2024-02-29", width=200, height=40)
        CalendarEditor("2024-02-29", parent=app, top=50, width=380, height=388)
        host = RecordingHost()
        arrange(app, host)
        paint_tree(app, host)
        self.assertIn("2024-02-29", host.texts)
        self.assertIn("February 2024", host.texts)
        self.assertIn("29", host.texts)

    def test_calendar_days_use_neutral_and_selected_theme_roles_across_all_themes(self):
        for name in theme_names():
            with self.subTest(theme=name):
                theme = get_theme(name).styled(Button, Style(fill="#ff00ff", shadow="#123456"))
                editor = self.keep(CalendarEditor("2024-02-29", theme_override=theme))
                ordinary = resolve_style(day_button(editor, "2024-02-28"), "row")
                selected = resolve_style(day_button(editor, "2024-02-29"), "row", selected=True)
                self.assertEqual(ordinary.fill, theme.tokens.surface)
                self.assertNotEqual(selected.fill, ordinary.fill)
                self.assertNotEqual(ordinary.fill, "#ff00ff")
                self.assertIsNone(ordinary.shadow)


class DatePopupTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=440, height=500)
        self.picker = DatePicker(parent=self.app, value="2024-02-28", width=200, height=40)
        self.runtime = Runtime(self.app, RecordingHost())
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task
        self.app.destroy()

    async def test_direct_date_entry_accepts_historical_date_and_preserves_dismissal(self):
        self.picker.open()
        popup = self.runtime.popup
        popup.content.entry.focus()
        self.runtime.router.process(Input("text", text="1990-10-07", paste=True))
        self.assertEqual(self.picker.value, "2024-02-28")
        self.runtime.router.process(Input("key_down", key="Enter"))
        await self.runtime.dispatcher.drain()
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(self.picker.value, "1990-10-07")
        self.picker.open()
        popup = self.runtime.popup
        popup.content.entry.replace_selection("1980-01-01")
        popup.dismiss()
        self.assertEqual(self.picker.value, "1990-10-07")

    async def test_direct_date_entry_rejects_invalid_and_changed_bounds_then_corrects(self):
        self.picker.open()
        popup = self.runtime.popup
        entry = popup.content.entry
        for invalid in ("1990-", "1990-02-30", "19901007"):
            entry.load_text(invalid)
            popup.commit()
            self.assertIs(self.runtime.popup, popup)
            self.assertEqual(entry.text, invalid)
            self.assertIn("ISO date", popup.message.text)
            self.assertEqual(self.picker.value, "2024-02-28")
        entry.load_text("1990-10-07")
        self.picker.minimum = "2000-01-01"
        popup.commit()
        self.assertIs(self.runtime.popup, popup)
        self.assertEqual(entry.text, "1990-10-07")
        self.assertEqual(self.picker.value, "2024-02-28")
        entry.load_text("2001-10-07")
        popup.commit()
        self.assertEqual(self.picker.value, "2001-10-07")

    async def test_calendar_choice_replaces_invalid_text_and_navigation_keeps_untouched(self):
        self.picker.open()
        popup = self.runtime.popup
        popup.content.entry.text = "bad"
        day_button(popup.content, "2024-02-29").activate()
        self.assertEqual(popup.content.entry.text, "2024-02-29")
        self.assertEqual(popup.content.read(), "2024-02-29")
        popup.commit()
        self.assertEqual(self.picker.value, "2024-02-29")
        self.picker.open()
        popup = self.runtime.popup
        popup.content.navigate(-1)
        self.picker.value = "2024-03-01"
        popup.commit()
        self.assertEqual(self.picker.value, "2024-03-01")

    async def test_pointer_open_day_selection_apply_and_escape_have_single_owner_commit(self):
        events = []
        self.picker.changed.connect(lambda event: events.append(event))
        box = self.picker.bounds
        self.runtime.router.process(Input("pointer_down", x=box.x + 10, y=box.y + 10))
        self.runtime.router.process(Input("pointer_up", x=box.x + 10, y=box.y + 10))
        popup = self.runtime.popup
        self.assertIsInstance(popup, EditorPopup)
        self.assertIsInstance(popup.content, CalendarEditor)
        day_button(popup.content, "2024-02-29").activate()
        self.assertEqual(popup.content.read(), "2024-02-29")
        self.assertEqual(self.picker.value, "2024-02-28")
        popup.accept_button.activate()
        await self.runtime.dispatcher.drain()
        self.assertEqual(self.picker.value, "2024-02-29")
        self.assertIsNone(self.runtime.popup)
        self.assertEqual([(e.old_value, e.new_value, e.origin) for e in events], [
            ("2024-02-28", "2024-02-29", "user"),
        ])
        self.picker.open()
        self.runtime.popup.content.select("2024-03-01")
        self.runtime.router.process(Input("key_down", key="Escape"))
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(self.picker.value, "2024-02-29")

    async def test_day_keyboard_moves_focus_across_leap_month_and_enter_submits(self):
        self.picker.focus()
        self.runtime.router.process(Input("key_down", key="ArrowDown"))
        content = self.runtime.popup.content
        self.assertIs(self.runtime.router.focus, day_button(content, "2024-02-28"))
        for expected in ("2024-02-29", "2024-03-01"):
            self.runtime.router.process(Input("key_down", key="ArrowRight"))
            self.assertEqual(content.read(), expected)
            self.assertIs(self.runtime.router.focus, day_button(content, expected))
        self.runtime.router.process(Input("key_down", key="PageUp"))
        self.assertEqual(content.read(), "2024-02-01")
        self.runtime.router.process(Input("key_down", key="End"))
        self.assertEqual(content.read(), "2024-02-29")
        self.runtime.router.process(Input("key_down", key="Enter"))
        await self.runtime.dispatcher.drain()
        self.assertEqual(self.picker.value, "2024-02-29")
        self.assertIsNone(self.runtime.popup)

    async def test_keyboard_navigation_clamps_at_gregorian_and_configured_endpoints(self):
        for value, low, high, keys in (
            ("0001-01-01", "0001-01-01", "9999-12-31", ("ArrowLeft", "PageUp", "Home")),
            ("9999-12-31", "0001-01-01", "9999-12-31", ("ArrowRight", "PageDown", "End")),
            ("2024-02-20", "2024-02-20", "2024-02-20", ("ArrowDown", "PageUp", "Home", "End")),
        ):
            with self.subTest(value=value):
                self.picker.update(value=value, minimum=low, maximum=high)
                self.picker.open()
                content = self.runtime.popup.content
                for key in keys:
                    self.runtime.router.process(Input("key_down", key=key))
                    self.assertEqual(content.read(), value)
                self.runtime.popup.dismiss()

    async def test_live_bounds_reject_draft_and_untouched_calendar_preserves_external_value(self):
        self.picker.open()
        popup = self.runtime.popup
        popup.content.select("2024-03-01")
        self.picker.maximum = "2024-02-29"
        popup.commit()
        self.assertIs(self.runtime.popup, popup)
        self.assertEqual(popup.content.read(), "2024-03-01")
        self.assertIn("minimum <= value <= maximum", popup.message.text)
        self.assertEqual(self.picker.value, "2024-02-28")
        self.assertEqual(self.picker._origin, "program")
        popup.dismiss()
        self.picker.open()
        self.runtime.popup.content.navigate(-1)
        self.picker.value = "2024-02-29"
        self.runtime.popup.commit()
        self.assertEqual(self.picker.value, "2024-02-29")
        self.assertIsNone(self.runtime.popup)

    async def test_read_only_disabled_and_owner_disposal_close_owned_popups(self):
        for name, value in (("read_only", True), ("enabled", False)):
            self.picker.open()
            popup = self.runtime.popup
            setattr(self.picker, name, value)
            self.assertIsNone(self.runtime.popup)
            self.assertTrue(popup._disposed)
            self.picker.open()
            self.assertIsNone(self.runtime.popup)
            setattr(self.picker, name, not value)
        self.picker.open()
        popup = self.runtime.popup
        self.picker.destroy()
        self.assertIsNone(self.runtime.popup)
        self.assertTrue(popup._disposed)

    async def test_inline_submission_and_navigation_do_not_reinterpret_queued_day_selection(self):
        editor = CalendarEditor("2024-02-28", parent=self.app, top=50, width=380, height=342)
        submitted = []
        editor.submitted.connect(lambda event: submitted.append((editor.read(), event.origin)))
        # Both commands occur before queued button observers run. Selection is
        # synchronous, so a later month refresh cannot change what was chosen.
        editor.next_button.activate()
        day_button(editor, "2024-02-29").activate()
        await self.runtime.dispatcher.drain()
        self.assertEqual(editor.read(), "2024-02-29")
        editor.select("2024-03-01")
        day_button(editor, "2024-03-01").focus()
        self.runtime.router.process(Input("key_down", key="Enter"))
        await self.runtime.dispatcher.drain()
        self.assertEqual(submitted, [("2024-03-01", "user")])
        self.assertEqual(editor._origin, "program")
        self.assertEqual(self.picker.value, "2024-02-28")
