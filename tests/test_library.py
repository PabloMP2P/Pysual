"""Shared UI behavior, through the same runtime used by both real adapters."""

import asyncio
from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase

from pysual import App, Button, Label, Slider, Theme, Style, Rule, dark, light
from pysual.errors import BindingError, LifecycleError
from pysual.host import CapabilityError, Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.runtime import Runtime


class RecordingHost:
    """Deterministic platform I/O for tests; never a public runtime backend."""

    capabilities = frozenset({"host_resize", "clipboard"})

    def __init__(self):
        self.size = (640, 400)
        self.events = []
        self.frames = 0
        self.texts = []
        self.opened = self.closed = False
        self.clipboard = ""
        self.text_target = None

    def open(self, title, width, height, resizable, scale):
        self.opened = True
        self.size = (width, height)

    def close(self):
        self.closed = True

    def set_title(self, title):
        self.title = title

    def set_size(self, width, height):
        self.size = (width, height)

    def poll(self):
        events, self.events = self.events, []
        return events

    def begin(self, color):
        self.texts = []

    def present(self):
        self.frames += 1

    def clip(self, rect):
        pass

    def rect(self, *args, **kwargs):
        pass

    def line(self, *args, **kwargs):
        pass

    def text(self, text, *args, **kwargs):
        self.texts.append(text)

    def image(self, *args):
        pass

    def measure(self, text, size, mono=False):
        return len(text) * size * 0.6, size * 1.2

    def text_input(self, rect):
        self.text_target = rect

    async def clipboard_read(self):
        return self.clipboard

    async def clipboard_write(self, text):
        self.clipboard = text


async def eventually(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.005)


class RuntimeTests(AsyncUIOwnerTestCase):
    async def start(self, app, host=None):
        self.app = app
        self.host = host or RecordingHost()
        self.runtime = Runtime(app, self.host)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: app.is_open or self.task.done())
        if self.task.done():
            await self.task
        return self.runtime

    async def asyncTearDown(self):
        if hasattr(self, "task") and not self.task.done():
            self.runtime._stop.set()
            await asyncio.wait_for(self.task, 2)
        if hasattr(self, "app") and self.app.lifecycle_state != "CLOSED":
            self.app.close()

    async def click(self, control):
        r = control.bounds
        self.host.events += [
            Input("pointer_down", r.x + 10, r.y + 10),
            Input("pointer_up", r.x + 10, r.y + 10),
        ]
        await asyncio.sleep(0.03)

    async def test_blocking_run_rejects_ui_owner_before_opening(self):
        app = App()
        with self.assertRaisesRegex(LifecycleError, "cannot run on the UI owner"):
            app.run_blocking()
        self.assertEqual(app.lifecycle_state, "CREATED")
        self.assertIsNone(app._runtime)
        app.destroy()

    async def test_loaded_async_mutation_events_and_closed_after_host_release(self):
        trace = []
        host = RecordingHost()

        class Demo(App):
            def build(self):
                self.go = self.button(text="Go")

            async def go_on_click(self, event):
                trace.append("clicked")
                await asyncio.sleep(0.02)
                self.go.text = "Done"

            async def Demo_on_loaded(self, event):
                await asyncio.sleep(0.01)
                trace.append("loaded")

            def Demo_on_closed(self, event):
                trace.append(("closed", host.closed))

        app = Demo(width=640, height=400)
        await self.start(app, host)
        await self.click(app.go)
        await eventually(lambda: app.go.text == "Done")
        await eventually(lambda: "Done" in host.texts)
        self.assertIn("loaded", trace)
        app.close()
        await self.task
        self.assertEqual(trace[-1], ("closed", True))
        self.assertEqual(app.lifecycle_state, "CLOSED")

    async def test_close_veto_keeps_ui_responsive_and_coalesces_pending_request(self):
        decisions = []

        class Demo(App):
            async def Demo_on_closing(self, event):
                decisions.append(event)
                await asyncio.sleep(0.03)
                if len(decisions) == 1:
                    event.cancel()

        app = Demo()
        await self.start(app)
        one = asyncio.create_task(app.close_async())
        two = asyncio.create_task(app.close_async())
        self.assertIsNot(one, two)
        self.assertEqual(await asyncio.gather(one, two), [False, False])
        self.assertTrue(app.is_open)
        self.assertTrue(await app.close_async())
        await self.task
        self.assertEqual(len(decisions), 2)

    async def test_failure_propagates_after_host_cleanup(self):
        class Demo(App):
            async def Demo_on_loaded(self, event):
                await asyncio.sleep(0.01)
                raise ValueError("application failure")

        await self.start(Demo())
        with self.assertRaisesRegex(ValueError, "application failure"):
            await self.task
        self.assertTrue(self.host.closed)
        another = App()
        another.destroy()

    async def test_failed_host_open_releases_partial_resources(self):
        class Broken(RecordingHost):
            def open(self, *args):
                self.opened = True
                raise RuntimeError("open failure")

        app = App()
        host = Broken()
        with self.assertRaisesRegex(RuntimeError, "open failure"):
            await Runtime(app, host).main()
        self.assertTrue(host.closed)
        self.assertEqual(app.lifecycle_state, "CLOSED")
        App().destroy()

    async def test_checkbox_slider_and_origin_from_platform_input(self):
        changes = []

        class Demo(App):
            def build(self):
                self.layout = "stack"
                self.check = self.check_box(text="Enable")
                self.level = self.slider()

            def check_on_changed(self, event):
                changes.append((event.new_value, event.origin))

            def level_on_changed(self, event):
                changes.append((event.new_value, event.origin))

        app = Demo()
        await self.start(app)
        await self.click(app.check)
        self.assertTrue(app.check.checked)
        self.runtime.router.set_focus(app.level)
        self.host.events.append(Input("key_down", key="End"))
        await eventually(lambda: app.level.value == 100)
        await eventually(lambda: len(changes) == 2)
        self.assertEqual(changes, [(True, "user"), (100, "user")])
        app.level.value = 50
        await eventually(lambda: len(changes) == 3)
        self.assertEqual(changes[-1], (50, "program"))

    async def test_text_editing_undo_unicode_selection_clipboard_and_read_only(self):
        class Demo(App):
            def build(self):
                self.entry = self.text_box(text="Cafe\u0301")

        app = Demo()
        await self.start(app)
        router = self.runtime.router
        entry = app.entry
        router.set_focus(entry)
        router.process(Input("key_down", key="Backspace"))
        self.assertEqual(entry.text, "Caf")
        router.process(Input("text", text="é"))
        router.process(Input("key_down", key="a", ctrl=True))
        await router.clipboard("c")
        self.assertEqual(self.host.clipboard, "Café")
        self.host.clipboard = "new\nvalue"
        await router.clipboard("v")
        self.assertEqual(entry.text, "new value")
        entry.undo()
        self.assertEqual(entry.text, "Café")
        entry.redo()
        self.assertEqual(entry.text, "new value")
        entry.password = True
        entry.select_all()
        await router.clipboard("x")
        self.assertEqual(entry.text, "new value")
        self.assertEqual(entry.selection_text, "")
        entry.read_only = True
        router.process(Input("key_down", key="Backspace"))
        router.process(Input("text", text="ignored"))
        self.assertEqual(entry.text, "new value")

    async def test_selection_only_replacement_undo_and_redo_request_frames(self):
        app = App()
        entry = app.text_box(text="same")
        await self.start(app)
        entry.focus()
        entry.select_all()
        await asyncio.sleep(0.035)
        changes = []
        entry.changed.connect(lambda e: changes.append(e.new_value))
        for operation, selection in (
            (lambda: entry.replace_selection("same"), (4, 4)),
            (entry.undo, (0, 4)),
            (entry.redo, (4, 4)),
        ):
            frames = self.host.frames
            operation()
            self.assertEqual(entry.selection_range, selection)
            await eventually(lambda frames=frames: self.host.frames > frames)
        self.assertEqual(changes, [])

    async def test_keyboard_clipboard_edits_have_user_origin_only_during_commit(self):
        app = App()
        entry = app.text_box(text="original")
        rt = await self.start(app)
        entry.focus()
        entry.select_all()
        changes = []
        entry.changed.connect(lambda event: changes.append((event.new_value, event.origin)))
        self.host.clipboard = "pasted"
        rt.router.process(Input("key_down", key="v", ctrl=True))
        await eventually(lambda: entry.text == "pasted")
        self.assertEqual(entry._origin, "program")
        entry.select_all()
        rt.router.process(Input("key_down", key="x", ctrl=True))
        await eventually(lambda: entry.text == "")
        self.assertEqual(self.host.clipboard, "pasted")
        self.assertEqual(entry._origin, "program")
        entry.replace_selection("direct")
        entry.text = "assigned"
        await rt.dispatcher.drain()
        self.assertEqual(changes, [
            ("pasted", "user"), ("", "user"),
            ("direct", "program"), ("assigned", "program"),
        ])

    async def test_pointer_capture_selects_text_and_clears_on_blur(self):
        class Demo(App):
            def build(self):
                self.entry = self.text_box(text="abcdefghij", width=200)

        app = Demo()
        rt = await self.start(app)
        rt.router.process(Input("pointer_down", 11, 10))
        rt.router.process(Input("pointer_move", 60, 10))
        # At x=60 the nearest insertion boundary is after f, not before it.
        self.assertEqual(app.entry.selection_text, "abcdef")
        rt.router.process(Input("blur"))
        self.assertIsNone(rt.router.capture)
        self.assertFalse(app.entry._pressed)
        self.assertIsNone(self.host.text_target)

    async def test_pending_clipboard_does_not_edit_a_changed_selection_or_target(self):
        class Demo(App):
            def build(self):
                self.entry = self.text_box(text="first second")
                self.other = self.text_box(text="other", top=50)

        host = RecordingHost()
        started, release = asyncio.Event(), asyncio.Event()

        async def read():
            started.set()
            await release.wait()
            return "pasted"

        async def write(text):
            host.clipboard = text
            started.set()
            await release.wait()

        host.clipboard_read, host.clipboard_write = read, write
        app = Demo()
        rt = await self.start(app, host)
        for key, change in (
            ("x", "selection"),
            ("v", "selection"),
            ("x", "focus"),
            ("v", "text"),
        ):
            with self.subTest(key=key, change=change):
                started.clear()
                release.clear()
                app.entry.load_text("first second")
                rt.router.set_focus(app.entry)
                app.entry.select(0, 5)
                pending = asyncio.create_task(rt.router.clipboard(key))
                await started.wait()
                self.assertEqual(app.entry._origin, "program")
                if change == "selection":
                    app.entry.select(6, 12)
                elif change == "focus":
                    rt.router.set_focus(app.other)
                else:
                    app.entry.text = "updated text"
                before = app.entry.text
                release.set()
                await pending
                self.assertEqual(app.entry.text, before)
                self.assertEqual(app.entry._origin, "program")
        rt.router.set_focus(app.entry)
        app.entry.load_text("first second")
        app.entry.select(0, 5)
        await rt.router.clipboard("x")
        self.assertEqual(app.entry.text, " second")
        self.assertEqual(host.clipboard, "first")

    async def test_clipboard_service_paste_is_separate_from_adjacent_typing(self):
        app = App()
        entry = app.text_box()
        rt = await self.start(app)
        rt.router.set_focus(entry)
        rt.router.process(Input("text", text="ab"))
        self.host.clipboard = "PASTE"
        await rt.router.clipboard("v")
        self.assertEqual(entry.text, "abPASTE")
        entry.undo()
        self.assertEqual(entry.text, "ab")
        entry.redo()
        rt.router.process(Input("text", text="cd"))
        entry.undo()
        self.assertEqual(entry.text, "abPASTE")

    async def test_batched_cut_then_tab_does_not_edit_the_later_focused_field(self):
        app = App()
        first = app.text_box(text="FIRST", width=180, height=38)
        second = app.text_box(text="SECOND", top=50, width=180, height=38)
        rt = await self.start(app)
        first.select_all()
        second.select_all()
        first.focus()
        self.host.clipboard = "unchanged"
        self.host.events.extend(
            [Input("key_down", key="x", ctrl=True), Input("key_down", key="Tab")]
        )
        await eventually(lambda: not self.host.events)
        await rt.dispatcher.drain()
        self.assertIs(rt.router.focus, second)
        self.assertEqual((first.text, second.text), ("FIRST", "SECOND"))
        self.assertEqual(self.host.clipboard, "unchanged")

    async def test_batched_paste_then_click_does_not_edit_the_later_focused_field(self):
        app = App()
        first = app.text_box(text="FIRST", width=180, height=38)
        second = app.text_box(text="SECOND", top=50, width=180, height=38)
        rt = await self.start(app)
        first.select_all()
        first.focus()
        self.host.clipboard = "PASTED"
        self.host.events.extend(
            [
                Input("key_down", key="v", ctrl=True),
                Input("pointer_down", 10, 60),
                Input("pointer_up", 10, 60),
            ]
        )
        await eventually(lambda: not self.host.events)
        await rt.dispatcher.drain()
        self.assertIs(rt.router.focus, second)
        self.assertEqual((first.text, second.text), ("FIRST", "SECOND"))

    async def test_clipboard_ignores_selection_changes_before_its_task_starts(self):
        app = App()
        entry = app.text_box(text="first second")
        rt = await self.start(app)
        entry.focus()
        for key in ("c", "x", "v"):
            with self.subTest(key=key):
                entry.load_text("first second")
                entry.select(0, 5)
                self.host.clipboard = "unchanged"
                rt.router.process(Input("key_down", key=key, ctrl=True))
                entry.select(6, 12)
                await rt.dispatcher.drain()
                self.assertEqual(entry.text, "first second")
                self.assertEqual(entry.selection_range, (6, 12))
                self.assertEqual(self.host.clipboard, "unchanged")

    async def test_focus_loss_clears_composition_hover_and_emits_disabled_focus_change(
        self,
    ):
        class Demo(App):
            def build(self):
                self.entry = self.text_box(text="saved")
                self.other = self.button(text="Other", top=50)

        app = Demo()
        rt = await self.start(app)
        rt.router.process(Input("pointer_move", 12, 12))
        rt.router.set_focus(app.entry)
        rt.router.process(Input("composition", text="pending"))
        self.assertTrue(app.entry._hover)
        rt.router.process(Input("blur"))
        self.assertEqual(app.entry._composition, "")
        self.assertEqual(app.entry.text, "saved")
        self.assertFalse(app.entry._hover)
        self.assertIsNone(rt.router.hover)
        rt.router.set_focus(app.entry)
        rt.router.process(Input("composition", text="another"))
        observed = []
        app.entry.focused_changed.connect(
            lambda event: observed.append(event.source.focused)
        )
        app.entry.enabled = False
        rt.router.reconcile()
        await asyncio.sleep(0.025)
        self.assertEqual(app.entry._composition, "")
        self.assertEqual(observed, [False])
        self.assertIsNone(self.host.text_target)

    async def test_modal_focus_trap_close_veto_and_restore(self):
        class Demo(App):
            def build(self):
                self.launch = self.button(text="Dialog")

        app = Demo()
        rt = await self.start(app)
        rt.router.set_focus(app.launch)
        dialog = app.sub_window(left=120, top=70)
        inside = dialog.button(text="OK")
        modal = app.create_task(dialog.show_modal_async())
        await eventually(lambda: rt.modal is dialog)
        await eventually(lambda: inside.bounds.width > 0)
        self.assertIs(rt.router.focus, inside)
        await self.click(app.launch)
        rt.router.process(Input("key_down", key="Tab"))
        self.assertIs(rt.router.focus, inside)

        def veto(event):
            event.cancel()

        subscription = dialog.closing.connect(veto)
        self.assertFalse(await dialog.close_async("no"))
        self.assertFalse(modal.done())
        subscription.disconnect()
        self.assertTrue(await dialog.close_async("accepted"))
        self.assertEqual(await modal, "accepted")
        self.assertIs(rt.router.focus, app.launch)
        self.assertIn(dialog, app.children)
        self.assertFalse(dialog.visible)
        self.assertFalse(dialog._disposed)

    async def test_modal_destroy_restores_input_and_clears_old_capture(self):
        app = App()
        underlying = app.button()
        rt = await self.start(app)
        rt.router.process(Input("pointer_down", 10, 10))
        self.assertIs(rt.router.capture, underlying)
        dialog = app.sub_window()
        task = app.create_task(dialog.show_modal_async())
        await eventually(lambda: rt.modal is dialog)
        self.assertIsNone(rt.router.capture)
        dialog.destroy()
        self.assertIsNone(await task)
        self.assertIsNone(rt.modal)
        self.assertIs(rt.router.focus, underlying)

    async def test_source_disposal_cancels_task_and_redraws(self):
        app = App()
        label = app.label(text="temporary")
        await self.start(app)
        cleaned = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(100)
            finally:
                cleaned.set()

        label.create_task(work())
        await asyncio.sleep(0)
        label.destroy()
        await eventually(cleaned.is_set)
        await eventually(lambda: "temporary" not in self.host.texts)

    async def test_native_size_mutation_and_startup_only_options(self):
        app = App(width=400, height=300)
        await self.start(app)
        app.width = 500
        await eventually(lambda: self.host.size == (500, 300))
        with self.assertRaises(LifecycleError):
            app.ui_scale = 2
        self.host.capabilities = frozenset()
        with self.assertRaises(CapabilityError):
            app.width = 600
        self.assertEqual(app.width, 500)

    async def test_idle_has_no_repaint_but_timer_changes_paint(self):
        app = App()
        label = app.label(text="before")
        await self.start(app)
        await asyncio.sleep(0.03)
        frames = self.host.frames
        await asyncio.sleep(0.04)
        self.assertEqual(self.host.frames, frames)
        label.text = "after"
        await eventually(lambda: self.host.frames > frames)
        self.assertIn("after", self.host.texts)


class LayoutAndThemeTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App()
        self.host = RecordingHost()

    def tearDown(self):
        self.app.close()

    def test_stack_flex_nested_add_and_theme_remeasurement(self):
        self.app.layout = "stack"
        panel = self.app.panel(layout="stack", flex=1)
        title = panel.label(text="abc")
        arrange(self.app, self.host)
        self.assertEqual(panel.bounds.width, 640)
        old = title.bounds.height
        self.app.theme = self.app.theme.derive(font_size=24)
        arrange(self.app, self.host)
        self.assertGreater(title.bounds.height, old)
        self.app.theme = self.app.theme.styled(Label, Style(font_size=30))
        arrange(self.app, self.host)
        self.assertEqual(title.bounds.height, 36)
        nested = panel.panel(height=90, layout="stack")
        new = nested.button(text="Added later")
        arrange(self.app, self.host)
        self.assertGreater(new.bounds.width, 0)
        new.destroy()
        self.assertEqual(nested.children, ())

    def test_grid_auto_reserves_later_explicit_slots_and_spans(self):
        self.app.layout = "grid"
        self.app.columns = 3
        self.app.spacing = 0
        auto = self.app.label(text="Auto")
        explicit = self.app.label(
            text="Pinned", grid_row=0, grid_col=0, grid_col_span=2
        )
        arrange(self.app, self.host)
        self.assertEqual(explicit.bounds.x, 0)
        self.assertAlmostEqual(explicit.bounds.width, 640 * 2 / 3)
        self.assertAlmostEqual(auto.bounds.x, 640 * 2 / 3)

    def test_anchors_resize_and_clipped_hit_testing(self):
        panel = self.app.panel(width=200, height=100)
        child = panel.button(left=150, top=30, width=100, anchor="right,top")
        arrange(self.app, self.host)
        self.assertEqual(child._clip.width, 50)
        panel.width = 300
        arrange(self.app, self.host)
        self.assertEqual(child.bounds.x, 250)
        rt = Runtime(self.app, self.host)
        self.assertIs(rt.router.hit(self.app, 275, 40), child)
        self.assertIsNot(rt.router.hit(self.app, 325, 40), child)

    def test_right_and_bottom_anchors_preserve_edges_when_content_grows(self):
        panel = self.app.panel(width=400, height=300, padding=10)
        button = panel.button(left=280, top=10, text="Save", anchor="right,top")
        label = panel.label(left=10, top=230, text="Bottom", font_size=15,
                            anchor="left,bottom")
        arrange(self.app, self.host)
        right, bottom = button.bounds.right, label.bounds.bottom
        button.text = "Save document now"
        label.font_size = 30
        arrange(self.app, self.host)
        self.assertEqual(button.bounds.right, right)
        self.assertEqual(label.bounds.bottom, bottom)
        panel.update(width=450, height=350)
        arrange(self.app, self.host)
        self.assertEqual(button.bounds.right, right + 50)
        self.assertEqual(label.bounds.bottom, bottom + 50)
        # An explicit position intentionally establishes a new anchor baseline.
        button.left = 20
        arrange(self.app, self.host)
        self.assertEqual(button.bounds.x, panel.bounds.x + panel.padding + 20)

    def test_virtual_list_work_is_bounded_by_viewport(self):
        listing = self.app.list_view(
            items=tuple(f"Row {i}" for i in range(100_000)), width=250, height=200
        )
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        self.assertLessEqual(listing._painted_rows, 8)
        listing.handle_input(Input("key_down", key="End"))
        paint_tree(self.app, self.host)
        self.assertEqual(listing.selected_index, 99_999)
        self.assertIn("Row 99999", self.host.texts)
        listing.items = ("One",)
        self.assertEqual(listing.selected_index, -1)
        paint_tree(self.app, self.host)
        self.assertIn("One", self.host.texts)

    def test_nested_list_and_text_paint_only_rows_intersecting_outer_clip(self):
        area = self.app.scroll_area(width=250, height=90)
        listing = area.list_view(
            items=tuple(f"Row {i}" for i in range(1000)),
            width=240,
            height=30_000,
            row_height=30,
        )
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        self.assertEqual(self.host.texts, ["Row 0", "Row 1", "Row 2"])
        area.scroll_y = 1515
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        self.assertEqual(self.host.texts, [f"Row {i}" for i in range(50, 54)])
        self.assertEqual(listing._painted_rows, 4)
        listing.destroy()
        area.text_box(
            text="\n".join(f"Line {i}" for i in range(1000)),
            multiline=True,
            font_size=10,
            width=240,
            height=14_016,
        )
        area.scroll_y = 0
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        self.assertEqual(self.host.texts, [f"Line {i}" for i in range(5)])
        area.scroll_y = 708
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        self.assertEqual(self.host.texts, [f"Line {i}" for i in range(50, 56)])

    def test_row_scrolling_keeps_fractional_wheel_and_clamps_after_resize(self):
        listing = self.app.list_view(
            items=tuple(str(i) for i in range(100)), width=240, height=90, row_height=30
        )
        entry = self.app.text_box(
            text="\n".join(str(i) for i in range(100)),
            multiline=True,
            font_size=10,
            width=240,
            height=58,
        )
        arrange(self.app, self.host)
        for control in (listing, entry):
            with self.subTest(control=type(control).__name__):
                for _ in range(100):
                    control.handle_input(Input("wheel", delta=0.01))
                self.assertEqual(control._scroll, 3)
                control.handle_input(Input("wheel", delta=1))
                self.assertEqual(control._scroll, 6)
                for _ in range(100):
                    control.handle_input(Input("wheel", delta=-0.01))
                self.assertEqual(control._scroll, 3)
                control.handle_input(Input("wheel", delta=-100))
                self.assertEqual(control._scroll, 0)
        listing.handle_input(Input("key_down", key="End"))
        entry.select(len(entry.text), len(entry.text))
        self.assertEqual((listing._scroll, entry._scroll), (97, 97))
        listing.height, entry.height = 300, 156
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        self.assertEqual((listing._scroll, entry._scroll), (90, 90))
        listing.row_height = 15
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        self.assertEqual(listing._scroll, 80)

    def test_scroll_area_reconciles_shrink_hide_destroy_and_viewport_growth(self):
        area = self.app.scroll_area(width=120, height=100, padding=10)
        child = area.label(text="Content", width=1000, height=1000)
        overlay = area.sub_window(left=3000, top=3000, width=100, height=100)
        arrange(self.app, self.host)
        area.scroll_x, area.scroll_y = 900, 900
        arrange(self.app, self.host)
        self.assertEqual(overlay.bounds.x, 3010)
        child.width, child.height = 30, 30
        arrange(self.app, self.host)
        self.assertEqual((area.scroll_x, area.scroll_y), (0, 0))
        self.assertEqual(child.bounds.x, 10)
        child.width, child.height = 1000, 1000
        arrange(self.app, self.host)
        area.scroll_y = 900
        child.visible = False
        arrange(self.app, self.host)
        self.assertEqual(area.scroll_y, 0)
        overlay.destroy()
        child.visible = True
        arrange(self.app, self.host)
        area.scroll_y = 900
        child.destroy()
        arrange(self.app, self.host)
        self.assertEqual(area.scroll_y, 0)
        child = area.label(width=100, height=200)
        arrange(self.app, self.host)
        area.scroll_y = 100
        area.height = 300
        arrange(self.app, self.host)
        self.assertEqual(area.scroll_y, 0)
        self.assertEqual(child.bounds.y, 10)

    def test_scroll_area_uses_declared_stack_and_grid_geometry(self):
        for layout in ("stack", "grid"):
            with self.subTest(layout=layout):
                area = self.app.scroll_area(
                    width=120,
                    height=100,
                    padding=10,
                    layout=layout,
                    columns=1,
                    spacing=0,
                )
                first = area.label(width=100, height=100)
                last = area.label(width=100, height=100)
                arrange(self.app, self.host)
                area.scroll_y = 1000
                arrange(self.app, self.host)
                self.assertEqual(last.bounds.bottom, area.content_bounds(area.bounds).bottom)
                first.visible = False
                last.height = 30
                arrange(self.app, self.host)
                self.assertEqual(area.scroll_y, 0)
                area.destroy()

    def test_checkbox_and_radio_natural_size_contains_the_painted_label(self):
        for factory in (self.app.check_box, self.app.radio_button):
            control = factory(text="A long natural label" * 3, font_size=24)
            width, height = control.measure(self.host)
            text_width, _ = self.host.measure(control.text, 24)
            self.assertGreaterEqual(width, 30 + text_width)
            self.assertGreaterEqual(height, 20)

    def test_slider_pointer_matches_painted_thumb_center_and_endpoints(self):
        class Surfaces:
            height = 38

            def __init__(self):
                self.parts = {}
                self.styles = {
                    "track": Style(radius=2), "fill": Style(radius=3),
                    "thumb": Style(radius=9),
                }

            def style(self, part):
                return self.styles[part]

            def surface(self, rect, style):
                self.parts[next(part for part, value in self.styles.items() if value is style)] = rect

            def marker(self, rect, style, *, shape, checked=False):
                self.surface(rect, style)

        slider = self.app.slider(width=180, height=38, value=50)
        for width in (180, 300):
            slider.width = width
            arrange(self.app, self.host)
            for value in (0, 25, 50, 75, 100):
                slider.value = value
                surfaces = Surfaces()
                slider.paint(surfaces)
                thumb = surfaces.parts["thumb"]
                center = slider.bounds.x + thumb.x + thumb.width / 2
                slider.handle_input(Input("pointer_down", x=center))
                self.assertEqual(slider.value, value)
                slider.handle_input(Input("pointer_up", x=center))

    def test_theme_roundtrip_precedence_and_validation(self):
        theme = (
            light()
            .styled(Button, Style(fill="#123456"))
            .styled(Button, Style(fill="#abcdef"), state="hover")
        )
        self.assertEqual(Theme.from_json(theme.to_json()), theme)
        self.assertEqual(theme.resolve("Button", state="hover").fill, "#abcdef")
        self.assertEqual(theme.resolve("Button", state="normal").fill, "#123456")
        with self.assertRaises(ValueError):
            dark().derive(accent="wrong")
        with self.assertRaises(TypeError):
            Style(wdith=10)
        with self.assertRaises(ValueError):
            Theme(rules=(Rule("Button"), Rule("Button")))
        with self.assertRaises(ValueError):
            Theme.from_json('{"schema":"untrusted"}')
        with self.assertRaises(ValueError):
            Theme.from_json('{"schema":"pysual-theme/1","tokens":{},"rules":[]}')
        with self.assertRaises(ValueError):
            Theme.from_json("[]")

    def test_invalid_layout_and_color_fail_at_mutation(self):
        control = self.app.button()
        with self.assertRaises(ValueError):
            control.anchor = "middle"
        with self.assertRaises(ValueError):
            control.background = "garbage"
        with self.assertRaises(ValueError):
            control.max_width = -1
        control.min_width = 20
        with self.assertRaises(ValueError):
            control.max_width = 10
        self.assertIsNone(control.max_width)
        with self.assertRaises(ValueError):
            Slider(value=300)
        with self.assertRaises(BindingError):
            self.app.run = 3
