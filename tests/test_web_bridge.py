"""Shared SVG frames and local browser delivery, plus installed JS contracts."""

import asyncio
import base64
from contextlib import chdir
from importlib import resources
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from xml.etree import ElementTree as ET

from pysual._png import encode_png
from pysual.backends import _SingleSurfaceHost, _SingleSurfaceSession
from pysual.backends import _font
from pysual.backends._web_pyodide import BundledSVGHost
from pysual.geometry import Rect
from pysual.host import CapabilityError
from pysual.image_resources import MAX_IMAGE_BYTES, image_source


NS = {"s": "http://www.w3.org/2000/svg"}


def browser_bridge(**options):
    return SimpleNamespace(**{
        "width": 640, "height": 400, "renderScale": 1.5,
        "resourceRevision": 0, "open": Mock(), "close": Mock(),
        "setSessionNamespace": Mock(),
        "title": Mock(), "textInput": Mock(), "present": Mock(),
        "poll": lambda: "[]", **options,
    })


class WebBundleClipboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_clipboard_write_requires_success_and_cancels_its_service(self):
        results, calls, cancelled = [True, None, False], [], []
        started = asyncio.Event()

        async def write(text, operation):
            calls.append((text, operation))
            if results:
                return results.pop(0)
            started.set()
            await asyncio.Future()

        bridge = SimpleNamespace(writeClipboard=write, cancelOperation=cancelled.append)
        with patch.dict("sys.modules", {"js": SimpleNamespace(pysualHost=bridge)}):
            host = BundledSVGHost()
        await host.clipboard_write("copied")
        for text in ("closed", "cancelled"):
            with self.assertRaises(CapabilityError):
                await host.clipboard_write(text)
        task = asyncio.create_task(host.clipboard_write("pending"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(calls, [("copied", 1), ("closed", 2), ("cancelled", 3), ("pending", 4)])
        self.assertEqual(cancelled, [4])


class WebBundleBridgeTests(unittest.TestCase):
    def host(self, bridge):
        with patch.dict("sys.modules", {"js": SimpleNamespace(pysualHost=bridge)}):
            return BundledSVGHost()

    def test_rejected_second_surface_preserves_first_bridge_and_namespace(self):
        bridge = browser_bridge()
        session = _SingleSurfaceSession("web")
        first = _SingleSurfaceHost(session, self.host(bridge))
        second = _SingleSurfaceHost(session, self.host(bridge))
        first.set_session_namespace("apps.First")
        bridge.setSessionNamespace.assert_not_called()
        first.open("First", 640, 400, True, 1)
        second.set_session_namespace("apps.Second")
        with self.assertRaisesRegex(CapabilityError, "one presentation surface"):
            second.open("Second", 640, 400, True, 1)
        second.close()  # Failed runtime startup still cleans up its host.
        bridge.close.assert_not_called()
        bridge.setSessionNamespace.assert_called_once_with("apps.First")
        self.assertIs(session.active, first)
        first.begin("#ffffff")
        first.present()
        bridge.present.assert_called_once()
        first.close()
        bridge.close.assert_called_once()
        second.open("Second", 640, 400, True, 1)
        bridge.setSessionNamespace.assert_called_with("apps.Second")
        second.close()
        self.assertIsNone(session.active)
        self.assertEqual(bridge.close.call_count, 2)

    def test_failed_open_only_closes_a_bridge_it_acquired(self):
        for failure in ("open", "namespace", "scene"):
            with self.subTest(failure=failure):
                bridge = browser_bridge()
                host = self.host(bridge)
                target = {"open": bridge.open,
                          "namespace": bridge.setSessionNamespace}.get(failure)
                if target is not None:
                    target.side_effect = RuntimeError("startup failure")
                else:
                    host.reset_scene = Mock(side_effect=RuntimeError("startup failure"))
                with self.assertRaisesRegex(RuntimeError, "startup failure"):
                    host.open("Failed", 640, 400, True, 1)
                host.close()
                host.close()
                self.assertEqual(bridge.close.call_count, int(failure != "open"))
                self.assertFalse(host._opened)

    def test_title_updates_cross_the_bridge_only_when_changed_across_reopen(self):
        bridge = browser_bridge()
        host = self.host(bridge)
        host.open("Initial", 640, 400, True, 1)
        host.set_title("Initial")
        bridge.title.assert_not_called()
        host.set_title("Changed")
        host.set_title("Changed")
        bridge.title.assert_called_once_with("Changed")
        host.close()
        host.open("Reopened", 640, 400, True, 1)
        bridge.open.assert_called_with("Reopened", 640, 400, 1)
        host.set_title("Reopened")
        bridge.title.assert_called_once_with("Changed")
        host.set_title("Changed")
        self.assertEqual(bridge.title.call_count, 2)

    def test_lines_and_metrics_need_no_drawing_or_measurement_bridge_calls(self):
        bridge = browser_bridge()
        host = self.host(bridge)
        host.open("Shared scene", 640, 400, True, None)
        host.begin("#ffffff")
        host.lines(((1, 2), (3, 4), (5, 6)), "#12345680", 3)
        host.text("AV &\n\nCafe\u0301", 10, 20, "#123456", 16)
        self.assertEqual(host.measure("AV &\n\nCafe\u0301", 16), _font.measure("AV &\n\nCafe\u0301", 16))
        bridge.present.assert_not_called()
        host.present()
        packet = json.loads(bridge.present.call_args.args[0])
        self.assertTrue(packet["reset"])
        self.assertEqual((packet["width"], packet["height"]), (640, 400))
        root = ET.fromstring(host.export_svg())
        lines = root.findall("s:line", NS)
        self.assertEqual([(line.get("x1"), line.get("y1"), line.get("x2"), line.get("y2")) for line in lines],
                         [("1", "2", "3", "4"), ("3", "4", "5", "6")])
        self.assertTrue(all(line.get("stroke") == "#12345680" and line.get("stroke-width") == "3" for line in lines))
        text = root.findall("s:text", NS)
        self.assertEqual([node.text for node in text], ["AV &", "Cafe\u0301"])
        self.assertEqual(float(text[1].get("y")) - float(text[0].get("y")), 2 * host.measure("", 16)[1])

    def test_scene_deltas_are_local_and_reopen_starts_a_fresh_frame(self):
        bridge = browser_bridge()
        host = self.host(bridge)
        host.open("Scene", 640, 400, True, 1)
        for color in ("#ffffff", "#ffffff", "#000000"):
            host.begin(color)
            host.present()
        packets = [json.loads(call.args[0]) for call in bridge.present.call_args_list]
        self.assertEqual([packet["reset"] for packet in packets], [True, False, False])
        self.assertEqual(packets[1]["updates"], [])
        self.assertEqual(len(packets[2]["updates"]), 1)
        host.set_title("Renamed")
        host.text_input(Rect(1, 2, 30, 18))
        host.present()
        packet = json.loads(bridge.present.call_args.args[0])
        self.assertEqual(packet["title"], "Renamed")
        self.assertEqual(packet["text_input"], [1, 2, 30, 18])
        bridge.title.assert_called_with("Renamed")
        bridge.textInput.assert_called_with(1, 2, 30, 18)
        with self.assertRaises(RuntimeError):
            host.open("Again", 640, 400, True, 1)
        host.close()
        host.open("New scene", 640, 400, True, 1)
        host.begin("#123456")
        host.present()
        packet = json.loads(bridge.present.call_args.args[0])
        self.assertTrue(packet["reset"])
        self.assertEqual(packet["title"], "New scene")
        self.assertIsNone(packet["text_input"])
        self.assertEqual(packet["length"], 1)

    def test_vector_surfaces_keep_clipping_and_lifetime_without_raster_methods(self):
        bridge = browser_bridge()
        host = self.host(bridge)
        self.assertIn("render_surfaces", host.capabilities)
        self.assertIn("image_fit", host.capabilities)
        self.assertNotIn("isolated_paint", host.capabilities)
        host.open("Cached", 640, 400, True, 1)
        surface = host.surface_create(Rect(10.25, 20, 40, 30))
        host.surface_begin(surface)
        host.rect(Rect(0, 0, 100, 100), "#ff000080")
        host.surface_end()
        host.begin("#ffffff")
        host.surface_blit(surface)
        host.present()
        cached = ET.fromstring(host.export_svg()).find("s:g", NS)
        bounds = cached.find(".//s:clipPath/s:rect", NS)
        self.assertEqual(bounds.attrib, {"x": "10.25", "y": "20", "width": "40", "height": "30"})
        painted = cached.find(".//s:g[@clip-path]/s:rect", NS)
        self.assertEqual(painted.get("fill"), "#ff000080")
        self.assertIsNone(cached.find(".//s:svg", NS))
        host.close()
        host.open("Reopened", 640, 400, True, 1)
        with self.assertRaises(ValueError):
            host.surface_blit(surface)

    def test_image_decode_failures_become_bounded_diagnostics(self):
        bridge = browser_bridge()
        host = self.host(bridge)
        host.open("Images", 640, 400, True, 1)
        source = image_source(encode_png(1, 1, bytes((255, 0, 0, 255))))
        host.image(source, Rect(0, 0, 1, 1))
        identifier = host._images[source][1]
        bridge.poll = lambda: json.dumps([
            {"kind": "image_error", "text": str(identifier)},
            {"kind": "image_error", "text": str(identifier)},
            {"kind": "image_error", "text": "unknown"},
            {"kind": "image_error", "text": str(identifier + 1)},
        ])
        events = host.poll()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "resource_error")
        self.assertIn("Browser could not decode", events[0].text)
        self.assertEqual(host.poll(), [])

    def test_included_images_embed_relative_and_absolute_file_paths(self):
        host = self.host(browser_bridge())
        assets = {
            "image.png": ("image/png", encode_png(1, 1, bytes((255, 0, 0, 255)))),
            "image.svg": ("image/svg+xml", b'<svg xmlns="http://www.w3.org/2000/svg"/>'),
        }
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            # Match the documented Path(__file__).resolve().parent pattern.
            script = Path("app.py").resolve()
            for name, (mime, data) in assets.items():
                path = script.parent / name
                path.write_bytes(data)
                expected = f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
                for source in (name, str(path)):
                    with self.subTest(source=source):
                        self.assertEqual(host._resolve_image_source(source), expected)

    def test_browser_urls_keep_their_resolution_when_not_virtual_files(self):
        host = self.host(browser_bridge())
        png = image_source(encode_png(1, 1, bytes((255, 0, 0, 255))))
        for source in (
            "https://example.com/image.png", "http://example.com/image.png",
            "blob:https://example.com/image", "//example.com/image.png", png,
            "data:image/svg+xml,%3Csvg/%3E", "file:///image.png",
        ):
            with self.subTest(source=source), patch.object(
                Path, "is_file", side_effect=AssertionError("URL used as local file")
            ):
                self.assertEqual(host._resolve_image_source(source), source)
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            for source in ("/missing-browser-image.png", "missing.png", "../outside.png",
                           str(Path("missing.png").resolve())):
                with self.subTest(source=source):
                    self.assertEqual(host._resolve_image_source(source), source)
            data = encode_png(1, 1, bytes((0, 0, 255, 255)))
            Path("my image.png").write_bytes(data)
            self.assertEqual(host._resolve_image_source("my%20image.png?v=1#preview"),
                             image_source(data))

    def test_included_absolute_and_relative_images_keep_size_limits(self):
        host = self.host(browser_bridge())
        with tempfile.TemporaryDirectory() as directory, chdir(directory):
            for name, header, limit in (
                ("large.png", encode_png(1, 1, bytes((255, 0, 0, 255))), MAX_IMAGE_BYTES),
                ("large.svg", b'<svg xmlns="http://www.w3.org/2000/svg"/>', 32 * 1024 * 1024),
            ):
                path = Path(name).resolve()
                with path.open("wb") as stream:
                    stream.write(header)
                    stream.truncate(limit + 1)
                for source in (name, str(path)):
                    with self.subTest(source=source), self.assertRaisesRegex(ValueError, "MiB"):
                        host._resolve_image_source(source)

    def test_page_scale_is_explicit_and_resource_changes_invalidate_caches(self):
        bridge = browser_bridge()
        host = self.host(bridge)
        with self.assertRaises(CapabilityError):
            host.open("Invalid scale", 640, 400, True, 2)
        bridge.open.assert_not_called()
        host.open("Valid", 640, 400, True, None)
        revision = host.resource_revision
        bridge.resourceRevision += 1
        self.assertGreater(host.resource_revision, revision)
        self.assertEqual(host.render_scale, 1.5)

    def test_browser_host_contracts(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Install Node.js 20+ to run the web bridge checks.")
        with resources.as_file(
            resources.files("pysual").joinpath("web/host.js")
        ) as host:
            result = subprocess.run(
                [
                    node, "--test",
                    str(Path(__file__).parent / "web/_web_pyodide.test.mjs"),
                    str(Path(__file__).parent / "web/browser_services.test.mjs"),
                ],
                env={**os.environ, "PYSUAL_TEST_HOST": str(host)},
                capture_output=True,
                text=True,
                timeout=20,
            )
        diagnostics = re.sub(r"data:text/javascript;base64,[A-Za-z0-9+/=]+", "<embedded-js>",
                             result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, diagnostics)
