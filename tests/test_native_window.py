"""Resource, clipping and retained-animation checks against the real C renderer.

The windows are hidden. These tests exercise SDL rendering and IPC; they do not
claim physical input or non-Windows platform qualification.
"""
import base64
import os
from pathlib import Path
import struct
import sys
import time
import unittest
from uuid import uuid4
import zlib

from pysual.backends._native_client import NativeClient, NativeHostError


PACKAGE = Path(__file__).resolve().parents[1] / "src" / "pysual"
EXE = Path(os.environ.get("PYSUAL_HOST", PACKAGE / "bin" /
                        ("pysual-host.exe" if sys.platform == "win32" else "pysual-host")))


def png_data(width, height, pixels):
    def chunk(kind, data):
        return (struct.pack(">I", len(data)) + kind + data +
                struct.pack(">I", zlib.crc32(kind + data)))
    raw = b"".join(b"\0" + pixels[y * width * 4:(y + 1) * width * 4]
                   for y in range(height))
    png = (b"\x89PNG\r\n\x1a\n" +
           chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) +
           chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def bmp_pixels(path):
    """Read the uncompressed 24/32-bit BMP variants SDL_SaveBMP emits."""
    data = path.read_bytes()
    if data[:2] != b"BM":
        raise AssertionError("Native capture is not a BMP")
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    depth = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if depth not in (24, 32) or compression not in (0, 3):
        raise AssertionError((depth, compression))
    stride = (width * depth + 31) // 32 * 4
    rows = []
    for y in range(abs(height)):
        row = abs(height) - 1 - y if height > 0 else y
        base = offset + row * stride
        rows.append(tuple(tuple(reversed(data[base + x * (depth // 8):
                                                base + x * (depth // 8) + 3]))
                          for x in range(width)))
    return rows


@unittest.skipUnless(EXE.is_file(), "Build the standalone C host first")
class NativeWindowTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.client = NativeClient(self.events.append, executable=EXE)
        self.addCleanup(self.client.close)
        # Ordinary inherited permissions let the separate native process access
        # captures even under Windows sandbox identities (mkdtemp uses 0700).
        self.directory = PACKAGE.parents[1] / "work" / "sdl-tests" / uuid4().hex
        self.directory.mkdir(parents=True)
        self.output = self.directory / "frame.bmp"
        self.addCleanup(self.cleanup_capture)
        self.info = self.client.request("open", backend="window", width=160,
            height=96, title="Pysual renderer regression", hidden=True,
            vsync=False, scale=1, resizable=False, font_dir=str(PACKAGE / "assets"))

    def submit(self, commands, transition=0):
        return self.client.request("frame", commands=commands,
                                   transition_seconds=transition)

    def cleanup_capture(self):
        self.output.unlink(missing_ok=True)
        self.directory.rmdir()

    def capture(self):
        info = self.client.request("capture", path=str(self.output))
        self.assertEqual((info["width"], info["height"]), (160, 96))
        return bmp_pixels(self.output)

    def test_disconnected_segments_match_lines_full_frame_and_retained(self):
        strokes = ((10, 12, 125, 74), (125, 12, 10, 74), (135, 10, 145, 80))
        points = [list(point) for x1, y1, x2, y2 in strokes
                  for point in ((x1, y1), (x2, y2))]
        for color in ("#ff8844", "#ff884480"):
            for width in (1, 2.5, 7):
                with self.subTest(color=color, width=width):
                    clip = ["clip", [15, 10, 110, 75]]
                    commands = [clip, *[["line", *stroke, color, width] for stroke in strokes]]
                    self.submit([["begin", "#123456"], *commands])
                    expected = self.capture()
                    batched = [clip, ["segments", points, color, width]]
                    self.submit([["begin", "#123456"], *batched])
                    self.assertEqual(self.capture(), expected)
                    self.patch([self.segment("strokes", [0, 0, 160, 96], batched)],
                               order=["strokes"], background="#123456")
                    self.assertEqual(self.capture(), expected)
        before = self.capture()
        for invalid in ([[1, 2]], [[1, 2], [3]], [[1, 2], [float("nan"), 4]]):
            with self.subTest(invalid=invalid), self.assertRaises((NativeHostError, ValueError)):
                self.submit([["begin", "#ffffff"], ["segments", invalid, "#ffffff", 2]])
        self.assertEqual(self.capture(), before)

    def test_capture_does_not_consume_pending_animation_presentation(self):
        animation = ["animated_rect", [0, 10, 10, 10], "#00ff00", 0, "", 0,
                     {"property": "x", "from": 0, "to": 100, "duration": .05,
                      "loop": False, "yoyo": False}]
        for retained in (False, True):
            for cache_scene in (False, True):
                with self.subTest(retained=retained, cache_scene=cache_scene):
                    self.client.request("configure", continuous=False, fps_limit=1,
                                        cache_scene=cache_scene)
                    self.submit([["begin", "#000000"]])
                    if retained:
                        self.patch([self.segment("animation", [0, 10, 110, 10], [animation])],
                                   order=["animation"], background="#000000")
                    else:
                        self.submit([["begin", "#000000"], animation])
                    # Endpoint is due before the next presentation at one second.
                    time.sleep(.1)
                    before = self.client.request("stats")
                    self.assertTrue(before["animating"])
                    pixels = self.capture()
                    self.assertEqual(pixels[15][5], (0, 0, 0))
                    self.assertEqual(pixels[15][105], (0, 255, 0))
                    captured = self.client.request("stats")
                    self.assertEqual(captured["frames"], before["frames"])
                    self.assertTrue(captured["animating"])
                    if retained:
                        self.assertEqual(captured["active_segment_count"], 0)
                    deadline = time.monotonic() + 3
                    while captured["animating"] and time.monotonic() < deadline:
                        time.sleep(.01)
                        captured = self.client.request("stats")
                    self.assertFalse(captured["animating"])
                    self.assertEqual(captured["frames"], before["frames"] + 1)
                    self.assertEqual(self.capture(), pixels)
                    self.assertFalse(self.client.request("stats")["animating"])

    def test_reduced_motion_presents_settled_frame_without_scene_resubmission(self):
        for retained in (False, True):
            for looping in (False, True):
                with self.subTest(retained=retained, looping=looping):
                    self.client.request("configure", continuous=False, fps_limit=1,
                                        reduce_motion=False)
                    animation = ["animated_rect", [0, 10, 10, 10], "#00ff00", 0, "", 0,
                                 {"property": "x", "from": 0, "to": 100, "duration": 10,
                                  "loop": looping, "yoyo": True}]
                    self.submit([["begin", "#000000"]])
                    if retained:
                        self.patch([self.segment("animation", [0, 10, 110, 10], [animation])],
                                   order=["animation"], background="#000000")
                    else:
                        self.submit([["begin", "#000000"], animation])
                    # Establish the frame deadline before changing only the policy.
                    self.client.request("present")
                    before = self.client.request("stats")
                    self.assertTrue(before["animating"])
                    self.client.request("configure", reduce_motion=True)
                    deadline = time.monotonic() + 3
                    settled = self.client.request("stats")
                    while settled["frames"] == before["frames"] and time.monotonic() < deadline:
                        time.sleep(.01)
                        settled = self.client.request("stats")
                    self.assertEqual(settled["frames"], before["frames"] + 1)
                    self.assertFalse(settled["animating"])
                    time.sleep(.1)
                    self.assertEqual(self.client.request("stats")["frames"], settled["frames"])
                    # A capture can itself draw the endpoint; only inspect it after
                    # proving that a settling presentation happened independently.
                    pixels = self.capture()
                    self.assertEqual(pixels[15][5], (0, 0, 0))
                    self.assertEqual(pixels[15][105], (0, 255, 0))

    @unittest.skipUnless(sys.platform == "win32", "Uses Windows window messages")
    def test_native_horizontal_and_vertical_wheel_directions(self):
        import ctypes
        from ctypes import wintypes

        user = ctypes.WinDLL("user32", use_last_error=True)
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        handles = []

        @enum_proc
        def visit(hwnd, _):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == self.client.process.pid:
                title = ctypes.create_unicode_buffer(128)
                user.GetWindowTextW(hwnd, title, len(title))
                if title.value == "Pysual renderer regression":
                    handles.append(hwnd)
            return True

        self.assertTrue(user.EnumWindows(visit, 0))
        self.assertEqual(len(handles), 1)
        # Target only this test's hidden child window; no global pointer input.
        for message, amount, expected, horizontal in (
            (0x020A, 120, -1, False), (0x020A, -120, 1, False),
            (0x020E, 120, 1, True), (0x020E, -120, -1, True),
        ):
            with self.subTest(message=message, amount=amount):
                self.events.clear()
                self.assertTrue(user.PostMessageW(handles[0], message,
                                                  (amount & 0xffff) << 16, (60 << 16) | 60))
                deadline = time.monotonic() + 2
                while not any(event.get("kind") == "wheel" for event in self.events):
                    self.assertLess(time.monotonic(), deadline, "Native wheel event not delivered")
                    time.sleep(.01)
                wheels = [event for event in self.events if event.get("kind") == "wheel"]
                self.assertEqual([(event["delta"], event["shift"]) for event in wheels],
                                 [(expected, horizontal)])

    def patch(self, upsert=(), remove=(), order=None, background=None, transition=0):
        payload = dict(upsert=list(upsert), remove=list(remove),
                       transition_seconds=transition)
        if order is not None:
            payload["order"] = order
        if background is not None:
            payload["background"] = background
        return self.client.request("patch", **payload)

    @staticmethod
    def segment(identifier, bounds, commands):
        return dict(id=identifier, bounds=bounds, commands=commands)

    def test_large_dirty_segment_replays_only_intersecting_segments(self):
        segments = [self.segment("large", [0, 0, 100, 60],
            [["rect", [0, 0, 100, 60], "#ff0000", 0, "", 0]])]
        for i in range(12):
            segments.append(self.segment(f"small-{i}", [130, i * 7, 20, 5],
                [["rect", [130, i * 7, 20, 5], "#00ff00", 0, "", 0]]))
        self.patch(segments, order=[s["id"] for s in segments], background="#123456")
        self.client.request("stats", reset=True)
        self.patch([self.segment("large", [0, 0, 100, 60],
            [["rect", [0, 0, 100, 60], "#0000ff", 0, "", 0]])])
        stats = self.client.request("stats")
        self.assertEqual(stats["last_commands_touched"], 1)
        self.assertLessEqual(stats["last_scene_segments_tested"], stats["segment_count"])
        self.assertEqual(stats["last_commands_replayed"], 1)
        self.assertEqual(stats["command_count"], 13)
        self.assertEqual(stats["segments_updated"], 1)
        pixels = self.capture()
        self.assertEqual(pixels[20][20], (0, 0, 255))
        self.assertEqual(pixels[2][140], (0, 255, 0))
        self.assertEqual(pixels[90][140], (18, 52, 86))
        self.client.request("stats", reset=True)
        self.patch([
            self.segment(f"small-{i}", [130, i * 7, 20, 5],
                [["rect", [130, i * 7, 20, 5], "#ffff00", 0, "", 0]])
            for i in range(4)
        ])
        many = self.client.request("stats")
        self.assertGreater(many["last_scene_segments_tested"], 0)
        self.assertLessEqual(many["last_scene_segments_tested"], many["segment_count"])

    def test_segment_removal_movement_insertion_and_reorder_preserve_overlaps(self):
        red = self.segment("red", [10, 10, 70, 50],
            [["rect", [10, 10, 70, 50], "#ff0000", 0, "", 0]])
        blue = self.segment("blue", [40, 20, 60, 50],
            [["rect", [40, 20, 60, 50], "#0000ff", 0, "", 0]])
        self.patch([red, blue], order=["red", "blue"], background="#000000")
        self.assertEqual(self.capture()[30][50], (0, 0, 255))
        self.patch(order=["blue", "red"])
        self.assertEqual(self.capture()[30][50], (255, 0, 0))
        self.patch(remove=["red"], order=["blue"])
        pixels = self.capture()
        self.assertEqual(pixels[30][20], (0, 0, 0))
        self.assertEqual(pixels[30][50], (0, 0, 255))
        self.patch([self.segment("blue", [110, 10, 40, 40],
            [["rect", [110, 10, 40, 40], "#0000ff", 0, "", 0]])])
        pixels = self.capture()
        self.assertEqual(pixels[30][50], (0, 0, 0))
        self.assertEqual(pixels[30][120], (0, 0, 255))
        self.patch([red], order=["blue", "red"])
        self.assertEqual(self.capture()[30][20], (255, 0, 0))
        self.assertEqual(self.client.request("stats")["segment_count"], 2)

    def test_clip_and_shadow_overrides_cannot_escape_partial_damage(self):
        left = self.segment("left", [10, 10, 50, 50],
            [["clip", [10, 10, 50, 50]], ["styled_rect", [20, 20, 20, 20],
              {"fill": "#ffffff", "shadow": "#ff0000", "shadow_blur": 6},
              [0, 0, 160, 96]]])
        right = self.segment("right", [100, 10, 40, 40],
            [["rect", [100, 10, 40, 40], "#00ff0080", 0, "", 0]])
        self.patch([left, right], order=["left", "right"], background="#000000")
        before = self.capture()
        self.assertGreater(before[25][110][1], 0)  # Left's clip does not leak.
        left["commands"][1][2]["fill"] = "#0000ff"
        self.patch([left])
        after = self.capture()
        self.assertEqual(after[25][110], before[25][110])
        self.assertEqual(after[25][25], (0, 0, 255))

    def test_effect_clip_omitted_null_and_rectangle_match_full_frame_after_patch(self):
        for operation in ("styled_rect", "marker"):
            for clip_mode in ("omitted", "null", "rectangle"):
                with self.subTest(operation=operation, effect_clip=clip_mode):
                    self.submit([["begin", "#000000"]])
                    style = {"fill": "#ffffff", "shadow": "#ff0000", "shadow_blur": 8}
                    command = [operation, [40, 30, 40, 30], style]
                    if operation == "marker":
                        command += ["square", False]
                    if clip_mode != "omitted":
                        command.append(None if clip_mode == "null" else [30, 20, 60, 50])
                    commands = [["clip", [40, 30, 40, 30]], command]
                    segment = self.segment("effect", [40, 30, 40, 30], commands)
                    self.patch([segment], order=["effect"], background="#000000")
                    style["shadow"] = "#0000ff"
                    self.patch([segment])
                    patched = self.capture()
                    if clip_mode == "omitted":
                        self.assertEqual(patched[44][37], (0, 0, 0))
                    else:
                        self.assertEqual(patched[44][37][:2], (0, 0))
                        self.assertGreater(patched[44][37][2], 0)
                    self.patch(remove=["effect"], order=[])
                    self.assertTrue(all(pixel == (0, 0, 0)
                                        for row in self.capture() for pixel in row))
                    self.submit([["begin", "#000000"], *commands])
                    self.assertEqual(self.capture(), patched)

    def test_invalid_patch_is_atomic_and_full_frame_resets_segments(self):
        original = self.segment("original", [10, 10, 30, 30],
            [["rect", [10, 10, 30, 30], "#ff0000", 0, "", 0]])
        self.patch([original], order=["original"], background="#000000")
        before = self.capture()
        updates = self.client.request("stats")["scene_updates"]
        edits = [dict(upsert=[self.segment("new", [0, 0, 30, 30], [["unknown"]])]),
                 dict(upsert=[original], order=["missing"]),
                 dict(upsert=[original, original]),
                 dict(remove=["original", "original"]),
                 dict(upsert=[self.segment("new", [0, 0, 30, 30], [["begin", "#fff"]])])]
        for edit in edits:
            with self.subTest(edit=edit), self.assertRaises(NativeHostError):
                self.client.request("patch", **edit)
        self.assertEqual(self.client.request("stats")["scene_updates"], updates)
        self.assertEqual(self.capture(), before)
        self.submit([["begin", "#123456"]])
        self.assertEqual(self.client.request("stats")["segment_count"], 0)
        self.assertEqual(self.capture()[25][25], (18, 52, 86))

    def test_transition_snapshots_only_changed_segment_and_stays_bounded(self):
        segments = [self.segment(str(i), [i % 16 * 10, i // 16 * 10, 8, 8],
            [["styled_rect", [i % 16 * 10, i // 16 * 10, 8, 8], {"fill": "#ff0000"}]])
                    for i in range(96)]
        self.patch(segments, order=[s["id"] for s in segments], background="#000000")
        self.client.request("stats", reset=True)
        segments[0]["commands"][0][2]["fill"] = "#0000ff"
        self.patch([segments[0]], transition=.5)
        stats = self.client.request("stats")
        self.assertEqual(stats["transition_snapshot_commands"], 1)
        self.assertEqual(stats["commands_touched"], 1)
        self.assertEqual(stats["segment_count"], 96)
        time.sleep(.04)
        segments[0]["commands"][0][2]["fill"] = "#00ff00"
        self.patch([segments[0]], transition=.1)
        self.assertEqual(self.client.request("stats")["transition_snapshot_commands"], 1)
        time.sleep(.15)
        self.assertEqual(self.capture()[4][4], (0, 255, 0))
        self.assertEqual(self.client.request("stats")["transition_snapshot_commands"], 0)

    def test_fractional_damage_keeps_neighbor_covering_the_same_pixel(self):
        left = self.segment("left", [5.2, 5.2, 9.1, 20.1],
            [["clip", [5.2, 5.2, 9.1, 20.1]],
             ["rect", [0, 0, 160, 96], "#ff0000", 0, "", 0]])
        right = self.segment("right", [15.4, 5.2, 10.1, 20.1],
            [["clip", [15.4, 5.2, 10.1, 20.1]],
             ["rect", [0, 0, 160, 96], "#00ff0080", 0, "", 0]])
        for scale in (1, 2):
            with self.subTest(scale=scale):
                self.client.request("configure", scale=scale)
                self.submit([["begin", "#000000"]])
                self.patch([left, right], order=["left", "right"], background="#000000")
                left["commands"][1][2] = "#0000ff"
                self.patch([left])
                patched = self.capture()
                self.submit([["begin", "#000000"]] + left["commands"] +
                            [["clip", None]] + right["commands"])
                self.assertEqual(self.capture(), patched)
                left["commands"][1][2] = "#ff0000"

    def test_native_animation_expands_bounds_and_finishes_without_a_trail(self):
        animation = self.segment("animation", [0, 10, 10, 10],
            [["animated_rect", [0, 10, 10, 10], "#00ff00", 0, "", 0,
              {"property": "x", "from": 0, "to": 100, "duration": .08}]])
        self.patch([animation], order=["animation"], background="#000000")
        time.sleep(.14)
        pixels = self.capture()
        self.assertEqual(pixels[15][5], (0, 0, 0))
        self.assertEqual(pixels[15][105], (0, 255, 0))
        self.assertFalse(self.client.request("stats")["animating"])

    def test_offscreen_finite_playback_expires_and_reveals_final_state_after_resize(self):
        source = png_data(2, 1, bytes((255, 0, 0, 255, 0, 0, 255, 255)))
        cases = (
            ("shape", ["animated_rect", [200, 20, 20, 20], "#ff0000", 0, "", 0,
                       {"property": "opacity", "from": 0, "to": 1, "duration": .04}],
             (255, 0, 0)),
            ("sprite", ["sprite", source, [200, 20, 20, 20], 1, 1, 2, 50, False, "#ffffff"],
             (0, 0, 255)),
            ("transition", ["styled_rect", [200, 20, 20, 20], {"fill": "#ff0000"}],
             (0, 0, 255)),
        )
        for kind, command, final_color in cases:
            with self.subTest(kind=kind):
                self.client.request("set_size", width=160, height=96)
                self.submit([["begin", "#000000"]])
                segment = self.segment("offscreen", [200, 20, 20, 20], [command])
                self.patch([segment], order=["offscreen"], background="#000000")
                initial_bytes = self.client.request("stats")["retained_scene_bytes"]
                if kind == "transition":
                    command[2]["fill"] = "#0000ff"
                    self.patch([segment], transition=.04)
                deadline = time.monotonic() + 2
                while True:
                    settled = self.client.request("stats")
                    if not settled["animating"] or time.monotonic() >= deadline:
                        break
                    time.sleep(.01)
                self.assertFalse(settled["animating"])
                self.assertEqual(settled["active_segment_count"], 0)
                self.assertEqual(settled["transition_snapshot_commands"], 0)
                self.assertEqual(settled["retained_scene_bytes"], initial_bytes)
                # With no explicit present/capture, a completed offscreen scene
                # must return to idle rather than continually copying its cache.
                self.client.request("stats", reset=True)
                time.sleep(.05)
                idle = self.client.request("stats")
                self.assertEqual(idle["frames"], 0)
                self.assertEqual(idle["segments_tested"], 0)
                self.assertEqual(idle["animation_segments_tested"], 0)
                self.client.request("set_size", width=240, height=96)
                self.client.request("capture", path=str(self.output))
                pixels = bmp_pixels(self.output)
                self.assertEqual(len(pixels[0]), 240)
                self.assertEqual(pixels[30][210], final_color)

    def test_unrelated_patch_preserves_finite_sprite_epoch(self):
        source = png_data(2, 1, bytes((255, 0, 0, 255, 0, 0, 255, 255)))
        sprite = self.segment("sprite", [20, 20, 40, 40],
            [["sprite", source, [20, 20, 40, 40], 1, 1, 2, 10, False, "#ffffff"]])
        other = self.segment("other", [100, 10, 30, 30],
            [["rect", [100, 10, 30, 30], "#00ff00", 0, "", 0]])
        self.patch([sprite, other], order=["sprite", "other"], background="#000000")
        time.sleep(.25)
        self.assertEqual(self.capture()[40][40], (0, 0, 255))
        other["commands"][0][2] = "#ffffff"
        self.patch([other])
        self.assertEqual(self.capture()[40][40], (0, 0, 255))
        # Re-emitting an identical sprite segment also retains its epoch.
        self.patch([sprite])
        self.assertEqual(self.capture()[40][40], (0, 0, 255))

    def test_settled_transition_releases_old_bounds_and_snapshot_memory(self):
        moving = self.segment("moving", [0, 10, 10, 10],
            [["styled_rect", [0, 10, 10, 10], {"fill": "#ff0000"}]])
        middle = self.segment("middle", [70, 10, 10, 10],
            [["rect", [70, 10, 10, 10], "#0000ff", 0, "", 0]])
        self.patch([moving, middle], order=["moving", "middle"], background="#000000")
        initial = self.client.request("stats")["retained_scene_bytes"]
        for x in (130, 0):
            moving["bounds"] = [x, 10, 10, 10]
            moving["commands"][0][1] = [x, 10, 10, 10]
            self.patch([moving], transition=.08)
            active = self.client.request("stats")
            self.assertGreater(active["retained_scene_bytes"], initial)
            self.assertEqual(active["retained_scene_budget"], 64 * 1024 * 1024)
            time.sleep(.13)
            self.capture()
            self.assertEqual(self.client.request("stats")["retained_scene_bytes"], initial)
        moving["commands"][0][2]["fill"] = "#00ff00"
        self.patch([moving])
        self.assertEqual(self.client.request("stats")["last_scene_commands_replayed"], 1)
        pixels = self.capture()
        self.assertEqual(pixels[15][5], (0, 255, 0))
        self.assertEqual(pixels[15][135], (0, 0, 0))
        self.assertEqual(pixels[15][75], (0, 0, 255))
        self.patch(remove=["moving", "middle"], order=[])
        self.assertEqual(self.client.request("stats")["retained_scene_bytes"], 0)

    def test_retained_memory_budget_rejects_oversized_patch_atomically(self):
        original = self.segment("original", [10, 10, 20, 20],
            [["rect", [10, 10, 20, 20], "#ff0000", 0, "", 0]])
        self.patch([original], order=["original"], background="#000000")
        before = self.client.request("stats")
        # Fits the transport payload and command-count limit, but its JSON
        # objects exceed the retained heap budget on the supported 64-bit host.
        command = ["rect", [0, 0, 1, 1], "#ffffff", 0, "", 0]
        oversized = self.segment("oversized", [0, 0, 1, 1], [command] * 120000)
        with self.assertRaisesRegex(NativeHostError, "64 MiB memory budget"):
            self.patch([oversized])
        after = self.client.request("stats")
        self.assertEqual(after["scene_updates"], before["scene_updates"])
        self.assertEqual(after["retained_scene_bytes"], before["retained_scene_bytes"])
        self.assertEqual(after["segment_count"], 1)
        self.assertEqual(self.capture()[15][15], (255, 0, 0))

    def test_uncapped_static_cache_does_not_scan_extreme_scene_segments(self):
        grid = self.segment("grid", [0, 0, 160, 96], [["clip", [0, 0, 160, 96]]])
        cells = [self.segment(str(i), [i % 100 * 1.5, i // 100 * 1.5, 1, 1],
            [["rect", [i % 100 * 1.5, i // 100 * 1.5, 1, 1], "#00ff00", 0, "", 0]])
                 for i in range(5000)]
        self.patch([grid] + cells, order=["grid"] + [s["id"] for s in cells], background="#000000")
        self.client.request("configure", continuous=True, vsync=False, fps_limit=None)
        self.client.request("stats", reset=True)
        time.sleep(.04)
        idle = self.client.request("stats")
        self.assertGreater(idle["frames"], 0)
        self.assertEqual(idle["segments_tested"], 0)
        self.assertEqual(idle["animation_segments_tested"], 0)
        self.assertEqual(idle["active_segment_count"], 0)
        self.patch([grid])
        identical = self.client.request("stats")
        self.assertEqual(identical["last_scene_commands_replayed"], 0)
        self.assertEqual(identical["segments_updated"], 0)
        self.assertEqual(identical["unchanged_segments"], 1)
        # A clip change still paints no pixels and cannot damage the full grid.
        grid["commands"][0][1][2] = 159
        self.patch([grid])
        clip_edit = self.client.request("stats")
        self.assertEqual(clip_edit["last_scene_commands_replayed"], 0)
        self.assertEqual(clip_edit["segments_tested"], 0)

    def test_large_supplied_bounds_do_not_expand_a_small_native_footprint(self):
        controls = [self.segment(str(i), [0, 0, 160, 96],
            [["rect", [i * 12, 20, 8, 8], "#ff0000", 0, "", 0]]) for i in range(12)]
        self.patch(controls, order=[s["id"] for s in controls], background="#000000")
        self.client.request("stats", reset=True)
        controls[0]["commands"][0][2] = "#00ff00"
        self.patch([controls[0]])
        self.assertEqual(self.client.request("stats")["last_scene_commands_replayed"], 1)
        pixels = self.capture()
        self.assertEqual(pixels[24][4], (0, 255, 0))
        self.assertEqual(pixels[24][16], (255, 0, 0))

    def test_identical_upsert_keeps_a_transition_running_from_its_original_epoch(self):
        control = self.segment("control", [10, 10, 40, 40],
            [["styled_rect", [10, 10, 40, 40], {"fill": "#ff0000"}]])
        self.patch([control], order=["control"], background="#000000")
        control["commands"][0][2]["fill"] = "#0000ff"
        self.patch([control], transition=.2)
        time.sleep(.06)
        self.patch([control], transition=.2)
        time.sleep(.17)
        self.assertEqual(self.capture()[25][25], (0, 0, 255))
        stats = self.client.request("stats")
        self.assertEqual(stats["transition_snapshot_commands"], 0)
        self.assertEqual(stats["active_segment_count"], 0)

    def test_resize_reveals_unclipped_retained_commands_without_resubmission(self):
        outside = self.segment("outside", [200, 20, 40, 40],
            [["rect", [200, 20, 40, 40], "#ff0000", 0, "", 0]])
        self.patch([outside], order=["outside"], background="#000000")
        before = self.client.request("stats")["scene_updates"]
        self.client.request("set_size", width=320, height=96)
        self.client.request("capture", path=str(self.output))
        pixels = bmp_pixels(self.output)
        self.assertEqual(len(pixels[0]), 320)
        self.assertEqual(pixels[30][210], (255, 0, 0))
        self.assertEqual(self.client.request("stats")["scene_updates"], before)

    def test_removing_an_animated_segment_unlinks_it_from_native_schedule(self):
        animation = self.segment("animation", [20, 20, 40, 40],
            [["animated_rect", [20, 20, 40, 40], "#ff0000", 0, "", 0,
              {"property": "opacity", "from": 0, "to": 1, "duration": .1, "loop": True}]])
        self.patch([animation], order=["animation"], background="#000000")
        self.assertEqual(self.client.request("stats")["active_segment_count"], 1)
        self.patch(remove=["animation"], order=[])
        stats = self.client.request("stats")
        self.assertEqual(stats["active_segment_count"], 0)
        self.assertFalse(stats["animating"])
        self.assertEqual(self.capture()[30][30], (0, 0, 0))

    def test_bad_edit_preserves_presented_pixels_and_scene_counter(self):
        self.submit([["begin", "#123456"],
                     ["rect", [20, 20, 50, 40], "#df8142", 5, "#ffffff", 2]])
        before = self.capture()
        updates = self.client.request("stats")["scene_updates"]
        invalid = [
            [["begin", "#ffffff"], ["unknown"]],
            [["rect", [0, 0, -1, 20], "#fff", 0, "", 0]],
            [["styled_rect", [0, 0, 10, 10], {"shadow_blur": 25}]],
            [["styled_rect", [0, 0, 10, 10], {"pattern_spacing": 1}]],
            [["styled_rect", [0, 0, 10, 10], {"foreground": "#zzzzzz"}]],
            [["styled_rect", [0, 0, 10, 10], {"unrecognized": 1}]],
        ]
        for commands in invalid:
            with self.subTest(commands=commands), self.assertRaises(NativeHostError):
                self.submit(commands)
        self.assertEqual(self.client.request("stats")["scene_updates"], updates)
        self.assertEqual(self.capture(), before)

    def test_fractional_clip_and_translucent_border_have_disjoint_corners(self):
        self.submit([["begin", "#000000"], ["clip", [10.2, 10.2, 5.1, 5.1]],
                     ["rect", [0, 0, 160, 96], "#ff0000", 0, "", 0],
                     ["clip", None],
                     ["rect", [40, 20, 40, 30], "", 0, "#ff000080", 4]])
        pixels = self.capture()
        self.assertEqual(pixels[10][10], (255, 0, 0))
        self.assertEqual(pixels[15][15], (255, 0, 0))
        self.assertEqual(pixels[9][10], (0, 0, 0))
        self.assertEqual(pixels[16][15], (0, 0, 0))
        self.assertEqual(pixels[20][40], pixels[20][50])
        self.assertEqual(pixels[20][40], pixels[30][40])
        self.assertIn(pixels[20][40][0], (127, 128))
        self.assertEqual(pixels[30][50], (0, 0, 0))

    def test_shadow_uses_ancestor_effect_clip_outside_control_body(self):
        self.submit([["begin", "#000000"], ["clip", [40, 30, 40, 30]],
                     ["styled_rect", [40, 30, 40, 30],
                      {"fill": "#ffffff", "shadow": "#ff0000", "shadow_blur": 8},
                      [0, 0, 160, 96]]])
        pixels = self.capture()
        self.assertGreater(pixels[44][39][0], 0)
        self.assertEqual(pixels[44][39][1:], (0, 0))
        self.assertEqual(pixels[44][50], (255, 255, 255))
        self.assertEqual(pixels[44][20], (0, 0, 0))

    def test_missing_image_reports_once_and_keeps_frame_usable(self):
        commands = [["begin", "#123456"],
                    ["image", str(self.directory / "missing.png"),
                     [0, 0, 100, 80], "#ffffff", "contain"],
                    ["image", "data:image/png;base64,INVALID!", [0, 0, 20, 20],
                     "#ffffff", "stretch"]]
        for _ in range(3):
            self.submit(commands)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            errors = [event for event in self.events if event["kind"] == "resource_error"]
            if len(errors) == 2:
                break
            time.sleep(.01)
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("image unavailable" in event["text"] for event in errors))
        self.assertEqual(self.capture()[20][20], (18, 52, 86))
        self.assertTrue(self.client.is_alive)

    def test_font_texture_cache_is_bounded_and_survives_eviction(self):
        commands = [["begin", "#000000"]] + [
            ["text", f"entry {i:04d}", 0, 0, "#ffffff", 10, False]
            for i in range(600)
        ]
        self.submit(commands)
        stats = self.client.request("stats")
        self.assertLessEqual(stats["texture_entries"], 512)
        self.assertLessEqual(stats["texture_bytes"], 8 * 1024 * 1024)
        self.submit([["begin", "#000000"], commands[1], commands[-1]])
        self.assertTrue(any(pixel != (0, 0, 0) for row in self.capture() for pixel in row))

    def test_finite_sprite_keeps_its_epoch_after_unrelated_scene_edit(self):
        source = png_data(2, 1, bytes((255, 0, 0, 255, 0, 0, 255, 255)))
        commands = [["begin", "#000000"],
                    ["sprite", source, [20, 20, 40, 40], 1, 1, 2, 10, False, "#ffffff"]]
        self.submit(commands)
        time.sleep(.25)
        self.assertEqual(self.capture()[40][40], (0, 0, 255))
        self.submit(commands + [["rect", [100, 0, 10, 10], "#00ff00", 0, "", 0]])
        self.assertEqual(self.capture()[40][40], (0, 0, 255))
        self.assertFalse(self.client.request("stats")["animating"])

    def test_finite_shape_epoch_and_reduced_motion_final_value(self):
        animation = ["animated_rect", [0, 20, 20, 20], "#ff0000", 0, "", 0,
                     {"property": "x", "from": 0, "to": 100, "duration": .1,
                      "loop": False, "yoyo": False}]
        commands = [["begin", "#000000"], animation]
        self.submit(commands)
        time.sleep(.15)
        self.submit(commands + [["rect", [150, 0, 5, 5], "#ffffff", 0, "", 0]])
        self.assertEqual(self.capture()[25][105], (255, 0, 0))
        self.assertFalse(self.client.request("stats")["animating"])
        animation[-1]["duration"] = 10
        animation[-1]["loop"] = True
        self.submit(commands)
        self.client.request("configure", reduce_motion=True)
        self.assertEqual(self.capture()[25][105], (255, 0, 0))
        self.assertFalse(self.client.request("stats")["animating"])

    def test_vsync_readback_and_unicode_font_metrics(self):
        self.assertEqual(self.info["vsync"], 0)
        configured = self.client.request("configure", vsync=False)
        self.assertEqual(configured["vsync"], 0)
        one = self.client.request("measure", text="café λ 中", size=16, mono=False)
        two = self.client.request("measure", text="café λ 中\ncafé λ 中", size=16, mono=False)
        self.assertEqual(two, [one[0], one[1] * 2])
        self.assertGreater(one[0], 0)

    def test_foreground_only_transition_interpolates_text_and_icon_lines(self):
        def scene(color):
            return [["begin", "#000000"],
                    ["text", "MMMM", 10, 10, color, 16, False],
                    ["lines", [[10, 50], [100, 50]], color, 4],
                    ["segments", [[10, 75], [40, 75], [80, 75], [100, 75]], color, 4],
                    ["rect", [120, 10, 20, 20], color, 3, "", 0]]
        self.submit(scene("#ff0000"))
        self.submit(scene("#0000ff"), transition=.5)
        time.sleep(.08)
        pixels = self.capture()
        red, _, blue = pixels[50][30]
        self.assertGreater(red, 0)
        self.assertGreater(blue, 0)
        self.assertLess(red, 255)
        self.assertLess(blue, 255)
        self.assertEqual(pixels[75][25], pixels[50][30])
        self.assertEqual(pixels[75][60], (0, 0, 0))
        self.assertGreater(max(pixel[0] for row in pixels[10:30]
                               for pixel in row[10:70]), 0)
        self.assertGreater(max(pixel[2] for row in pixels[10:30]
                               for pixel in row[10:70]), 0)
        time.sleep(.5)
        self.assertEqual(self.capture()[50][30], (0, 0, 255))
        self.assertFalse(self.client.request("stats")["animating"])

    def test_scene_cache_matches_command_replay_and_invalidates_on_edits_and_resize(self):
        commands = [["begin", "#18212a"],
                    ["styled_rect", [10, 10, 130, 72],
                     {"fill": "#245878aa", "fill_end": "#38557755",
                      "radius": 9, "border": "#aaccee80", "border_width": 1,
                      "shadow": "#00000099", "shadow_blur": 5}, None],
                    ["text", "Cache café", 20, 30, "#ffffff", 16, False],
                    ["lines", [[20, 65], [40, 45], [60, 62]], "#ffaa77a0", 2]]
        self.client.request("configure", cache_scene=False)
        self.submit(commands)
        direct = self.capture()
        self.assertEqual(self.client.request("stats")["scene_cache_bytes"], 0)
        self.client.request("configure", cache_scene=True)
        self.assertEqual(self.capture(), direct)
        before_hits = self.client.request("stats")["scene_cache_hits"]
        self.assertEqual(self.capture(), direct)
        stats = self.client.request("stats")
        self.assertGreater(stats["scene_cache_hits"], before_hits)
        self.assertEqual(stats["scene_cache_bytes"], 160 * 96 * 4)
        self.assertLessEqual(stats["scene_cache_bytes"], 32 * 1024 * 1024)
        self.submit([["begin", "#aabbcc"]])
        self.assertEqual(self.capture()[0][0], (170, 187, 204))
        self.client.request("set_size", width=180, height=100)
        self.submit([["begin", "#123456"]])
        capture = self.client.request("capture", path=str(self.output))
        self.assertEqual((capture["width"], capture["height"]), (180, 100))
        self.assertEqual(bmp_pixels(self.output)[99][179], (18, 52, 86))
        self.assertEqual(self.client.request("stats")["scene_cache_bytes"], 180 * 100 * 4)
        self.client.request("configure", cache_scene=False)
        self.assertEqual(self.client.request("stats")["scene_cache_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
