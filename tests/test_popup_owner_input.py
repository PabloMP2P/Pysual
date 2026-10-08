"""Popup dismissal preserves owner edits without reactivating popup openers."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from pysual import (
    App, Button, ComboBox, Control, DataGrid, Dropdown, GridColumn, GridRow,
    NumericInput, TextBox, get_theme,
)
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import Painter, resolve_style
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


class PopupOwnerInputTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=600, height=400, reduce_motion=True)
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task
        self.app.destroy()

    def layout(self):
        arrange(self.app, self.host)

    def pointer(self, control, kind, x=12, **kwargs):
        self.runtime.router.process(Input(
            kind, control.bounds.x + x, control.bounds.y + 15, **kwargs,
        ))

    def click(self, control, x=12):
        self.pointer(control, "pointer_down", x)
        self.pointer(control, "pointer_up", x)

    def combo(self, **kwargs):
        return ComboBox(parent=self.app, left=20, top=20, width=200, height=38,
                        text="abcdef", **kwargs)

    async def test_combo_text_dismisses_choices_and_starts_selection(self):
        combo = self.combo(items=("abcdef", "other"))
        self.layout()
        combo.select_all()
        combo.open()
        self.layout()
        popup = self.runtime.popup
        self.pointer(combo, "pointer_down")
        self.assertTrue(popup._disposed)
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(combo.selection_range, (0, 0))
        self.assertIs(self.runtime.router.capture, combo)
        self.pointer(combo, "pointer_move", 48)
        self.pointer(combo, "pointer_up", 48)
        self.assertGreater(combo.selection_range[1], 0)
        self.assertIsNone(self.runtime.router.capture)

    async def test_choice_openers_stay_closed_through_release_and_cancellation(self):
        for cls in (ComboBox, Dropdown):
            for cancel in (None, "pointer_cancel", "blur"):
                with self.subTest(control=cls.__name__, cancel=cancel):
                    owner = cls(parent=self.app, left=20, top=20, width=200,
                                height=38, items=("First", "Second"))
                    self.layout()
                    owner.open()
                    self.layout()
                    x = 190 if cls is ComboBox else 50
                    self.pointer(owner, "pointer_down", x)
                    self.assertIsNone(self.runtime.popup)
                    if cancel:
                        self.runtime.router.process(Input(cancel))
                    self.pointer(owner, "pointer_up", x)
                    self.assertIsNone(self.runtime.popup)
                    self.click(owner, x)
                    self.assertIsNotNone(self.runtime.popup)
                    self.runtime.popup.dismiss()
                    owner.destroy()

    async def test_numeric_step_discards_draft_and_uses_same_click(self):
        number = NumericInput(parent=self.app, value=5, left=20, top=20,
                              width=200, height=38)
        changes = []
        number.changed.connect(changes.append)
        self.layout()
        for x, expected in ((190, 6), (12, 5)):
            number.edit()
            popup = self.runtime.popup
            popup.entry.replace_selection("99")
            self.layout()
            self.click(number, x)
            self.assertIsNone(self.runtime.popup)
            self.assertTrue(popup._disposed)
            self.assertEqual(number.value, expected)
        await self.runtime.dispatcher.drain()
        self.assertEqual([(e.old_value, e.new_value, e.origin) for e in changes],
                         [(5, 6, "user"), (6, 5, "user")])

    async def test_numeric_dismissal_keeps_center_closed_and_cancelled_step_idle(self):
        number = NumericInput(parent=self.app, value=5, left=20, top=20,
                              width=200, height=38)
        self.layout()
        for cancel in ("pointer_cancel", "blur"):
            number.edit()
            self.runtime.popup.entry.replace_selection("99")
            self.layout()
            self.pointer(number, "pointer_down", 190)
            self.runtime.router.process(Input(cancel))
            self.pointer(number, "pointer_up", 190)
            self.assertIsNone(self.runtime.popup)
            self.assertEqual(number.value, 5)
        number.edit()
        self.runtime.popup.entry.replace_selection("99")
        self.layout()
        self.click(number, 100)
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(number.value, 5)

    async def test_press_on_other_control_only_dismisses(self):
        combo = self.combo(items=("abcdef",))
        button = Button(parent=self.app, left=300, top=20, width=200, height=38)
        clicks = []
        button.click.connect(clicks.append)
        self.layout()
        combo.open()
        self.layout()
        self.click(button)
        await self.runtime.dispatcher.drain()
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(clicks, [])
        self.click(button)
        await self.runtime.dispatcher.drain()
        self.assertEqual(len(clicks), 1)

    async def test_unavailable_combo_arrow_places_caret(self):
        for properties in ({"items": ()}, {"items": ("abcdef",), "read_only": True}):
            with self.subTest(properties=properties):
                combo = self.combo(**properties)
                self.layout()
                combo.select_all()
                self.click(combo, 190)
                self.assertIsNone(self.runtime.popup)
                self.assertEqual(combo.selection_range, (6, 6))
                combo.destroy()

    async def test_clipboard_keys_reach_custom_control_and_textbox_still_edits(self):
        class KeyControl(Control):
            def _initialize(self):
                super()._initialize()
                self._keys = []

            def handle_input(self, event, /):
                if event.kind == "key_down":
                    self._keys.append((event.key, event.ctrl))

        custom = KeyControl(parent=self.app, focusable=True, width=200, height=38)
        entry = TextBox(parent=self.app, text="copy me", top=50, width=200, height=38)
        self.layout()
        custom.focus()
        for key in ("c", "x", "v"):
            self.runtime.router.process(Input("key_down", key=key, ctrl=True))
        self.assertEqual(custom._keys, [(key, True) for key in ("c", "x", "v")])
        entry.focus()
        entry.select_all()
        for key in ("c", "x", "v"):
            self.runtime.router.process(Input("key_down", key=key, ctrl=True))
            await self.runtime.dispatcher.drain()
            self.assertEqual(self.host.clipboard, "copy me")
            self.assertEqual(entry.text, "" if key == "x" else "copy me")

    async def test_grid_copy_keeps_unsupported_clipboard_keys_available(self):
        class KeyGrid(DataGrid):
            def _initialize(self):
                super()._initialize()
                self._keys = []

            def handle_input(self, event, /):
                if event.kind == "key_down":
                    self._keys.append(event.key)
                super().handle_input(event)

        grid = KeyGrid(parent=self.app, width=300, height=160,
                       columns=(GridColumn("name", "Name"),),
                       rows=(GridRow("a", ("copied cell",)),), selected_key="a")
        self.layout()
        grid.focus()
        for key in ("c", "x", "v"):
            self.runtime.router.process(Input("key_down", key=key, ctrl=True))
            await self.runtime.dispatcher.drain()
        self.assertEqual(self.host.clipboard, "copied cell")
        self.assertEqual(grid._keys, ["x", "v"])


class ChoicePlaceholderTests(UIOwnerTestCase):
    def test_placeholder_is_muted_and_selected_item_uses_foreground(self):
        class TextHost(RecordingHost):
            def text(self, text, x, y, color, size, mono=False):
                self.texts.append((text, color))

        app, host = App(theme=get_theme("modern")), TextHost()
        self.addCleanup(app.destroy)
        dropdown = Dropdown(parent=app, items=("North",), placeholder="Choose")
        for selected, expected in ((-1, app.theme.tokens.muted), (0, resolve_style(dropdown).foreground)):
            dropdown.selected_index = selected
            arrange(app, host)
            host.texts.clear()
            dropdown.paint(Painter(host, dropdown.bounds, app.theme, dropdown))
            self.assertEqual(host.texts, [("Choose" if selected == -1 else "North", expected)])
