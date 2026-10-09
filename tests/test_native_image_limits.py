"""Native image rejection happens before invoking the pixel decoder."""

import base64
from pathlib import Path
import struct
import time
import unittest
from uuid import uuid4
import zlib

from pysual._png import encode_png
from pysual.backends._native_client import NativeClient, native_executable
from test_native_window import bmp_pixels

ROOT = Path(__file__).resolve().parents[1]


def solid_png(width, height):
    """Compress one row at a time; the test never allocates the decoded image."""
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff))

    compressor = zlib.compressobj()
    row = b"\0" + b"\xff\0\0\xff" * width
    data = b"".join(compressor.compress(row) for _ in range(height)) + compressor.flush()
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", data) + chunk(b"IEND", b""))


@unittest.skipUnless(native_executable().is_file(), "Build the full native helper")
class NativeImageLimits(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "work" / "native-image-limits" / uuid4().hex
        self.directory.mkdir(parents=True)
        self.addCleanup(self.directory.rmdir)
        self.events = []
        self.client = NativeClient(self.events.append)
        self.addCleanup(self.client.close)
        self.client.request("open", backend="window", hidden=True, width=160, height=96,
                            scale=1, vsync=False, font_dir=str(ROOT / "src/pysual/assets"))

    def file(self, name, data):
        path = self.directory / name
        path.write_bytes(data)
        self.addCleanup(path.unlink, missing_ok=True)
        return str(path)

    def sources(self, data):
        return (self.file(uuid4().hex + ".png", data),
                "data:image/png;base64," + base64.b64encode(data).decode("ascii"))

    def draw(self, source):
        self.client.request("frame", commands=[["begin", "#123456"],
                            ["image", source, [0, 0, 32, 32], "#fff", "stretch"]])
        self.client.request("present")
        return self.client.request("stats")

    def rejected(self, source, message):
        self.events.clear()
        before = self.client.request("stats")["image_decode_attempts"]
        stats = self.draw(source)
        self.assertEqual(stats["image_decode_attempts"], before)
        self.assertEqual(stats["texture_entries"], 0)
        deadline = time.monotonic() + 2
        while not self.events and time.monotonic() < deadline:
            time.sleep(.005)
        errors = [event["text"] for event in self.events if event.get("kind") == "resource_error"]
        self.assertTrue(any(message in text for text in errors), errors)
        self.assertTrue(self.client.is_alive)

    def test_oversized_pixel_dimensions_are_rejected_before_decode(self):
        # A valid 64 MiB RGBA image compresses to less than 100 KiB. Neither
        # the test nor the helper should allocate that decompressed surface.
        data = solid_png(4096, 4096)
        self.assertLess(len(data), 100_000)
        for source in self.sources(data):
            with self.subTest(data_uri=source.startswith("data:")):
                self.rejected(source, "PNG dimensions exceed")

    def test_encoded_file_and_data_uri_limits_precede_decode(self):
        data = encode_png(1, 1, b"\xff\0\0\xff")
        data += bytes(8 * 1024 * 1024 + 1 - len(data))
        for source in self.sources(data):
            with self.subTest(data_uri=source.startswith("data:")):
                self.rejected(source, "encoded input exceeds 8 MiB")

    def test_other_formats_are_rejected_by_content_and_png_extensions_are_not_required(self):
        # SDL_image can decode GIF, but the bounded native contract accepts PNG.
        gif = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
        for source in self.sources(gif):
            with self.subTest(data_uri=source.startswith("data:")):
                self.rejected(source, "require PNG data")
        png = encode_png(1, 1, b"\xff\0\0\xff")
        source = self.file("valid-image.dat", png)
        stats = self.draw(source)
        self.assertEqual(stats["image_decode_attempts"], 1)
        self.assertEqual(stats["texture_entries"], 1)
        output = self.directory / "capture.bmp"
        self.addCleanup(output.unlink, missing_ok=True)
        self.client.request("capture", path=str(output))
        self.assertEqual(bmp_pixels(output)[8][8], (255, 0, 0))
        self.client.request("stats", reset=True)
        self.assertEqual(self.draw(source)["image_decode_attempts"], 0)

    def test_malformed_bounded_png_is_recoverable(self):
        png = encode_png(1, 1, b"\xff\0\0\xff")
        stats = self.draw(self.file("truncated.png", png[:33]))
        self.assertEqual(stats["image_decode_attempts"], 1)
        self.assertEqual(stats["texture_entries"], 0)
        stats = self.draw(self.file("valid.png", png))
        self.assertEqual(stats["image_decode_attempts"], 2)
        self.assertEqual(stats["texture_entries"], 1)
