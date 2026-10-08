import asyncio
import unittest
from _ui_testcase import AsyncUIOwnerTestCase
from unittest.mock import patch
from pysual import App, ColorPicker, NumericInput
from pysual.backends._font import measure
from pysual.events import ChangeEvent
from pysual.host import Input
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


class NumericValueTests(unittest.TestCase):
    def test_increments_keep_exact_nonround_bounds(self):
        for boundary, direction in (
            (1.23456789016, 1),
            (-1.23456789016, -1),
            (-1.23456789016, 1),
            (1.23456789016, -1),
        ):
            with self.subTest(boundary=boundary, direction=direction):
                bounds = {"maximum" if direction > 0 else "minimum": boundary}
                editor = NumericInput(value=boundary, **bounds)
                try:
                    editor.increment(direction)
                    self.assertEqual(editor.value, boundary)
                    editor.increment(-direction)
                    editor.increment(10 * direction)
                    self.assertEqual(editor.value, boundary)
                finally:
                    editor.destroy()

        editor = NumericInput(value=0, step=0.1)
        try:
            for _ in range(10):
                editor.increment()
            self.assertEqual(editor.value, 1)
        finally:
            editor.destroy()


class ValueEditorTests(AsyncUIOwnerTestCase):
    async def test_color_apply_preserves_untouched_and_equivalent_values(self):
        app = App()
        picker = app.color_picker(value="#5c6bd4", width=200, height=40)
        changes = []
        picker.changed.connect(lambda event: changes.append(event.new_value))
        runtime = Runtime(app, RecordingHost())
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            for action in ("untouched", "external", "case", "rgba", "channel_back", "alpha", "new"):
                with self.subTest(action=action):
                    picker.value = "#5c6bd4"
                    await runtime.dispatcher.drain()
                    picker.edit()
                    popup = runtime.popup
                    expected = "#5c6bd4"
                    if action == "external":
                        picker.value = expected = "#112233"
                    elif action in ("case", "rgba", "new"):
                        value = {"case": "#5C6BD4", "rgba": "#5C6BD4FF", "new": "#123abc"}[action]
                        popup.entry.replace_selection(value)
                        if action == "new":
                            expected = "#123ABC"
                    elif action in ("channel_back", "alpha"):
                        runtime.router.set_focus(popup._channels_rgba[3])
                        runtime.router.process(Input("key_down", key="ArrowLeft"))
                        if action == "channel_back":
                            runtime.router.process(Input("key_down", key="ArrowRight"))
                        else:
                            expected = "#5C6BD4FE"
                    await runtime.dispatcher.drain()
                    changes.clear()
                    popup.commit()
                    await runtime.dispatcher.drain()
                    self.assertEqual(picker.value, expected)
                    self.assertEqual(changes, [expected] if action in ("new", "alpha") else [])
                    self.assertIsNone(runtime.popup)
        finally:
            runtime._stop.set()
            await task

    async def test_numeric_apply_distinguishes_explicit_reentry_from_untouched(self):
        app = App()
        number = app.numeric_input(value=1 / 3, decimals=2, width=200, height=40)
        runtime = Runtime(app, RecordingHost())
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            for action in ("untouched", "external", "reenter", "edit_back", "undo"):
                with self.subTest(action=action):
                    number.value = 1 / 3
                    number.edit()
                    popup = runtime.popup
                    if action == "external":
                        number.value = 0.8
                    elif action == "reenter":
                        popup.entry.replace_selection("0.33")
                    elif action in ("edit_back", "undo"):
                        popup.entry.replace_selection("0.34")
                        if action == "undo":
                            popup.entry.undo()
                        else:
                            popup.entry.select_all()
                            popup.entry.replace_selection("0.33")
                    popup.commit()
                    expected = 0.8 if action == "external" else (
                        0.33 if action in ("reenter", "edit_back") else 1 / 3
                    )
                    self.assertEqual(number.value, expected)
                    self.assertIsNone(runtime.popup)
        finally:
            runtime._stop.set()
            await task

    async def test_validation_feedback_is_concise_elided_and_keeps_full_details(self):
        class FontHost(RecordingHost):
            def measure(self, text, size, mono=False):
                return measure(text, size, mono)

        class CustomNumber(NumericInput):
            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if name == "value" and value == 3:
                    raise ValueError("Choose another value: three is reserved for the application")

        app, host = App(), FontHost()
        number = CustomNumber(parent=app, value=5, minimum=0, maximum=10, width=200, height=40)
        color = app.color_picker(value="#123456", top=60, width=200, height=40)
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            cases = (
                (number, "20", "Enter a number from 0 to 10.", "minimum <= value"),
                (number, "nan", "Enter a finite number.", "finite"),
                (number, "blue", "Enter a finite number.", "float"),
                (color, "blue", "Use #RRGGBB or #RRGGBBAA.", "Expected #RRGGBB"),
                (number, "3", "Choose another value: three is reserved for the application", "reserved"),
            )
            for owner, invalid, message, detail in cases:
                with self.subTest(invalid=invalid, owner=type(owner).__name__):
                    owner.edit()
                    popup = runtime.popup
                    popup.entry.replace_selection(invalid)
                    popup.commit()
                    self.assertIs(runtime.popup, popup)
                    self.assertEqual(popup.message.text, message)
                    self.assertIn(detail, popup.message.tooltip)
                    self.assertEqual(number.value, 5)
                    self.assertEqual(color.value, "#123456")
                    for width in (300, 190):
                        popup.width = width
                        runtime._paint_frame()
                        painted = [text for text in host.texts if text.startswith(message[:4])]
                        self.assertEqual(len(painted), 1)
                        self.assertLessEqual(measure(painted[0], 12)[0], popup.message.bounds.width)
                    popup.dismiss()
        finally:
            runtime._stop.set()
            await task

    async def test_color_text_and_queued_slider_input_stay_in_sync(self):
        # Clock resolution must not decide the ordering of edits. Force every
        # property notification to have the same timestamp on every platform.
        notifications = patch(
            "pysual.controls.ChangeEvent",
            side_effect=lambda **kwargs: ChangeEvent(timestamp=1.0, **kwargs),
        )
        notifications.start()
        self.addCleanup(notifications.stop)
        app = App()
        tint = app.color_picker(value="#123456", width=200, height=40)
        runtime = Runtime(app, RecordingHost())
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            tint.edit()
            popup = runtime.popup
            red, green, blue, _alpha = popup._channels_rgba

            popup.entry.focus()
            popup.entry.select_all()
            runtime.router.process(Input("text", text="#11223344"))
            self.assertEqual([s.value for s in popup._channels_rgba], [17, 34, 51, 68])
            red.focus()
            runtime.router.process(Input("key_down", key="ArrowRight"))
            self.assertEqual(popup.entry.text, "#12223344")
            self.assertTrue(popup.entry.can_undo)
            self.assertEqual(popup.entry.selection_range, (9, 9))
            await runtime.dispatcher.drain()
            self.assertEqual(popup.entry.text, "#12223344")

            # A queued slider notification must not undo a later text edit.
            runtime.router.process(Input("key_down", key="ArrowRight"))
            popup.entry.focus()
            popup.entry.select_all()
            runtime.router.process(Input("text", text="#aAbBcCdD"))
            await runtime.dispatcher.drain()
            self.assertEqual(popup.entry.text.lower(), "#aabbccdd")
            self.assertEqual(
                [s.value for s in popup._channels_rgba], [170, 187, 204, 221]
            )

            red.focus()
            runtime.router.process(Input("key_down", key="ArrowRight"))
            popup.entry.load_text("#12")
            await runtime.dispatcher.drain()
            self.assertEqual(popup.entry.text, "#12")

            popup.entry.load_text("#102030")
            self.assertEqual([s.value for s in popup._channels_rgba], [16, 32, 48, 255])
            popup.entry.load_text("#12")
            self.assertEqual([s.value for s in popup._channels_rgba], [16, 32, 48, 255])
            green.focus()
            runtime.router.process(Input("key_down", key="ArrowRight"))
            blue.focus()
            runtime.router.process(Input("key_down", key="ArrowLeft"))
            await runtime.dispatcher.drain()
            self.assertEqual(popup.entry.text, "#10212FFF")
            self.assertEqual(tint.value, "#123456")
            popup.commit()
            self.assertEqual(tint.value, "#10212FFF")
        finally:
            runtime._stop.set()
            await task

    async def test_tab_cycles_within_value_popups(self):
        app = App()
        number = app.numeric_input(width=200, height=40)
        tint = app.color_picker(top=60, width=200, height=40)
        runtime = Runtime(app, RecordingHost())
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            for owner in (number, tint):
                owner.edit()
                popup = runtime.popup
                expected = [*popup._channels_rgba, popup.accept_button, popup.entry]
                self.assertTrue(popup.entry.focused)
                for target in expected:
                    runtime.router.process(Input("key_down", key="Tab"))
                    self.assertIs(runtime.popup, popup)
                    self.assertTrue(target.focused)
                runtime.router.process(Input("key_down", key="Tab", shift=True))
                self.assertTrue(popup.accept_button.focused)
                runtime.router.process(Input("key_down", key="Escape"))
                self.assertIsNone(runtime.popup)
        finally:
            runtime._stop.set()
            await task

    async def test_numeric_bounds_invalid_commit_cancel_and_user_origin(self):
        class Demo(App):
            def build(self):
                self.amount = NumericInput(
                    value=5, minimum=0, maximum=10, width=200, height=40
                )
                self.tint = ColorPicker(value="#123456", top=60, width=200, height=40)

        app = Demo()
        host = RecordingHost()
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        changes = []
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            app.amount.changed.connect(
                lambda e: changes.append((e.new_value, e.origin))
            )
            app.amount.focus()
            host.events.append(Input("key_down", key="ArrowUp", shift=True))
            await eventually(lambda: app.amount.value == 10)
            await runtime.dispatcher.drain()
            self.assertIn((10, "user"), changes)
            app.amount.edit()
            popup = runtime.popup
            popup.entry.load_text("nan")
            popup.commit()
            self.assertTrue(popup.is_open)
            self.assertEqual(app.amount.value, 10)
            popup.entry.load_text("20")
            popup.commit()
            self.assertTrue(popup.is_open)
            host.events.append(Input("key_down", key="Escape"))
            await eventually(lambda: runtime.popup is None)
            self.assertEqual(app.amount.value, 10)
            app.amount.edit()
            runtime.popup.entry.load_text("3.25")
            runtime.popup.commit()
            await eventually(lambda: (3.25, "user") in changes)
            app.tint.changed.connect(lambda e: changes.append((e.new_value, e.origin)))
            app.tint.edit()
            popup = runtime.popup
            popup.entry.load_text("#11223380")
            popup.commit()
            await eventually(lambda: ("#11223380", "user") in changes)
            self.assertEqual(app.tint.value, "#11223380")
            app.tint.edit()
            popup = runtime.popup
            app.tint.destroy()
            await eventually(lambda: runtime.popup is None)
        finally:
            runtime._stop.set()
            await task
