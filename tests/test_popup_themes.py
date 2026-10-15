"""Open popups follow their anchor's theme without replacing draft content."""

import asyncio
from dataclasses import replace

from _ui_testcase import AsyncUIOwnerTestCase
from test_library import RecordingHost, eventually

from pysual import App, Button, Dropdown, NumericInput, Popup, get_theme
from pysual.host import Input
from pysual.painting import resolve_style
from pysual.runtime import Runtime


class PopupThemeTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=600, height=400, theme=get_theme("modern"), reduce_motion=True)
        self.runtime = Runtime(self.app, RecordingHost())
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task
        self.app.destroy()

    async def test_generic_popup_follows_app_and_anchor_metrics_without_losing_input(self):
        owner = Button(parent=self.app, width=180, height=38)
        self.runtime._paint_frame()
        popup = Popup(width=240, layout="stack")
        entry = popup.text_box(text="Initial")
        popup.show(owner)
        self.runtime._paint_frame()
        entry.text = "Unsaved draft"
        entry.select_all()
        previous_height = entry.bounds.height
        dark = get_theme("modern_dark")
        larger = replace(dark, tokens=replace(dark.tokens, control_height=56, font_size=22))
        for theme, anchor_override in ((larger, False), (get_theme("modern"), True)):
            with self.subTest(anchor_override=anchor_override):
                if anchor_override:
                    owner.theme_override = theme
                else:
                    self.app.theme = theme
                self.runtime._paint_frame()
                self.assertIs(self.runtime.popup, popup)
                self.assertEqual(popup.effective_theme, theme)
                self.assertEqual(resolve_style(popup).fill, theme.resolve("Popup").fill)
                self.assertEqual(entry.text, "Unsaved draft")
                self.assertEqual(entry.selection_text, "Unsaved draft")
                self.assertTrue(entry.focused)
                self.assertNotEqual(entry.bounds.height, previous_height)
                previous_height = entry.bounds.height
        self.runtime._paint_frame()
        popup.reconcile()
        self.assertFalse(self.runtime._dirty)

    async def test_dropdown_keeps_pending_choice_across_theme_replacements(self):
        owner = Dropdown(parent=self.app, width=180, height=38,
                         items=("First", "Second"), selected_index=0)
        self.runtime._paint_frame()
        owner.open()
        self.runtime._paint_frame()
        popup = self.runtime.popup
        choices = popup.children[0]
        self.runtime.router.process(Input("key_down", key="ArrowDown"))
        for theme, anchor_override in ((get_theme("modern_dark"), False),
                                       (get_theme("modern"), True)):
            with self.subTest(anchor_override=anchor_override):
                if anchor_override:
                    owner.theme_override = theme
                else:
                    self.app.theme = theme
                self.runtime._paint_frame()
                self.assertIs(self.runtime.popup, popup)
                self.assertEqual(choices.effective_theme, theme)
                self.assertEqual(choices.selected_index, 1)
                self.assertEqual(owner.selected_index, 0)
                self.assertTrue(choices.focused)
        self.runtime.router.process(Input("key_down", key="Enter"))
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(owner.selected_index, 1)

    async def test_numeric_editor_keeps_draft_and_commit_after_owner_theme_changes(self):
        owner = NumericInput(parent=self.app, width=180, height=38, value=5)
        self.runtime._paint_frame()
        owner.edit()
        self.runtime._paint_frame()
        popup = self.runtime.popup
        popup.entry.select_all()
        popup.entry.replace_selection("8.75")
        owner.theme_override = get_theme("modern_dark")
        self.runtime._paint_frame()
        self.assertIs(self.runtime.popup, popup)
        self.assertEqual(popup.effective_theme, owner.effective_theme)
        self.assertEqual(popup.entry.text, "8.75")
        self.assertTrue(popup.entry.focused)
        self.assertEqual(owner.value, 5)
        popup.commit()
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(owner.value, 8.75)
