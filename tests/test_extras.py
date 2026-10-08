"""Small controls retain composition, ordinary input and explicit host services."""

import asyncio
import unittest
from _ui_testcase import AsyncUIOwnerTestCase

from pysual import App, Button
from pysual.extras import GroupBox, Hyperlink, Separator
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree, resolve_style
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


class ExtraGeometryTests(unittest.TestCase):
    def test_group_flow_respects_available_width_and_keeps_heading(self):
        app, host = App(width=220, height=300, layout="stack"), RecordingHost()
        try:
            group = GroupBox(
                parent=app,
                title="Actions",
                layout="flow",
                direction="horizontal",
                padding=10,
                spacing=0,
            )
            children = [
                Button(parent=group, text=str(i), width=100, height=30)
                for i in range(3)
            ]
            arrange(app, host)
            self.assertEqual(children[-1].inspect().clip.height, 30)
            first_height = group.bounds.height
            app.width = 420
            arrange(app, host)
            self.assertLess(group.bounds.height, first_height)
            self.assertEqual(children[-1].inspect().clip.height, 30)
        finally:
            app.destroy()

    def test_group_intrinsic_size_includes_heading_and_full_child(self):
        app, host = App(width=400, height=240, layout="stack"), RecordingHost()
        try:
            group = GroupBox(parent=app, title="Settings", layout="stack")
            child = Button(parent=group, text="Apply", height=38)
            arrange(app, host)
            heading = resolve_style(group, "heading").font_size * 1.4 + 8
            self.assertGreaterEqual(
                group.bounds.height, 38 + group.padding * 2 + heading
            )
            self.assertEqual(child.inspect().clip.height, child.bounds.height)
            self.assertGreaterEqual(
                child.bounds.y, group.bounds.y + group.padding + heading
            )
            group.title = ""
            arrange(app, host)
            self.assertEqual(group.bounds.height, 38 + group.padding * 2)
        finally:
            app.destroy()

    def test_separator_orientation_uses_normal_painter_geometry(self):
        class LinesHost(RecordingHost):
            def __init__(self):
                super().__init__()
                self.lines = []

            def line(self, *args):
                self.lines.append(args)

        app, host = App(), LinesHost()
        try:
            horizontal = Separator(parent=app, left=10, top=20, width=100, height=10)
            vertical = Separator(
                parent=app,
                left=150,
                top=20,
                width=10,
                height=100,
                orientation="vertical",
            )
            arrange(app, host)
            paint_tree(app, host)
            self.assertEqual(host.lines[0][:4], (10, 25, 110, 25))
            self.assertEqual(host.lines[1][:4], (155, 20, 155, 120))
            self.assertFalse(horizontal.focusable)
            self.assertFalse(vertical.focusable)
        finally:
            app.destroy()


class HyperlinkTests(AsyncUIOwnerTestCase):
    async def test_pointer_keyboard_and_shortcut_share_activation_and_owner_cleanup(
        self,
    ):
        class LinkHost(RecordingHost):
            capabilities = RecordingHost.capabilities | {"open_url"}

            def __init__(self):
                super().__init__()
                self.urls = []

            async def open_url(self, url):
                self.urls.append(url)

        app, host = App(width=400, height=240), LinkHost()
        link = Hyperlink(
            parent=app,
            text="Documentation",
            url="https://example.com/docs",
            shortcut="Ctrl+L",
            left=10,
            top=10,
            width=180,
            height=38,
        )
        clicks = []
        link.click.connect(clicks.append)
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            for event in (
                Input("pointer_down", 20, 20),
                Input("pointer_up", 20, 20),
                Input("key_down", key="Enter"),
                Input("key_up", key="Enter"),
                Input("key_down", key="Space"),
                Input("key_up", key="Space"),
                Input("key_down", key="l", ctrl=True),
            ):
                runtime.router.process(event)
            await asyncio.sleep(0.025)
            self.assertEqual(host.urls, [link.url] * 4)
            self.assertEqual(len(clicks), 4)
            self.assertTrue(all(event.origin == "user" for event in clicks))
            link.enabled = False
            link.activate()
            runtime.router.process(Input("key_down", key="l", ctrl=True))
            await asyncio.sleep(0.025)
            self.assertEqual(len(host.urls), 4)
            link.enabled = True
            link.url = ""
            link.activate()
            await asyncio.sleep(0.025)
            self.assertEqual(len(clicks), 5)
            self.assertEqual(len(host.urls), 4)
            link.destroy()
            self.assertIsNone(runtime.router.focus)
        finally:
            runtime._stop.set()
            await task
