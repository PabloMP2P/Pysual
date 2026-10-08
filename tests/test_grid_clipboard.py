"""Selected grid cells use ordinary clipboard ownership and display formatting."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase
from test_library import RecordingHost

from pysual import App, DataGrid, GridColumn, GridRow, TextBox
from pysual.layout import arrange
from pysual.runtime import Runtime


class GridClipboardTests(AsyncUIOwnerTestCase):
    def setUp(self):
        self.app = App()
        self.host = RecordingHost()
        self.grid = DataGrid(
            parent=self.app, width=400, height=200,
            columns=(GridColumn("name", "Name"),
                     GridColumn("amount", "Amount", kind="number", prefix="$", format_spec=",.2f")),
            rows=(GridRow("a", ("Customer A", 1234.567)), GridRow("b", (None, None))),
            selected_key="a",
        )
        self.other = TextBox(parent=self.app, top=220)
        self.runtime = Runtime(self.app, self.host)
        arrange(self.app, self.host)
        self.runtime.router.set_focus(self.grid)

    def tearDown(self):
        self.app.destroy()

    async def test_copy_uses_full_formatted_cell_and_empty_cell(self):
        router = self.runtime.router
        await router.clipboard("c")
        self.assertEqual(self.host.clipboard, "Customer A")
        self.grid.selected_column = 1
        await router.clipboard("c")
        self.assertEqual(self.grid.selected_cell_text, "$1,234.57")
        self.assertEqual(self.host.clipboard, "$1,234.57")
        self.grid.selected_key = "b"
        await router.clipboard("c")
        self.assertEqual(self.host.clipboard, "")
        self.grid.selected_key = None
        self.host.clipboard = "unchanged"
        await router.clipboard("c")
        self.assertIsNone(self.grid.selected_cell_text)
        self.assertEqual(self.host.clipboard, "unchanged")

    async def test_stale_queued_copy_is_ignored_and_cut_paste_stay_text_only(self):
        router = self.runtime.router
        self.host.clipboard = "unchanged"
        for change in (
            lambda: setattr(self.grid, "selected_key", "b"),
            lambda: setattr(self.grid, "selected_column", 1),
            lambda: router.set_focus(self.other),
            lambda: setattr(self.grid, "visible", False),
        ):
            self.grid.update(selected_key="a", selected_column=0, visible=True)
            router.set_focus(self.grid)
            request = router.clipboard("c")
            change()
            await request
            self.assertEqual(self.host.clipboard, "unchanged")
        self.grid.visible = True
        router.set_focus(self.grid)
        before = self.grid.rows
        await router.clipboard("x")
        await router.clipboard("v")
        self.assertEqual(self.grid.rows, before)
        self.assertEqual(self.host.clipboard, "unchanged")
        request = router.clipboard("c")
        self.grid.destroy()
        await request
        self.assertEqual(self.host.clipboard, "unchanged")

    async def test_pending_copy_keeps_captured_text_without_changing_selection(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def write(text):
            entered.set()
            await release.wait()
            self.host.clipboard = text

        self.host.clipboard_write = write
        task = asyncio.create_task(self.runtime.router.clipboard("c"))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.grid.selected_key = "b"
            release.set()
            await task
            self.assertEqual(self.host.clipboard, "Customer A")
            self.assertEqual(self.grid.selected_key, "b")
        finally:
            release.set()
            await task
