"""Popup sizing, menu adornments and pointer targets share fixed text rows."""

import asyncio
import unittest
from _ui_testcase import AsyncUIOwnerTestCase

from pysual import App, MenuItem
from pysual.backends.term_text import TermTextHost
from pysual.host import Input
from pysual.runtime import Runtime

from test_library import RecordingHost
from _terminal import MemoryTerminal, until


class FixedPopupRowsTests(AsyncUIOwnerTestCase):
    async def start(self, host=None):
        class Demo(App):
            def build(self):
                self.choice = self.dropdown(
                    items=tuple(f"Choice {i:02}" for i in range(30)),
                    selected_index=17, visible_rows=4,
                    left=24, top=37, width=250, height=38,
                )
                self.editable = self.combo_box(
                    items=("Alpha", "Beta", "Gamma"), text="Gamma",
                    left=320, top=37, width=180, height=38,
                )
                self.menu_commands = []
                self.context = self.menu(items=(
                    MenuItem("save", "Save", "Ctrl+S", checked=True, icon="download"),
                    MenuItem("more", "More", children=(
                        MenuItem("child_a", "Child A"), MenuItem("child_b", "Child B"),
                    )),
                    MenuItem("separator", "", separator=True),
                    MenuItem("disabled", "Disabled", enabled=False),
                    MenuItem("finish", "Finish", "Ctrl+F"),
                ))

            def context_on_command(self, event):
                self.menu_commands.append(event.key)

        app = Demo(reduce_motion=True)
        host = host or TermTextHost(io=MemoryTerminal(100, 30), probe_timeout=0, environ={})
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())

        async def cleanup():
            runtime._stop.set()
            await asyncio.wait_for(task, 10)

        self.addAsyncCleanup(cleanup)
        await until(lambda: runtime.frames > 0 or task.done())
        if task.done():
            await task
        return app, host, runtime

    @staticmethod
    def rendered_row(host, text, *, left=0, right=None, top=0):
        return next(
            index for index, row in enumerate(host._renderer.snapshot_rows())
            if index >= top and text in row[left:right]
        )

    def click_row(self, runtime, host, rows, text):
        left, right = int(rows.bounds.x / 8), int(rows.bounds.right / 8)
        row = self.rendered_row(host, text, left=left, right=right, top=int(rows.bounds.y / 16))
        y = (row + 0.5) * 16
        x = rows.bounds.x + 60
        runtime.router.process(Input("pointer_down", x=x, y=y))
        runtime.router.process(Input("pointer_up", x=x, y=y))

    async def test_dropdown_and_combobox_visible_rows_and_pointer_commit(self):
        app, host, runtime = await self.start()
        app.choice.open()
        runtime._paint_frame()
        popup = runtime.popup
        choices = popup.children[0]
        self.assertEqual((popup.height, choices.bounds.height), (64, 64))
        self.assertEqual((choices._scroll, choices._visible_rows()), (14, 4))
        row_numbers = [
            self.rendered_row(host, f"Choice {i:02}", top=int(choices.bounds.y / 16))
            for i in range(14, 18)
        ]
        self.assertEqual(row_numbers, list(range(row_numbers[0], row_numbers[0] + 4)))
        self.click_row(runtime, host, choices, "Choice 15")
        self.assertEqual(app.choice.selected_index, 15)
        self.assertIsNone(runtime.popup)
        app.editable.open()
        runtime._paint_frame()
        self.assertEqual(runtime.popup.height, 48)
        self.click_row(runtime, host, runtime.popup.children[0], "Beta")
        self.assertEqual(app.editable.text, "Beta")
        self.assertIsNone(runtime.popup)

    async def test_menu_shortcuts_checks_and_cascade_align_with_command_rows(self):
        app, host, runtime = await self.start()
        app.context.show(app.choice, at=(24, 91))
        runtime._paint_frame()
        popup = runtime.popup
        root = popup.children[0]
        self.assertEqual((popup.height, root.bounds.height), (80, 80))
        rows = host._renderer.snapshot_rows()
        save_row = self.rendered_row(host, "Save")
        self.assertIn("Ctrl+S", rows[save_row])
        self.assertIn("✓", rows[save_row])
        self.assertIn("↓", rows[save_row])
        self.assertEqual(self.rendered_row(host, "More"), save_row + 1)
        self.assertIn("›", rows[save_row + 1])
        self.assertEqual(self.rendered_row(host, "Finish"), save_row + 4)
        self.click_row(runtime, host, root, "More")
        runtime._paint_frame()
        child = popup.children[1]
        self.assertEqual(child.bounds.y, root.bounds.y + 16)
        self.assertEqual(child.bounds.height, 32)
        self.assertEqual(self.rendered_row(host, "Child A"), save_row + 1)
        self.assertEqual(self.rendered_row(host, "Child B"), save_row + 2)
        self.click_row(runtime, host, child, "Child B")
        self.assertIsNone(runtime.popup)
        await until(lambda: app.menu_commands == ["child_b"])

    async def test_long_menu_keyboard_reveal_and_mouse_share_visible_rows(self):
        app, host, runtime = await self.start()
        app.context.items = tuple(MenuItem(f"cmd_{i}", f"Command {i:02}") for i in range(30))
        app.context.show(app.choice, at=(24, 91))
        runtime._paint_frame()
        rows = runtime.popup.children[0]
        self.assertEqual((runtime.popup.height, rows._visible_rows()), (192, 12))
        runtime.router.process(Input("key_down", key="End"))
        runtime._paint_frame()
        self.assertEqual((rows.selected_index, rows._scroll), (29, 18))
        self.assertEqual(
            self.rendered_row(host, "Command 29") - self.rendered_row(host, "Command 18"), 11
        )
        self.click_row(runtime, host, rows, "Command 23")
        self.assertIsNone(runtime.popup)
        await until(lambda: app.menu_commands == ["cmd_23"])

    async def test_pixel_popup_sizes_and_scroll_capacity_keep_their_defaults(self):
        app, _, runtime = await self.start(RecordingHost())
        app.choice.open()
        runtime._paint_frame()
        choices = runtime.popup.children[0]
        self.assertEqual((runtime.popup.height, choices._scroll), (120, 14))
        runtime.popup.dismiss()
        app.context.show(app.choice)
        runtime._paint_frame()
        self.assertEqual(runtime.popup.height, 150)
        self.assertEqual(runtime.popup.children[0]._row_height(), 30)


if __name__ == "__main__":
    unittest.main()
