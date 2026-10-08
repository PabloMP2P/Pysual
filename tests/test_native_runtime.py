"""The public application loop uses real native processes and retained scenes."""

import asyncio
from dataclasses import asdict
import json
import time
import unittest

from pysual import App, Button, Label, TextBox, terminal
from pysual.backends._native_client import native_executable
from pysual.backends.native import NativeHost, NativeTermTextHost
from pysual.geometry import Rect
from pysual.theme import Style


class NativeStyleEncodingTests(unittest.TestCase):
    def test_surface_and_marker_encode_independent_scalar_style_dictionaries(self):
        styles = (Style(), Style(
            fill="#123456", foreground="#abcdef", border="#fedcba", radius=3.5,
            border_width=1, font_family="mono", font_size=14, padding=6,
            fill_end="#ffffff", border_end="#000000", gradient_axis="horizontal",
            bevel="raised", bevel_width=2, bevel_light="#eeeeee", bevel_dark="#222222",
            highlight="#334455", inner_border="#667788", glow="#aabbcc", glow_width=3,
            pattern="grid", pattern_color="#8899aa", pattern_spacing=4, shadow="#111111",
            shadow_blur=8, shadow_x=-2.5, shadow_y=3,
        ))
        bounds = Rect(1, 2, 30, 40)
        for style in styles:
            for host in (NativeHost(), NativeTermTextHost()):
                with self.subTest(style=style, backend=host.backend):
                    self.assertTrue(host.styled_rect(bounds, style))
                    if host.backend == "terminal":
                        self.assertTrue(host.marker(bounds, style, shape="square", checked=True))
                    else:
                        self.assertTrue(host.styled_rect(bounds, style))
                    first, second = (command[2] for command in host._commands)
                    self.assertEqual(json.dumps(first), json.dumps(asdict(style)))
                    self.assertEqual(second, first)
                    self.assertIsNot(first, second)
                    first["fill"] = "#000000"
                    self.assertEqual(second["fill"], style.fill)
                    host.close()


class Demo(App):
    def build(self):
        self.width, self.height = 320, 160
        self.layout, self.padding, self.spacing = "stack", 8, 8
        self.message = Label(text="Ready", height=32)
        self.action = Button(text="Change", height=48)

    async def action_on_click(self, event):
        await asyncio.sleep(0.01)
        self.message.text = "Changed"


def until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Native application did not complete its event")
        time.sleep(0.005)


@unittest.skipUnless(native_executable().is_file(), "Build the native helper")
class NativeRuntimeTests(unittest.TestCase):
    def test_long_proportional_unicode_and_password_lines_open_and_scroll(self):
        for value, properties in (("a" * 2000, {}), ("a" * 20000, {}),
                                  ("é" * 2000, {"monospace": True}),
                                  ("e\u0301" * 2000, {}),
                                  ("secret" * 400, {"password": True})):
            with self.subTest(properties=properties, prefix=value[:2]):
                app = App(width=480, height=160, layout="absolute", reduce_motion=True)
                host = NativeHost(hidden=True, vsync=False)
                entry = TextBox(parent=app, text=value, width=470, height=150,
                                multiline=True, **properties)
                try:
                    app.run(backend=host)
                    self.assertTrue(app.is_open)
                    for offset in (len(value) // 2, len(value), 0):
                        # A public view update exercises both the middle and
                        # far-right retained scene, without replacing the value.
                        before = host.native_stats()["scene_updates"]
                        entry.restore_view_state((offset, offset, 0, offset * 5.0))
                        until(lambda: host.native_stats()["scene_updates"] > before)
                        self.assertTrue(app.is_open)
                    self.assertEqual(entry.text, value)
                finally:
                    app.close()
                    app.wait(timeout=5)
                    app.destroy()

    def test_window_model_async_handler_and_retained_idle(self):
        host = NativeHost(hidden=True, vsync=False)
        app = Demo(reduce_motion=True)
        self.addCleanup(app.destroy)
        try:
            app.run(backend=host)
            self.assertTrue(app.is_open)
            app.action.activate()
            until(lambda: app.message.text == "Changed")
            until(lambda: host.native_stats()["scene_updates"] >= 2)
            revision = host.native_stats()["scene_updates"]
            time.sleep(0.08)
            self.assertEqual(host.native_stats()["scene_updates"], revision)
            self.assertIn("scene_patches", app.capabilities)
        finally:
            app.close()
            app.wait(timeout=5)
        self.assertFalse(host._client.is_alive)

    def test_terminal_native_input_updates_model_and_cells(self):
        host = terminal(renderer="c", hidden=True, color="truecolor")
        app = Demo(reduce_motion=True)
        self.addCleanup(app.destroy)
        try:
            app.run(backend=host)
            bounds = app.action.bounds
            x, y = int((bounds.x + bounds.width / 2) / 8) + 1, int((bounds.y + bounds.height / 2) / 16) + 1
            host._host._request("feed_input", data=f"\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m")
            until(lambda: app.message.text == "Changed")
            until(lambda: any("Changed" in row for row in host.snapshot()["rows_text"]))
            self.assertEqual(host.renderer, "c")
            self.assertIn("scene_patches", app.capabilities)
        finally:
            app.close()
            app.wait(timeout=5)
        self.assertFalse(host._host._client.is_alive)
