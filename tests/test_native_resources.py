"""Application-relative resources remain independent of the helper's cwd."""

import os
from pathlib import Path
import unittest
from uuid import uuid4

from pysual import Rect, terminal
from pysual._png import encode_png
from pysual.backends._native_client import NativeHostError, native_executable
from pysual.backends.native import NativeHost
from pysual.image_resources import image_source
from test_native_window import bmp_pixels

ROOT = Path(__file__).resolve().parents[1]


class NativeResourceCommands(unittest.TestCase):
    def test_uri_sources_are_preserved_and_local_paths_are_rebased(self):
        host = NativeHost()
        sources = ("images/logo.png", "data:image/png;base64,AA==", "https://example.com/logo.png")
        for source in sources:
            with self.subTest(source=source):
                host.image(source, Rect(0, 0, 10, 10))
                host.image_nine(source, Rect(0, 0, 10, 10), (1, 1, 1, 1))
                host.sprite(source, Rect(0, 0, 10, 10), frame_width=1, frame_height=1,
                            frame_count=1)
                expected = os.path.abspath(source) if source == sources[0] else source
                self.assertEqual([command[1] for command in host._commands[-3:]], [expected] * 3)


@unittest.skipUnless(native_executable().is_file(), "Build the native helper")
class NativeResourceRendering(unittest.TestCase):
    def memory_host(self):
        host = NativeHost(hidden=True, vsync=False)
        self.addCleanup(host.close)
        host.open("Retained resource accounting", 160, 96, False, 1)
        return host

    def test_decoded_command_and_point_storage_matches_in_both_scene_forms(self):
        decoded_sizes = []
        for segmented in (False, True):
            with self.subTest(segmented=segmented):
                host = self.memory_host()

                def submit(commands):
                    if segmented:
                        host._request("patch", upsert=[dict(id="memory", bounds=[0, 0, 10, 10],
                                                           commands=commands)])
                    else:
                        host._request("frame", commands=commands)
                    return host.native_stats()["retained_scene_bytes"]

                single = submit([["clip", None]])
                # Replacing null with a four-number box adds four cJSON nodes,
                # without changing the decoded command or timestamp allocation.
                json_node = (submit([["clip", [0, 0, 10, 10]]]) - single) // 4
                self.assertGreater(json_node, 0)
                two = submit([["clip", None], ["clip", None]])
                # One extra command owns three JSON nodes, the "clip" string,
                # a double timestamp, and a decoded DrawCommand.
                decoded = two - single - 3 * json_node - len("clip\0") - 8
                self.assertGreater(decoded, 0)
                decoded_sizes.append(decoded)
                self.assertEqual(submit([["clip", None]]), single)

                points = [[0, 0], [1, 1]]
                before = submit([["lines", points, "#ffffff", 1]])
                after = submit([["lines", points + [[2, 2], [3, 3]], "#ffffff", 1]])
                # Every extra point owns its JSON array and two numeric nodes,
                # plus two doubles in the cached decoded point buffer.
                self.assertEqual(after - before, 6 * json_node + 4 * 8)
                self.assertEqual(submit([["lines", points, "#ffffff", 1]]), before)
                self.assertEqual(submit([["clip", None]]), single)
                if segmented:
                    host._request("patch", remove=["memory"], order=[])
                    self.assertEqual(host.native_stats()["retained_scene_bytes"], 0)
                    self.assertEqual(submit([["clip", None]]), single)
                host.close()
        self.assertEqual(decoded_sizes[0], decoded_sizes[1])

    def test_decoded_storage_budget_rejection_preserves_the_committed_scene(self):
        directory = ROOT / "work" / "resource-budget" / uuid4().hex
        directory.mkdir(parents=True)
        output = directory / "frame.bmp"
        self.addCleanup(directory.rmdir)
        self.addCleanup(output.unlink, missing_ok=True)
        original = ["rect", [10, 10, 20, 20], "#ff0000", 0, "", 0]
        # These compact commands fit the JSON-only budget and the 200,000
        # command limit. Their decoded arrays push the retained total past 64 MiB.
        oversized = [["clip", None]] * 150000
        for segmented in (False, True):
            with self.subTest(segmented=segmented):
                host = self.memory_host()
                if segmented:
                    host._request("patch", background="#000000", upsert=[
                        dict(id="original", bounds=[10, 10, 20, 20], commands=[original])
                    ], order=["original"])
                else:
                    host._request("frame", commands=[["begin", "#000000"], original])
                before = host.native_stats()
                with self.assertRaisesRegex(NativeHostError, "64 MiB memory budget"):
                    if segmented:
                        host._request("patch", remove=["original"], upsert=[
                            dict(id="oversized", bounds=[0, 0, 1, 1], commands=oversized)
                        ], order=["oversized"])
                    else:
                        host._request("frame", commands=oversized)
                after = host.native_stats()
                for key in ("scene_updates", "retained_scene_bytes", "command_count", "segment_count"):
                    self.assertEqual(after[key], before[key])
                host.capture(output)
                self.assertEqual(bmp_pixels(output)[15][15], (255, 0, 0))
                host.close()

    def test_relative_absolute_and_data_images_render_in_both_native_backends(self):
        directory = ROOT / "work" / "relative-images" / uuid4().hex
        directory.mkdir(parents=True)
        asset, output = directory / "red.png", directory / "frame.bmp"
        self.addCleanup(directory.rmdir)
        self.addCleanup(output.unlink, missing_ok=True)
        self.addCleanup(asset.unlink, missing_ok=True)
        data = encode_png(3, 3, bytes((255, 0, 0, 255)) * 9)
        asset.write_bytes(data)
        sources = (os.path.relpath(asset), str(asset), image_source(data))
        for backend in ("window", "terminal"):
            host = (NativeHost(hidden=True, vsync=False) if backend == "window"
                    else terminal(renderer="c", hidden=True, color="truecolor"))
            try:
                host.open("Relative image regression", 160, 96, False, 1)
                if "images" not in host.capabilities:
                    continue  # The SDL-free terminal build intentionally has no decoder.
                for source in sources:
                    for operation in ("image", "image_nine", "sprite"):
                        if operation == "sprite" and backend == "terminal":
                            continue
                        with self.subTest(backend=backend, operation=operation, relative=source == sources[0]):
                            host.begin("#123456")
                            if operation == "image":
                                host.image(source, Rect(0, 0, 32, 32))
                            elif operation == "image_nine":
                                host.image_nine(source, Rect(0, 0, 32, 32), (1, 1, 1, 1))
                            else:
                                host.sprite(source, Rect(0, 0, 32, 32), frame_width=3,
                                            frame_height=3, frame_count=1, loop=False)
                            host.present()
                            if backend == "window":
                                host.capture(output)
                                self.assertEqual(bmp_pixels(output)[8][8], (255, 0, 0))
                            else:
                                self.assertEqual(host.snapshot()["cells"][0]["foreground"], [255, 0, 0])
                            self.assertFalse([e for e in host.poll() if e.kind == "resource_error"])
            finally:
                host.close()
