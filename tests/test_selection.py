"""Selection controls and transient subtree behavior through installed packages."""

import asyncio
from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from unittest.mock import patch
from pysual import App, Dropdown, ProgressBar, Style, Theme, Toggle
from pysual.errors import LifecycleError
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


class SelectionDataTests(UIOwnerTestCase):
    def test_progress_text_uses_disjoint_surface_clips_and_preserves_overrides(self):
        class Host(RecordingHost):
            def clip(self, rect):
                self.current_clip = rect

            def text(self, *args):
                self.draws.append((args, self.current_clip))

        theme = Theme().styled(ProgressBar, Style(foreground="#123456"), part="track")
        theme = theme.styled(ProgressBar, Style(foreground="#fedcba"), part="fill")
        app, host = App(), Host()
        self.addCleanup(app.destroy)
        bar = ProgressBar(parent=app, left=20, top=10, width=200, height=40,
                          theme_override=theme, font_family="mono", font_size=17)
        arrange(app, host)
        for value in (0, 25, 50, 75, 100):
            bar.value = value
            host.draws = []
            paint_tree(app, host)
            with self.subTest(value=value):
                self.assertEqual(len(host.draws), 1 if value in (0, 100) else 2)
                self.assertEqual({args[0] for args, _ in host.draws}, {f"{value}%"})
                self.assertEqual(len({args[1:3] for args, _ in host.draws}), 1)
                self.assertTrue(all(args[4:] == (17, True) for args, _ in host.draws))
                for args, clip in host.draws:
                    if args[3] == "#fedcba":
                        self.assertEqual((clip.x, clip.right), (20, 20 + value * 2))
                    else:
                        self.assertEqual(args[3], "#123456")
                        self.assertEqual((clip.x, clip.right), (20 + value * 2, 220))
                self.assertEqual(sum(clip.width for _, clip in host.draws), 200)
        bar.value, bar.foreground = 50, "#abcdef"
        host.draws = []
        paint_tree(app, host)
        self.assertEqual([args[3] for args, _ in host.draws], ["#abcdef", "#abcdef"])
        bar.show_text = False
        host.draws = []
        paint_tree(app, host)
        self.assertEqual(host.draws, [])

    def test_combo_resize_reveals_caret_but_repaint_preserves_manual_scroll(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        combo = app.combo_box(text="abcdefghijklmnopqrstuvwxyz" * 2, width=200)
        runtime.router.focus = combo
        lines = []
        host.line = lambda *args: lines.append(args)

        def paint():
            lines.clear()
            arrange(app, host)
            paint_tree(app, host, combo)
            return next(args[0] for args in lines if args[0] == args[2])

        combo.restore_view_state((0, len(combo.text), 0, 20.0))
        paint()
        self.assertEqual(combo._scroll_x, 20.0)
        combo.select(len(combo.text), len(combo.text))
        self.assertLess(paint(), combo.bounds.right - 30)
        combo.handle_input(Input("wheel", delta=-1, shift=True))
        manually_scrolled = combo._scroll_x
        paint()
        self.assertEqual(combo._scroll_x, manually_scrolled)
        for width in (120, 240):
            with self.subTest(width=width):
                combo.width = width
                caret_x = paint()
                self.assertGreater(caret_x, combo.bounds.x)
                self.assertLess(caret_x, combo.bounds.right - 30)

    def test_combo_text_and_caret_stay_outside_the_arrow_area(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        combo = app.combo_box(
            text="abcdefghijklmnopqrstuvwxyz" * 2,
            left=20,
            top=15,
            width=200,
            height=38,
        )
        runtime.router.focus = combo
        clips, texts, lines = [], [], []
        host.clip = clips.append
        host.text = lambda *args: texts.append((args, clips[-1]))
        host.line = lambda *args: lines.append((args, clips[-1]))
        for monospace in (False, True):
            with self.subTest(monospace=monospace):
                combo.monospace = monospace
                combo.select(len(combo.text), len(combo.text))
                arrange(app, host)
                texts.clear()
                lines.clear()
                paint_tree(app, host, combo)
                arrow_left = combo.bounds.right - 30
                caret, clip = next(
                    (args, clip)
                    for args, clip in lines
                    if args[0] == args[2] and args[1] != args[3]
                )
                self.assertGreater(caret[0], combo.bounds.x)
                self.assertLess(caret[0], arrow_left)
                self.assertLessEqual(clip.right, arrow_left)
                self.assertTrue(texts)
                self.assertTrue(all(clip.right <= arrow_left for _, clip in texts))

    def test_dropdown_distinguishes_empty_selection_and_elides_narrow_labels(self):
        app = App()
        try:
            choice = app.dropdown(items=("", "A long choice"), selected_index=0)
            host = RecordingHost()
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(host.texts, [""])
            choice.selected_index = -1
            paint_tree(app, host)
            self.assertEqual(host.texts, [choice.placeholder])
            choice.selected_index = 1
            choice.width = 42
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(host.texts, [""])
        finally:
            app.destroy()

    def test_group_exclusion_and_progress_validation(self):
        app = App()
        try:
            first = app.radio_button(text="First", checked=True)
            second = app.radio_button(text="Second", checked=True)
            independent = app.radio_button(group="other", checked=True)
            self.assertFalse(first.checked)
            self.assertTrue(second.checked and independent.checked)
            first.checked = True
            self.assertFalse(second.checked)
            with self.assertRaisesRegex(LifecycleError, "Activation requires"):
                first.activate()
            self.assertTrue(first.checked)
            progress = ProgressBar(value=80)
            with self.assertRaises(ValueError):
                progress.maximum = 50
            self.assertEqual(progress.maximum, 100)
            with self.assertRaises(TypeError):
                Dropdown(items=["mutable"])
            with self.assertRaises(ValueError):
                Dropdown(items=("a",), selected_index=1)
            toggle = Toggle()
            with self.assertRaisesRegex(LifecycleError, "Activation requires"):
                toggle.activate()
            self.assertFalse(toggle.checked)
            toggle.checked = True
            self.assertTrue(toggle.checked)
            progress.destroy()
            toggle.destroy()
        finally:
            app.destroy()


class ListNavigationTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App()
        self.host = RecordingHost()
        self.listing = self.app.list_view(
            items=tuple(f"Row {i}" for i in range(100)),
            width=180,
            height=100,
            row_height=20,
        )
        arrange(self.app, self.host)

    def tearDown(self):
        self.app.destroy()

    def key(self, key):
        self.listing.handle_input(Input("key_down", key=key))

    def test_page_home_end_and_public_revelation(self):
        self.key("Home")
        self.key("PageDown")
        self.assertEqual((self.listing.selected_index, self.listing._scroll), (5, 1))
        self.key("End")
        self.assertEqual((self.listing.selected_index, self.listing._scroll), (99, 95))
        self.key("PageUp")
        self.assertEqual((self.listing.selected_index, self.listing._scroll), (94, 94))
        self.listing.reveal(25)
        self.assertEqual(self.listing.selected_index, 94)
        self.assertEqual(self.listing._scroll, 25)
        self.listing.reveal_selection()
        self.assertEqual(self.listing._scroll, 90)
        self.listing.selected_index = -1
        self.listing.reveal_selection()
        self.assertEqual(self.listing._scroll, 90)
        with self.assertRaises(ValueError):
            self.listing.reveal(100)
        with self.assertRaises(TypeError):
            self.listing.reveal(True)

    def test_typeahead_cycles_prefixes_and_expires(self):
        self.listing.items = ("Alpha", "Alpine", "Beta", "Berry", "Bravo", "delta")
        with patch("pysual._controls.lists.monotonic", return_value=1) as clock:
            self.key("b")
            self.assertEqual(self.listing.selected_index, 2)
            self.key("b")
            self.assertEqual(self.listing.selected_index, 3)
            clock.return_value = 2
            self.key("b")
            self.key("r")
            self.assertEqual(self.listing.selected_index, 4)
            clock.return_value = 3
            self.key("D")
            self.assertEqual(self.listing.selected_index, 5)
            self.assertEqual(self.listing._scroll, 1)
        self.listing.items = ("Alpha",)
        self.assertEqual((self.listing.selected_index, self.listing._scroll), (-1, 0))

    def test_scrollbar_pages_drags_and_cancels_without_selection(self):
        listing = self.listing
        listing.selected_index = 3
        listing.handle_input(Input("pointer_down", x=175, y=80))
        self.assertEqual((listing._scroll, listing.selected_index), (5, 3))
        track, thumb = listing._scrollbar()
        listing.handle_input(Input("pointer_down", x=175, y=thumb.y + 2, pointer_id=4))
        listing.handle_input(Input("pointer_move", x=175, y=200, pointer_id=5))
        self.assertEqual(listing._scroll, 5)
        listing.handle_input(Input("pointer_move", x=175, y=200, pointer_id=4))
        self.assertEqual((listing._scroll, listing.selected_index), (95, 3))
        listing.handle_input(Input("blur"))
        listing.handle_input(Input("pointer_move", x=175, y=0, pointer_id=4))
        self.assertEqual(listing._scroll, 95)
        self.assertIsNone(listing._scroll_drag)
        listing.height = 2000
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        self.assertEqual(listing._scroll, 0)
        self.assertIsNone(listing._scrollbar())

    def test_scrollbar_clips_text_to_rows_and_keeps_paint_work_bounded(self):
        clips = []
        self.host.clip = clips.append
        paint_tree(self.app, self.host)
        self.assertEqual(self.listing._painted_rows, 5)
        self.assertTrue(any(clip and clip.width == 168 for clip in clips))


class PopupTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        changes = []

        class Demo(App):
            def build(self):
                self.padding = 12
                self.choice = self.dropdown(
                    items=tuple(f"Choice {i}" for i in range(30)),
                    selected_index=0,
                    visible_rows=4,
                    left=220,
                    top=230,
                    width=150,
                    height=38,
                )
                self.editable = self.combo_box(
                    items=("Alpha", "Beta", "Gamma"),
                    text="Alpha",
                    left=10,
                    top=60,
                    width=180,
                    height=38,
                )
                self.other = self.button(text="Other", left=10, top=10)

            def choice_on_changed(self, event):
                changes.append((event.old_value, event.new_value, event.origin))

        self.changes = changes
        self.app = Demo(width=400, height=300)
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        if not self.task.done():
            self.runtime._stop.set()
        await self.task

    async def inputs(self, *events):
        self.host.events.extend(events)
        await asyncio.sleep(0.035)
        if self.task.done():
            await self.task
        self.runtime.dispatcher.raise_errors()

    async def key(self, key):
        await self.inputs(Input("key_down", key=key))

    async def open(self):
        self.app.choice.focus()
        await self.key("Space")
        self.assertIsNotNone(self.runtime.popup)
        return self.runtime.popup

    async def test_popup_preserves_requested_size_through_resize_and_live_edits(self):
        popup = self.app.popup(width=300, height=200, layout="stack")
        popup.button(text="Content")
        popup.show(self.app.other)
        await eventually(lambda: popup.bounds.width == 300)
        self.app.width, self.app.height = 174, 150
        await eventually(lambda: popup.bounds.width == 150)
        self.assertEqual(popup.bounds.height, 126)
        self.assertEqual((popup.width, popup.height), (300, 200))
        self.app.width, self.app.height = 500, 400
        await eventually(lambda: popup.bounds.width == 300)
        self.assertEqual(popup.bounds.height, 200)
        popup.width, popup.height = 330, 220
        await eventually(lambda: popup.bounds.width == 330)
        self.assertEqual(popup.bounds.height, 220)
        self.assertGreaterEqual(popup.bounds.x, self.app.padding)
        self.assertLessEqual(popup.bounds.right, self.app.width - self.app.padding)

    async def test_hint_recovers_after_resize_and_remeasures_theme(self):
        owner = self.app.other
        owner.tooltip = "Hint text" * 3
        self.runtime.router.hover = owner
        self.runtime.hints.active = True
        with patch("pysual.hints.monotonic", return_value=100) as clock:
            self.runtime.hints.update()
            clock.return_value = 101
            self.runtime.hints.update()
            hint = self.runtime.hints.control
            self.assertIsNotNone(hint)
            arrange(self.app, self.host)
            desired = hint.width
            self.host.size = (174, 44)
            arrange(self.app, self.host)
            self.runtime.hints.update()
            arrange(self.app, self.host)
            self.assertEqual(hint.bounds.width, 150)
            self.assertEqual(hint.bounds.height, 20)
            self.assertEqual(hint.width, desired)
            self.host.size = (500, 400)
            arrange(self.app, self.host)
            self.runtime.hints.update()
            arrange(self.app, self.host)
            self.assertEqual(hint.bounds.width, desired)
            self.assertEqual(hint.bounds.height, 32)
            owner.theme_override = owner.effective_theme.derive(font_size=10)
            self.runtime.hints.update()
            arrange(self.app, self.host)
            self.assertLess(hint.bounds.width, desired)

    async def test_keyboard_cancel_commit_and_viewport_placement(self):
        popup = await self.open()
        self.assertGreaterEqual(popup.bounds.x, 12)
        self.assertLessEqual(popup.bounds.right, 388)
        self.assertLessEqual(popup.bounds.bottom, self.app.choice.bounds.y)
        await self.key("ArrowDown")
        self.assertEqual(self.app.choice.selected_index, 0)
        await self.key("Escape")
        self.assertIsNone(self.runtime.popup)
        self.assertTrue(self.app.choice.focused)
        await self.open()
        await self.key("End")
        await self.key("Enter")
        self.assertEqual(self.app.choice.selected_index, 29)
        self.assertEqual(self.changes, [(0, 29, "user")])
        self.assertIsNone(self.runtime.popup)

    async def test_pointer_commit_outside_dismiss_and_data_change(self):
        popup = await self.open()
        row = popup.children[0].bounds
        await self.inputs(
            Input("pointer_down", row.x + 15, row.y + 45),
            Input("pointer_up", row.x + 15, row.y + 45),
        )
        self.assertEqual(self.app.choice.selected_index, 1)
        self.assertIsNone(self.runtime.popup)
        clicked = []
        self.app.other.click.connect(lambda e: clicked.append(True))
        await self.open()
        r = self.app.other.bounds
        await self.inputs(
            Input("pointer_down", r.x + 10, r.y + 10),
            Input("pointer_up", r.x + 10, r.y + 10),
        )
        self.assertEqual(clicked, [])
        self.assertIsNone(self.runtime.popup)
        await self.open()
        await self.inputs(
            Input(
                "wheel",
                self.runtime.popup.bounds.x + 10,
                self.runtime.popup.bounds.y + 10,
                delta=1,
            )
        )
        self.assertEqual(self.runtime.popup.children[0]._scroll, 3)
        self.app.choice.items = ("New",)
        await asyncio.sleep(0.02)
        self.assertIsNone(self.runtime.popup)
        self.assertEqual(self.app.choice.selected_index, -1)

    async def test_popup_scrollbar_and_page_navigation_do_not_commit_twice(self):
        popup = await self.open()
        await self.key("PageDown")
        choices = popup.children[0]
        self.assertEqual(choices.selected_index, 4)
        rect = choices.bounds
        await self.inputs(
            Input("pointer_down", rect.right - 3, rect.bottom - 3),
            Input("pointer_up", rect.right - 3, rect.bottom - 3),
        )
        self.assertIs(self.runtime.popup, popup)
        self.assertEqual(self.app.choice.selected_index, 0)
        self.assertEqual(choices._scroll, 5)
        await self.key("Enter")
        self.assertEqual(self.app.choice.selected_index, 4)

    async def test_combo_freetext_selection_and_undo(self):
        self.app.editable.focus()
        await self.key("ArrowDown")
        await self.key("End")
        await self.key("Enter")
        self.assertEqual(self.app.editable.text, "Gamma")
        self.assertEqual(self.app.editable.selected_index, 2)
        self.app.editable.undo()
        self.assertEqual(self.app.editable.text, "Alpha")
        self.app.editable.select_all()
        await self.inputs(Input("text", text="Custom"))
        self.assertEqual(self.app.editable.text, "Custom")
        self.assertEqual(self.app.editable.selected_index, -1)
        self.app.editable.read_only = True
        await self.key("ArrowDown")
        self.assertIsNone(self.runtime.popup)

    async def test_owner_disposal_tab_and_window_blur_cleanup(self):
        await self.open()
        await self.key("Tab")
        self.assertIsNone(self.runtime.popup)
        self.assertFalse(self.app.choice.focused)
        popup = await self.open()
        await self.inputs(Input("blur"))
        self.assertFalse(popup.is_open)
        popup = await self.open()
        self.app.choice.destroy()
        await asyncio.sleep(0.035)
        self.runtime.dispatcher.raise_errors()
        self.assertIsNone(self.runtime.popup)
        self.assertFalse(popup.is_open)

    async def test_popup_inside_modal_and_factory_creation(self):
        dialog = self.app.sub_window(width=300, height=180)
        choice = dialog.dropdown(items=("One", "Two"), width=160, height=38)
        task = asyncio.create_task(dialog.show_modal_async())
        await asyncio.sleep(0.025)
        with self.assertRaises(LifecycleError):
            self.app.choice.open()
        choice.focus()
        await self.key("Space")
        self.assertIsNotNone(self.runtime.popup)
        await self.key("Escape")
        self.assertIs(self.runtime.modal, dialog)
        self.assertTrue(choice.focused)
        await dialog.close_async()
        await task
        popup = self.app.popup(width=150, height=90, layout="stack")
        popup.button(text="Content")
        popup.show(self.app.other)
        await asyncio.sleep(0.025)
        self.assertTrue(popup.is_open)
        popup.dismiss()
        self.assertFalse(popup.is_open)
