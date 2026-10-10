"""U+0000 is removed from display text and rejected for every other native string.

Terminal painting already drops that character and keeps the suffix. The native
window path used to forward it, and the C host then stopped at the first zero.
These checks cover measurement, drawing, retained updates and service calls.
Clipboard tests do not read or write the desktop clipboard.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import unittest
from uuid import uuid4

from pysual import CapabilityError, Rect
from pysual.backends._native_client import (
    NativeClient, NativeHostError, display_text, prepare_native_request,
)
from pysual.backends._term_cells import CellRenderer, safe_text
from pysual.backends.native import NativeHost
from test_native_window import bmp_pixels

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "pysual"
EXE = Path(os.environ.get(
    "PYSUAL_HOST", PACKAGE / "bin" / ("pysual-host.exe" if sys.platform == "win32" else "pysual-host"),
))
_NUL = "Embedded NUL"


class EmbeddedNulPolicyTests(unittest.TestCase):
    def test_display_text_drops_nul_and_terminals_keep_the_same_suffix(self):
        self.assertEqual(display_text("A\0B"), "AB")
        self.assertEqual(display_text("\0café\0λ\0"), "caféλ")
        self.assertEqual(safe_text("A\0B"), display_text("A\0B"))
        self.assertEqual(safe_text("café\0λ"), "caféλ")
        # Other controls stay terminal-specific. The window can still carry them.
        self.assertEqual(display_text("A\x01B"), "A\x01B")
        self.assertEqual(safe_text("A\x01B"), "AB")
        cells = CellRenderer(8, 2)
        self.assertEqual(cells.measure("A\0B", 16, True), cells.measure("AB", 16, True))
        self.assertGreater(cells.measure("AB", 16, True)[0], cells.measure("A", 16, True)[0])
        cells.begin("#000000")
        cells.text("café\0λ", 0, 0, "#ffffff", 16, True)
        self.assertTrue(cells.snapshot_rows()[0].startswith("caféλ"))

    def test_serialization_sanitizes_display_strings_and_rejects_the_rest(self):
        untouched = {"text": "A\\u0000B", "size": 16, "mono": True}
        self.assertIs(prepare_native_request("measure", untouched), untouched)
        original = {"text": "A\0B\0C", "size": 16}
        prepared = prepare_native_request("measure", original)
        self.assertEqual(prepared["text"], "ABC")
        self.assertEqual(original["text"], "A\0B\0C")
        self.assertEqual(
            prepare_native_request("measure_many", {"texts": ["A\0B", "AB"]})["texts"],
            ["AB", "AB"],
        )
        self.assertEqual(prepare_native_request("set_title", {"title": "A\0B"})["title"], "AB")
        self.assertEqual(
            prepare_native_request("open", {"title": "A\0B", "backend": "window"})["title"], "AB",
        )
        command = ["text", "café\0λ", 0, 0, "#fff", 16, False]
        fields = {"commands": [command, ["rect", [0, 0, 1, 1], "#fff", 0, "", 0]]}
        prepared = prepare_native_request("frame", fields)
        self.assertEqual(command[1], "café\0λ")
        self.assertEqual(prepared["commands"][0][1], "caféλ")
        self.assertIs(prepared["commands"][1], fields["commands"][1])
        segment = {"id": "label", "commands": [["text", "A\0B", 0, 0, "#fff", 16, True]]}
        prepared = prepare_native_request("patch", {"upsert": [segment]})
        self.assertEqual(segment["commands"][0][1], "A\0B")
        self.assertEqual(prepared["upsert"][0]["commands"][0][1], "AB")
        for op, fields in (
            ("clipboard_write", {"text": "A\0B"}),
            ("frame", {"commands": [["image", "A\0B.png", [0, 0, 1, 1], "#fff", "stretch"]]}),
            ("patch", {"upsert": [{"id": "A\0B", "commands": [["text", "AB", 0, 0, "#fff", 16, True]]}]}),
            ("capture", {"path": "A\0B.bmp"}),
            ("reload_image", {"source": "A\0B.png"}),
            ("open", {"title": "OK", "font_dir": "A\0B"}),
            ("measure", {"text": "AB", "extra": "A\0B"}),
        ):
            with self.subTest(op=op):
                with self.assertRaisesRegex(ValueError, _NUL):
                    prepare_native_request(op, fields)
        with self.assertRaisesRegex(ValueError, _NUL):
            prepare_native_request("clipboard_\0write", {"text": "AB"})

    def test_adapter_records_display_text_and_does_not_send_rejected_services(self):
        class Recorder:
            def __init__(self):
                self.calls = []

            def request(self, op, **kwargs):
                self.calls.append((op, kwargs))
                if op == "open":
                    return {"viewport": {"width": 320, "height": 160, "scale": 1}}
                if op == "measure":
                    return [12, 16]
                if op == "measure_many":
                    return [[8, 16] for _ in kwargs["texts"]]
                return {}

            def close(self):
                pass

        recorder = Recorder()
        host = NativeHost(hidden=True, vsync=False, client_factory=lambda on_event: recorder)
        self.addCleanup(host.close)
        host.open("A\0B", 320, 160, False, 1)
        self.assertEqual(host._title, "AB")
        self.assertEqual(recorder.calls[0][1]["title"], "AB")
        host.set_title("AB")
        self.assertEqual([call[0] for call in recorder.calls], ["open"])
        host.text("café\0λ", 8, 8, "#ffffff", 32, True)
        self.assertEqual(host._commands, [["text", "caféλ", 8, 8, "#ffffff", 32, True]])
        host.measure("A\0B", 16, True)
        host.measure("AB", 16, True)
        self.assertEqual(
            [call for call in recorder.calls if call[0] == "measure"],
            [("measure", {"text": "AB", "size": 16, "mono": True})],
        )
        measured = host.measure_many(("A\0B", "AB", "A"), 16, True)
        self.assertEqual(measured[0], measured[1])
        self.assertNotEqual(measured[0], measured[2])
        # AB was measured above, so only the uncached suffix is sent.
        self.assertEqual(recorder.calls[-1][1]["texts"], ["A"])

        client = NativeClient()
        self.addCleanup(client.close)
        service = NativeHost(hidden=True, vsync=False)
        service._client = client
        service._closed = False
        service.image("A\0B.png", Rect(0, 0, 8, 8))
        self.assertIn("\0", service._commands[0][1])
        with self.assertRaisesRegex(ValueError, _NUL):
            service.present()
        self.assertIsNone(client.process)
        self.assertEqual(client.diagnostics["requests_sent"], 0)

        async def write_clipboard():
            with self.assertRaises(CapabilityError) as caught:
                await service.clipboard_write("A\0B")
            self.assertIn(_NUL, str(caught.exception.__cause__))

        asyncio.run(write_clipboard())
        self.assertEqual(client.diagnostics["requests_sent"], 0)
        self.assertIsNone(client.process)


@unittest.skipUnless(EXE.is_file(), "Build the standalone C host first")
class NativeWindowEmbeddedNulTests(unittest.TestCase):
    def setUp(self):
        self.client = NativeClient(lambda event: None, executable=EXE)
        self.addCleanup(self.client.close)
        self.client.request(
            "open", backend="window", width=320, height=160, title="Embedded NUL",
            hidden=True, vsync=False, scale=1, resizable=False,
            font_dir=str(PACKAGE / "assets"),
        )
        self.directory = PACKAGE.parents[1] / "work" / "embedded-nul" / uuid4().hex
        self.directory.mkdir(parents=True)
        self.output = self.directory / "frame.bmp"
        self.addCleanup(self.cleanup_capture)

    def cleanup_capture(self):
        self.output.unlink(missing_ok=True)
        self.directory.rmdir()

    def paint(self, commands):
        self.client.request("frame", commands=commands)
        self.client.request("present")
        self.client.request("capture", path=str(self.output))
        stats = self.client.request("stats")
        return stats, bmp_pixels(self.output)

    def test_measurement_keeps_characters_after_nul(self):
        full = self.client.request("measure", text="caféλ", size=16, mono=False)
        split = self.client.request("measure", text="café\0λ", size=16, mono=False)
        prefix = self.client.request("measure", text="café", size=16, mono=False)
        self.assertEqual(split, full)
        self.assertGreater(full[0], prefix[0])
        batch = self.client.request(
            "measure_many", texts=["A\0\0B", "AB", "A", "A\nB\0C", "A\nBC", "\0"],
            size=16, mono=True,
        )
        self.assertEqual(batch[0], batch[1])
        self.assertGreater(batch[1][0], batch[2][0])
        self.assertEqual(batch[3], batch[4])
        self.assertGreater(batch[4][0], self.client.request(
            "measure", text="A\nB", size=16, mono=True)[0])
        self.assertEqual(batch[5], self.client.request("measure", text="", size=16, mono=True))
        literal = "A\\u0000B"
        self.assertGreater(
            self.client.request("measure", text=literal, size=16, mono=True)[0],
            batch[1][0],
        )

    def test_drawing_and_retained_text_match_the_full_string(self):
        split_stats, split_pixels = self.paint(
            [["begin", "#101218"], ["text", "café\0λ", 8, 12, "#ffffff", 28, True]])
        full_stats, full_pixels = self.paint(
            [["begin", "#101218"], ["text", "caféλ", 8, 12, "#ffffff", 28, True]])
        prefix_stats, prefix_pixels = self.paint(
            [["begin", "#101218"], ["text", "café", 8, 12, "#ffffff", 28, True]])
        self.assertEqual(split_pixels, full_pixels)
        self.assertNotEqual(full_pixels, prefix_pixels)
        self.assertEqual(split_stats["retained_scene_bytes"], full_stats["retained_scene_bytes"])
        self.assertNotEqual(full_stats["retained_scene_bytes"], prefix_stats["retained_scene_bytes"])

    def test_retained_patch_draws_the_suffix_and_a_rejected_id_does_not_apply(self):
        adapter = NativeHost()
        adapter.text("KEEP", 8, 12, "#ffffff", 28, True)
        segment = dict(id="label", bounds=[0, 0, 240, 48], commands=list(adapter._commands))
        self.client.request("patch", background="#101218", upsert=[segment], order=["label"])
        self.client.request("present")
        before = self.client.request("stats")
        kept = self.capture_only()
        sent = self.client.diagnostics["requests_sent"]
        with self.assertRaisesRegex(ValueError, _NUL):
            self.client.request("patch", upsert=[dict(
                id="A\0B", bounds=[0, 0, 240, 48],
                commands=[["text", "NO", 8, 12, "#ffffff", 28, True]])])
        self.assertEqual(self.client.diagnostics["requests_sent"], sent)
        self.assertEqual(self.client.request("stats")["scene_updates"], before["scene_updates"])
        self.assertEqual(self.capture_only(), kept)
        embedded = [["text", "A\0B", 8, 12, "#ffffff", 28, True]]
        segment["commands"] = embedded
        self.client.request("patch", upsert=[segment])
        self.assertEqual(embedded[0][1], "A\0B")
        self.client.request("present")
        updated_stats = self.client.request("stats")
        updated = self.capture_only()
        self.assertGreater(updated_stats["scene_updates"], before["scene_updates"])
        self.assertNotEqual(updated, kept)
        segment["commands"] = [["text", "AB", 8, 12, "#ffffff", 28, True]]
        self.client.request("patch", upsert=[segment])
        self.client.request("present")
        same = self.capture_only()
        self.assertEqual(updated, same)
        self.assertEqual(
            self.client.request("stats")["retained_scene_bytes"],
            updated_stats["retained_scene_bytes"],
        )
        segment["commands"] = [["text", "A", 8, 12, "#ffffff", 28, True]]
        self.client.request("patch", upsert=[segment])
        self.client.request("present")
        self.assertNotEqual(self.capture_only(), updated)

    def capture_only(self):
        self.client.request("capture", path=str(self.output))
        return bmp_pixels(self.output)

    def test_service_strings_are_rejected_before_the_host_sees_them(self):
        self.paint([["begin", "#101218"], ["text", "KEEP", 8, 12, "#ffffff", 28, True]])
        before = self.client.request("stats")
        for op, fields in (
            ("clipboard_write", {"text": "A\0B"}),
            ("capture", {"path": "A\0B.bmp"}),
            ("reload_image", {"source": "A\0B.png"}),
            ("frame", {"commands": [[
                "image", "A\0B.png", [0, 0, 8, 8], "#ffffff", "stretch"]]}),
        ):
            with self.subTest(op=op):
                sent = self.client.diagnostics["requests_sent"]
                with self.assertRaisesRegex(ValueError, _NUL):
                    self.client.request(op, **fields)
                self.assertEqual(self.client.diagnostics["requests_sent"], sent)
        after = self.client.request("stats")
        self.assertEqual(after["scene_updates"], before["scene_updates"])
        self.assertEqual(after["retained_scene_bytes"], before["retained_scene_bytes"])

    def test_raw_json_is_rejected_without_changing_the_retained_scene(self):
        stats, pixels = self.paint(
            [["begin", "#101218"], ["text", "KEEP", 8, 12, "#ffffff", 28, True]])
        for payload in (
            json.dumps({"op": "measure", "text": "A\0B", "size": 16, "mono": True}).encode(),
            json.dumps({"op": "clipboard_write", "text": "A\0B"}).encode(),
            json.dumps({"op": "set_title", "title": "A\0B"}).encode(),
            json.dumps({"op": "frame", "commands": [
                ["begin", "#101218"], ["text", "A\0Z", 8, 12, "#ffffff", 28, True]]}).encode(),
            b'{"op":"clipboard_write","text":"A\x00B"}',
        ):
            with self.subTest(payload=payload[:48]):
                with self.assertRaisesRegex(NativeHostError, _NUL):
                    self.client._request(9, payload, timeout=5)
        later = self.client.request("stats")
        self.assertEqual(later["scene_updates"], stats["scene_updates"])
        self.assertEqual(later["retained_scene_bytes"], stats["retained_scene_bytes"])
        self.assertEqual(self.capture_only(), pixels)
        self.assertTrue(self.client.is_alive)
        # The desktop clipboard is not read back. Rejection is the protocol error above.


@unittest.skipUnless(EXE.is_file(), "Build the standalone C host first")
class NativeTerminalEmbeddedNulTests(unittest.TestCase):
    def setUp(self):
        self.client = NativeClient(lambda event: None, executable=EXE)
        self.addCleanup(self.client.close)
        self.client.request(
            "open", backend="terminal", hidden=True, headless=True,
            columns=12, rows=3, color="truecolor", continuous=False,
        )

    def row(self, text):
        self.client.request("frame", commands=[
            ["begin", "#000000"], ["text", text, 0, 0, "#ffffff", 16, True]])
        return self.client.request("snapshot")["rows_text"][0]

    def test_cells_measurement_and_retained_bytes_keep_the_suffix(self):
        self.assertTrue(self.row("café\0λ").startswith("caféλ"))
        self.assertTrue(self.row("café").startswith("café "))
        self.assertEqual(self.row("A\0B")[:2], "AB")
        split = self.client.request("stats")["retained_scene_bytes"]
        self.assertEqual(self.row("AB")[:2], "AB")
        full = self.client.request("stats")["retained_scene_bytes"]
        self.assertEqual(self.row("A")[:2], "A ")
        self.assertEqual(split, full)
        self.assertNotEqual(full, self.client.request("stats")["retained_scene_bytes"])
        measured = self.client.request(
            "measure_many", texts=["café\0λ", "caféλ", "café"], size=16, mono=True)
        self.assertEqual(measured[0], measured[1])
        self.assertGreater(measured[1][0], measured[2][0])

    def test_retained_patch_updates_cells_and_raw_json_does_not(self):
        segment = dict(id="label", bounds=[0, 0, 96, 16],
                       commands=[["text", "KEEP", 0, 0, "#ffffff", 16, True]])
        self.client.request("patch", background="#000000", upsert=[segment], order=["label"])
        before = self.client.request("snapshot")
        before_stats = self.client.request("stats")
        sent = self.client.diagnostics["requests_sent"]
        with self.assertRaisesRegex(ValueError, _NUL):
            self.client.request("patch", upsert=[dict(
                id="A\0B", bounds=[0, 0, 96, 16],
                commands=[["text", "NO", 0, 0, "#ffffff", 16, True]])])
        self.assertEqual(self.client.diagnostics["requests_sent"], sent)
        self.assertEqual(self.client.request("snapshot"), before)
        segment["commands"] = [["text", "A\0B", 0, 0, "#ffffff", 16, True]]
        self.client.request("patch", upsert=[segment])
        updated = self.client.request("snapshot")
        self.assertTrue(updated["rows_text"][0].startswith("AB"))
        self.assertGreater(
            self.client.request("stats")["scene_updates"], before_stats["scene_updates"])
        segment["commands"] = [["text", "AB", 0, 0, "#ffffff", 16, True]]
        self.client.request("patch", upsert=[segment])
        self.assertEqual(self.client.request("snapshot"), updated)
        payload = json.dumps({"op": "frame", "commands": [
            ["begin", "#000000"], ["text", "A\0Z", 0, 0, "#ffffff", 16, True]]}).encode()
        with self.assertRaisesRegex(NativeHostError, _NUL):
            self.client._request(9, payload, timeout=5)
        self.assertEqual(self.client.request("snapshot"), updated)
        self.assertTrue(self.client.is_alive)


if __name__ == "__main__":
    unittest.main()
