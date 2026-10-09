"""Selection continues only while a captured pointer remains outside the editor."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase
from test_library import RecordingHost, eventually
from pysual import App, Container, Rect, TextBox
from pysual.host import Input
from pysual.runtime import Runtime


class TextSelectionScrollTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=500, height=400)
        self.parent = Container(parent=self.app, width=450, height=350)
        self.entry = TextBox(parent=self.parent, left=20, top=20, width=260,
                             height=100, multiline=True, monospace=True,
                             text="\n".join(f"row {i:03d}: " + "word " * 30 for i in range(80)))
        self.runtime = Runtime(self.app, RecordingHost())
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task
        self.app.destroy()

    def send(self, kind, x, y):
        self.runtime.router.process(Input(kind, x=x, y=y, button=1))

    def start_drag(self, *, horizontal=False):
        box = self.entry.bounds
        self.send("pointer_down", box.x + 25, box.y + 20)
        self.send("pointer_move", box.right + 30 if horizontal else box.x + 25,
                  box.y + 20 if horizontal else box.bottom + 30)
        return self.entry._scroll_x if horizontal else self.entry._scroll

    async def test_stationary_drag_extends_read_only_selection_without_editing(self):
        self.entry.read_only = True
        original = self.entry.text
        scroll = self.start_drag()
        anchor = self.entry._anchor
        await eventually(lambda: self.entry._scroll > scroll)
        self.assertEqual(self.entry._anchor, anchor)
        self.assertGreater(self.entry.selection_range[1], 0)
        self.assertEqual(self.entry.text, original)
        self.assertFalse(self.entry.can_undo)
        self.assertIsNotNone(self.entry._selection_task)

    async def test_stationary_horizontal_drag_scrolls_and_reentry_stops(self):
        scroll = self.start_drag(horizontal=True)
        await eventually(lambda: self.entry._scroll_x > scroll)
        box = self.entry.bounds
        self.send("pointer_move", box.x + 50, box.y + 20)
        state = self.entry.capture_view_state()
        self.assertIsNone(self.entry._selection_task)
        await asyncio.sleep(.12)
        self.assertEqual(self.entry.capture_view_state(), state)

    async def test_stationary_pointer_just_above_or_left_scrolls_back_to_start(self):
        scroll = self.start_drag()
        await eventually(lambda: self.entry._scroll > scroll)
        box = self.entry.bounds
        self.send("pointer_move", box.x + 25, box.y - 1)
        await eventually(lambda: self.entry._scroll == 0)
        await eventually(lambda: self.entry._selection_task is None)
        self.send("pointer_up", box.x + 25, box.y - 1)
        self.entry.load_text(self.entry.text)
        scroll = self.start_drag(horizontal=True)
        await eventually(lambda: self.entry._scroll_x > scroll)
        self.send("pointer_move", box.x - 1, box.y + 20)
        await eventually(lambda: self.entry._scroll_x == 0)
        await eventually(lambda: self.entry._selection_task is None)

    async def test_pointer_down_beyond_reserved_viewport_never_restarts_the_anchor(self):
        class ReservedEntry(TextBox):
            def _text_viewport(self):
                area = super()._text_viewport()
                return Rect(area.x, area.y, max(0, area.width - 40), area.height)

        self.app.fps_limit = 10
        self.entry.destroy()
        self.entry = ReservedEntry(parent=self.parent, left=20, top=20, width=260,
                                   height=40, text="word " * 100)
        self.entry.load_text(self.entry.text)
        # The replacement has no hit geometry until a frame arranges it. A
        # fixed sleep can expire before that frame, especially on a busy host.
        await eventually(lambda: self.entry.bounds.width == 260)
        box = self.entry.bounds
        self.send("pointer_down", box.x + self.entry._text_viewport().right + 5, box.y + 20)
        self.assertIs(self.runtime.router.capture, self.entry)
        anchor = self.entry._anchor
        await eventually(lambda: self.entry._caret > anchor)
        self.assertEqual(self.entry._anchor, anchor)
        self.assertGreater(self.entry._caret, anchor)

    async def test_word_selection_autoscroll_keeps_word_endpoints(self):
        line = "alpha beta gamma"
        self.entry.load_text("\n".join([line] * 60))
        await asyncio.sleep(.03)
        box = self.entry.bounds
        x, y = box.x + self.entry._text_left() + 15, box.y + 20
        self.send("pointer_down", x, y)
        self.send("pointer_up", x, y)
        self.send("pointer_down", x, y)
        self.assertEqual(self.entry.selection_text, "alpha")
        self.send("pointer_move", x, box.bottom + 30)
        caret = self.entry._caret
        await eventually(lambda: self.entry._caret > caret)
        self.assertEqual(self.entry._anchor, 0)
        self.assertEqual(self.entry._caret % (len(line) + 1), len("alpha"))

    async def test_gesture_and_lifetime_changes_cancel_pending_scroll(self):
        for action in ("release", "cancel", "blur", "disable", "hide_parent"):
            with self.subTest(action=action):
                self.entry.load_text("\n".join("row " + str(i) for i in range(80)))
                self.entry.enabled = self.parent.visible = True
                await asyncio.sleep(.03)
                self.start_drag()
                if action == "release":
                    self.send("pointer_up", 50, 150)
                elif action in ("cancel", "blur"):
                    self.runtime.router.process(Input("pointer_cancel" if action == "cancel" else "blur"))
                elif action == "disable":
                    self.entry.enabled = False
                elif action == "hide_parent":
                    self.parent.visible = False
                await eventually(lambda: self.entry._selection_task is None)
                state = self.entry.capture_view_state()
                await asyncio.sleep(.12)
                self.assertEqual(self.entry.capture_view_state(), state)

    async def test_document_boundary_stops_task_and_destroy_cancels_owned_work(self):
        self.entry.load_text("\n".join("row " + str(i) for i in range(8)))
        await asyncio.sleep(.03)
        self.start_drag()
        await eventually(lambda: self.entry._selection_task is None)
        self.assertEqual(self.entry._scroll, self.entry.line_count - self.entry._page_rows())
        self.entry.load_text("\n".join("row " + str(i) for i in range(80)))
        self.start_drag()
        owned = self.entry._selection_task
        self.assertIsNotNone(owned)
        self.entry.destroy()
        await eventually(owned.done)
        self.assertTrue(owned.cancelled())
