"""Regressions for ordinary sizing, title dragging, and host-service cancellation."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from pysual import App, Button, Container, Hyperlink, ListView, ScrollArea, SplitPane, TextBox
from pysual.backends._web_native import LiveSVGHost
from pysual.geometry import Rect
from pysual.host import Input
from pysual.layout import arrange
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


class LayoutInteractionRegressions(UIOwnerTestCase):
    def setUp(self):
        self.app, self.host = App(width=600, height=600), RecordingHost()
        self.host.size = (600, 600)
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime

    def tearDown(self):
        self.app._runtime = None
        self.app.destroy()

    def layout(self):
        arrange(self.app, self.host)

    def test_size_changes_preserve_right_and_bottom_anchor_edges(self):
        parent = Container(parent=self.app, width=100, height=60)
        child = Button(parent=parent, left=40, top=5, width=30, height=20,
                       anchor="right,bottom")
        self.layout()
        parent.update(width=200, height=130)
        self.layout()
        self.assertEqual(child.bounds, Rect(140, 75, 30, 20))
        for width, height in ((20, 16), (60, 40), (None, None)):
            child.update(width=width, height=height)
            self.layout()
            self.assertEqual((child.bounds.right, child.bounds.bottom), (170, 95))
        child.update(left=10, top=8, width=20, height=16)
        self.layout()
        self.assertEqual(child.bounds, Rect(10, 8, 20, 16))
        parent.update(width=220, height=150)
        self.layout()
        self.assertEqual(child.bounds, Rect(30, 28, 20, 16))
        child.anchor = "left,top"
        self.layout()
        self.assertEqual(child.bounds, Rect(10, 8, 20, 16))

    def test_stretched_axis_accepts_new_size_without_resetting_other_axis(self):
        parent = Container(parent=self.app, width=100, height=80)
        child = Container(parent=parent, left=10, top=5, width=40, height=20,
                          anchor="left,right,top,bottom")
        self.layout()
        parent.update(width=200, height=140)
        self.layout()
        self.assertEqual(child.bounds, Rect(10, 5, 140, 80))
        child.width = 70
        self.layout()
        self.assertEqual(child.bounds, Rect(10, 5, 70, 80))
        parent.update(width=230, height=150)
        self.layout()
        self.assertEqual(child.bounds, Rect(10, 5, 100, 90))
        child.height = 60
        self.layout()
        self.assertEqual(child.bounds, Rect(10, 5, 100, 60))

    def test_split_caps_either_pane_on_either_axis_and_keeps_requested_position(self):
        for orientation, maximum, dimension in (
            ("horizontal", "max_width", "width"),
            ("vertical", "max_height", "height"),
        ):
            with self.subTest(orientation=orientation):
                pane = SplitPane(parent=self.app, width=500, height=500,
                                 orientation=orientation, position=.8)
                first, second = pane.first, pane.second
                setattr(first, maximum, 150)
                self.layout()
                self.assertAlmostEqual(getattr(first.bounds, dimension), 150)
                self.assertEqual(pane.position, .8)
                setattr(first, maximum, None)
                setattr(second, maximum, 150)
                pane.position = .2
                self.layout()
                self.assertAlmostEqual(getattr(second.bounds, dimension), 150)
                self.assertEqual(pane.position, .2)
                setattr(second, maximum, 120)
                self.layout()
                self.assertAlmostEqual(getattr(second.bounds, dimension), 120)
                pane.destroy()

    def test_split_keyboard_and_drag_use_feasible_caps(self):
        for orientation, maximum, dimension in (
            ("horizontal", "max_width", "width"),
            ("vertical", "max_height", "height"),
        ):
            with self.subTest(orientation=orientation):
                pane = SplitPane(parent=self.app, width=500, height=500,
                                 orientation=orientation)
                setattr(pane.first, maximum, 300)
                setattr(pane.second, maximum, 300)
                self.layout()
                for key, expected in (("Home", 194), ("End", 300)):
                    pane.handle_input(Input("key_down", key=key))
                    self.layout()
                    self.assertAlmostEqual(getattr(pane.first.bounds, dimension), expected)
                divider = pane._divider
                pane.handle_input(Input("pointer_down", divider.x + 1, divider.y + 1))
                pane.handle_input(Input("pointer_move", 1000, 1000))
                self.layout()
                self.assertAlmostEqual(getattr(pane.first.bounds, dimension), 300)
                pane.handle_input(Input("pointer_move", -100, -100))
                pane.handle_input(Input("pointer_up", -100, -100))
                self.layout()
                self.assertAlmostEqual(getattr(pane.first.bounds, dimension), 194)
                pane.destroy()

    def test_split_incompatible_caps_keep_existing_minimum_fallback(self):
        pane = SplitPane(parent=self.app, width=500, height=100, position=.8)
        pane.first.update(min_width=80, max_width=100)
        pane.second.update(min_width=80, max_width=100)
        self.layout()
        self.assertAlmostEqual(pane.first.bounds.width, 395.2)
        pane.first.update(min_width=300, max_width=350)
        pane.second.update(min_width=300, max_width=350)
        self.layout()
        self.assertAlmostEqual(pane.first.bounds.width, 247)
        self.assertAlmostEqual(pane.second.bounds.width, 247)

    def test_title_drag_keeps_descendant_editor_focus_selection_and_typing(self):
        dialog = self.app.sub_window(left=30, top=30, width=300, height=240)
        container = Container(parent=dialog, width=250, height=140)
        entry = TextBox(parent=container, width=200, height=40, text="hello")
        self.layout()
        self.runtime.router.set_focus(entry)
        entry.select(1, 4)
        x, y = dialog.bounds.x + 40, dialog.bounds.y + 10
        self.runtime.router.process(Input("pointer_down", x, y))
        self.runtime.router.process(Input("pointer_move", x + 25, y + 20))
        self.runtime.router.process(Input("pointer_up", x + 25, y + 20))
        self.layout()
        self.assertEqual((dialog.left, dialog.top), (55, 50))
        self.assertIs(self.runtime.router.focus, entry)
        self.assertEqual(entry.selection_range, (1, 4))
        self.runtime.router.process(Input("text", text="i"))
        self.assertEqual(entry.text, "hio")

    def test_anchored_title_drag_starts_at_arranged_position_after_host_resize(self):
        for padding in (0, 12):
            with self.subTest(padding=padding):
                self.app.update(width=700, height=500, padding=padding)
                self.runtime._submit_size()
                self.layout()
                dialog = self.app.sub_window(left=450, top=300, width=200, height=150,
                                             anchor="right,bottom")
                try:
                    self.layout()
                    self.host.size = (400, 300)
                    self.runtime._sync_viewport()
                    self.layout()
                    before = dialog.bounds
                    self.assertEqual((before.x, before.y), (150 + padding, 100 + padding))
                    x, y = before.x + 40, before.y + 10
                    self.runtime.router.process(Input("pointer_down", x, y, button=1))
                    for dx, dy in ((10, 10), (20, 15)):
                        self.runtime.router.process(Input("pointer_move", x + dx, y + dy))
                        self.layout()
                        self.assertEqual(dialog.bounds, Rect(before.x + dx, before.y + dy,
                                                             before.width, before.height))
                    self.runtime.router.process(Input("pointer_up", x + 20, y + 15, button=1))
                finally:
                    dialog.destroy()

    def test_other_chrome_and_blank_clicks_still_clear_focus(self):
        dialog = self.app.sub_window(left=30, top=30, width=300, height=240)
        entry = TextBox(parent=dialog, width=200, height=40, text="hello")
        outside = TextBox(parent=self.app, left=400, top=350, width=160, height=40)
        self.layout()
        close = dialog._close_bounds()
        points = (
            (dialog.bounds.x + close.x + 2, dialog.bounds.y + 2),
            (dialog.bounds.right - 2, dialog.bounds.bottom - 2),
            (dialog.bounds.x + 20, dialog.bounds.bottom - 40),
            (580, 580),
        )
        for x, y in points:
            with self.subTest(point=(x, y)):
                self.runtime.router.set_focus(entry)
                self.runtime.router.process(Input("pointer_down", x, y))
                self.assertIsNone(self.runtime.router.focus)
                self.runtime.router.process(Input("pointer_cancel", x, y))
        self.runtime.router.set_focus(outside)
        self.runtime.router.process(Input("pointer_down", dialog.bounds.x + 40,
                                          dialog.bounds.y + 10))
        self.assertIsNone(self.runtime.router.focus)
        self.runtime.router.process(Input("pointer_cancel"))
        dialog.draggable = False
        self.runtime.router.set_focus(entry)
        self.runtime.router.process(Input("pointer_down", dialog.bounds.x + 40,
                                          dialog.bounds.y + 10))
        self.assertIsNone(self.runtime.router.focus)

    def test_list_index_at_rejects_clipped_rows_and_keeps_visible_scrolled_rows(self):
        area = ScrollArea(parent=self.app, width=200, height=100)
        listing = ListView(parent=area, width=180, height=340,
                           items=tuple(str(i) for i in range(20)))
        self.layout()
        self.assertEqual(listing.index_at(10, 150), -1)
        self.assertIsNone(listing.insertion_index(10, 150))
        self.assertEqual(listing.index_at(10, 50), 1)
        listing.reveal(12)
        self.assertEqual(listing.index_at(10, 50), listing._scroll + 1)
        scrollbar = listing._scrollbar()[0]
        self.assertEqual(listing.index_at(scrollbar.x + 1, 50), -1)


class AsyncInteractionRegressions(AsyncUIOwnerTestCase):
    async def test_modal_title_drag_preserves_eligible_editor_focus(self):
        app, host = App(reduce_motion=True), RecordingHost()
        dialog = app.sub_window(left=20, top=20, visible=False)
        entry = TextBox(parent=dialog, width=180, height=40, text="modal")
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            dialog.show(modal=True)
            arrange(app, host)
            entry.focus()
            entry.select(0, 5)
            x, y = dialog.bounds.x + 40, dialog.bounds.y + 10
            runtime.router.process(Input("pointer_down", x, y))
            runtime.router.process(Input("pointer_move", x + 10, y + 10))
            runtime.router.process(Input("pointer_up", x + 10, y + 10))
            self.assertIs(runtime.modal, dialog)
            self.assertIs(runtime.router.focus, entry)
            self.assertEqual(entry.selection_range, (0, 5))
            self.assertTrue(await dialog.close_async())
        finally:
            runtime._stop.set()
            try:
                await asyncio.wait_for(task, 2)
            finally:
                app.destroy()

    async def test_cancelled_browser_service_keeps_app_open_and_next_link_works(self):
        class LinkHost(RecordingHost):
            capabilities = RecordingHost.capabilities | {"open_url"}
            open_url = LiveSVGHost.open_url

            def __init__(self):
                super().__init__()
                self.calls = []
                self.result = None  # The browser's Cancel button returns null.

            async def _rpc(self, method, **arguments):
                self.calls.append((method, arguments))
                return self.result

        app, host = App(reduce_motion=True), LinkHost()
        link = Hyperlink(parent=app, url="https://example.com")
        clicks = []
        link.click.connect(clicks.append)
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            link.activate()
            await eventually(lambda: len(host.calls) == 1)
            await asyncio.sleep(.03)
            self.assertFalse(task.done())
            self.assertTrue(app.is_open)
            self.assertEqual(len(clicks), 1)
            host.result = True
            link.activate()
            await eventually(lambda: len(host.calls) == 2)
            await asyncio.sleep(.03)
            self.assertFalse(task.done())
            self.assertEqual(len(clicks), 2)
        finally:
            runtime._stop.set()
            try:
                await asyncio.wait_for(task, 2)
            finally:
                app.destroy()

    async def test_unexpected_link_handler_error_still_ends_runtime(self):
        class LinkHost(RecordingHost):
            capabilities = RecordingHost.capabilities | {"open_url"}

            async def open_url(self, url):
                pass

        app, host = App(), LinkHost()
        link = Hyperlink(parent=app, url="https://example.com")

        def broken_handler(event):
            raise RuntimeError("handler failed")

        link.click.connect(broken_handler)
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            link.activate()
            with self.assertRaisesRegex(RuntimeError, "handler failed"):
                await asyncio.wait_for(task, 2)
            self.assertTrue(host.closed)
        finally:
            runtime._stop.set()
            if not task.done():
                await task
            app.destroy()
