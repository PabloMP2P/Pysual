"""Actual C process checks; never substitutes a Python renderer."""
import json
import os
from pathlib import Path
import sys
import time
import unittest
from uuid import uuid4

from pysual._png import decode_png, encode_png
from pysual.backends._native_client import NativeClient, NativeHostError
from pysual.backends.native import NativeHost

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "pysual"
EXE = Path(os.environ.get("PYSUAL_HOST", PACKAGE / "bin" / ("pysual-host.exe" if sys.platform == "win32" else "pysual-host")))


@unittest.skipUnless(EXE.is_file(), "Build the standalone C host first")
class NativeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.client = NativeClient(self.events.append)
        self.info = self.client.request("open", backend="window", width=320, height=160,
            title="Pysual protocol test", hidden=True, vsync=False, scale=1,
            resizable=False, font_dir=str(PACKAGE / "assets"))

    def tearDown(self):
        self.client.close()
        self.assertIsNotNone(self.client.process.poll())

    def scene(self):
        return [["begin", "#20242d"], ["rect", [10, 10, 100, 40], "#52d9a1", 8, "#ffffff", 1],
                ["text", "Hello café λ", 12, 60, "#ffffff", 16, False]]

    def test_clipboard_limits_keep_connection_alive_without_os_clipboard_access(self):
        # The C service rejects plain text beyond 8 MiB before SDL_SetClipboardText.
        with self.assertRaisesRegex(NativeHostError, "Clipboard text exceeds 8 MiB"):
            self.client.request("clipboard_write", text="x" * (8 * 1024 * 1024 + 1))
        self.assertIsInstance(self.client.request("stats"), dict)
        # Escaped controls can exceed the wire cap below the UTF-8 service cap.
        before = self.client.diagnostics["requests_sent"]
        with self.assertRaisesRegex(ValueError, "16 MiB protocol limit"):
            self.client.request("clipboard_write", text="\x01" * (3 * 1024 * 1024))
        self.assertEqual(self.client.diagnostics["requests_sent"], before)
        self.assertIsInstance(self.client.request("stats"), dict)
        self.assertTrue(self.client.is_alive)

    def test_idle_scene_and_continuous_replay_have_no_frame_messages(self):
        self.client.request("frame", commands=self.scene())
        self.client.request("configure", continuous=True, fps_limit=None, vsync=False)
        time.sleep(.08)  # Drain initial OS events before the audited interval.
        self.client.request("stats", reset=True)
        counts = self.client.diagnostics
        old_interval = sys.getswitchinterval()
        try:
            sys.setswitchinterval(1)
            deadline = time.perf_counter() + .3
            while time.perf_counter() < deadline:
                pass
            after = self.client.diagnostics
        finally:
            sys.setswitchinterval(old_interval)
        for key in ("requests_sent", "replies_received", "bytes_sent", "bytes_received"):
            self.assertEqual(after[key], counts[key], key)
        stats = self.client.request("stats")
        self.assertGreater(stats["frames"], 5)
        self.assertEqual(stats["scene_updates"], 0)

    def test_rejected_scene_preserves_previous_scene(self):
        self.client.request("frame", commands=self.scene())
        before = self.client.request("stats")
        with self.assertRaises(NativeHostError):
            self.client.request("frame", commands=[["rect", [0, 0, 5, 5], "#fff"], ["unknown"]])
        after = self.client.request("stats")
        self.assertEqual(after["scene_updates"], before["scene_updates"])
        self.client.request("frame", commands=self.scene())

    def test_scheduled_changes_and_replays_share_the_frame_cap(self):
        for fps in (60, 120):
            for continuous in (False, True):
                with self.subTest(fps=fps, continuous=continuous):
                    self.client.request("configure", continuous=continuous,
                                        redraw_interval=0 if continuous else None,
                                        fps_limit=fps, vsync=False)
                    self.client.request("frame", commands=self.scene())
                    time.sleep(.05)
                    before = self.client.request("stats")
                    deadline = time.monotonic() + .5
                    updates = 0
                    while time.monotonic() < deadline:
                        commands = self.scene()
                        commands[2][1] = f"Frame {updates}"
                        self.client.request("frame", commands=commands)
                        self.client.request("present", scheduled=True)
                        updates += 1
                        time.sleep(.002)
                    after = self.client.request("stats")
                    elapsed = after["elapsed_seconds"] - before["elapsed_seconds"]
                    frames = after["frames"] - before["frames"]
                    self.assertLessEqual(frames, fps * elapsed + 2)
                    self.assertGreater(frames, 2)
                    self.assertEqual(after["scene_updates"] - before["scene_updates"], updates)

    def test_first_scene_and_explicit_present_do_not_wait_for_the_cap(self):
        self.client.request("configure", redraw_interval=10, fps_limit=1)
        self.client.request("frame", commands=self.scene())
        first = self.wait_for_presentation(0)
        self.client.request("frame", commands=self.finite_shape(.1))
        self.client.request("present", scheduled=True)
        pending = self.client.request("stats")
        self.assertEqual(pending["frames"], first["frames"])
        self.assertEqual(pending["scene_updates"], first["scene_updates"] + 1)
        self.client.request("present")
        self.assertEqual(self.client.request("stats")["frames"], first["frames"] + 1)

    def test_patch_timings_are_separate_from_cached_presentations(self):
        segment = dict(id="control", bounds=[10, 10, 100, 40],
            commands=[["rect", [10, 10, 100, 40], "#ff0000", 0, "", 0]])
        self.assertTrue(self.info["scene_patches"])
        self.client.request("patch", background="#000000", upsert=[segment], order=["control"])
        self.client.request("configure", continuous=True, vsync=False, fps_limit=120)
        self.client.request("stats", reset=True)
        segment["commands"][0][2] = "#0000ff"
        self.client.request("patch", upsert=[segment])
        first = self.wait_for_presentation(self.client.request("stats")["frames"])
        time.sleep(.06)
        later = self.client.request("stats")
        self.assertGreater(later["frames"], first["frames"])
        self.assertEqual(later["scene_updates"], 1)
        self.assertEqual(later["last_scene_commands_touched"], 1)
        self.assertEqual(later["last_scene_commands_replayed"], 1)
        self.assertEqual(later["scene_update_samples_ms"], first["scene_update_samples_ms"])
        self.assertEqual(len(later["scene_update_samples_ms"]), 1)
        self.assertGreater(later["last_scene_update_seconds"], 0)
        self.assertGreaterEqual(later["max_scene_update_seconds"], later["last_scene_update_seconds"])
        self.assertGreaterEqual(later["last_scene_update_seconds"], later["last_scene_commit_seconds"])
        self.assertGreaterEqual(later["last_scene_update_seconds"], later["last_scene_render_seconds"])
        self.assertEqual(later["update_seconds"], later["scene_update_seconds"])

    def test_explicit_present_reuses_scene_without_counting_an_update(self):
        self.client.request("frame", commands=self.scene())
        before = self.client.request("stats")
        self.client.request("present")
        after = self.client.request("stats")
        self.assertGreater(after["frames"], before["frames"])
        self.assertEqual(after["scene_updates"], before["scene_updates"])
        self.assertEqual(after["commands_touched"], before["commands_touched"])
        self.assertEqual(after["scene_update_samples_ms"], before["scene_update_samples_ms"])

    def test_python_render_policy_exposes_real_vsync_and_preserves_omitted_mode(self):
        host = NativeHost(hidden=True, vsync=False)
        self.addCleanup(host.close)
        host.open("Rendering policy", 320, 160, False, 1)
        host.configure_rendering(redraw_interval=0, fps_limit=None, reduce_motion=True,
                                 vsync=False)
        uncapped = host.native_stats()
        self.assertEqual(host.info["vsync"], 0)
        self.assertEqual(uncapped["vsync"], 0)
        self.assertTrue(uncapped["continuous"])
        self.assertEqual(uncapped["fps_limit"], 0)
        host.configure_rendering(redraw_interval=None, fps_limit=60, reduce_motion=True)
        capped = host.native_stats()
        self.assertEqual(capped["vsync"], 0)
        self.assertFalse(capped["continuous"])
        self.assertEqual(capped["fps_limit"], 60)

    def test_patch_removal_retry_is_idempotent_and_update_samples_are_bounded(self):
        segment = dict(id="control", bounds=[10, 10, 10, 10],
            commands=[["rect", [10, 10, 10, 10], "#ff0000", 0, "", 0]])
        self.client.request("patch", background="#000000", upsert=[segment], order=["control"])
        self.client.request("patch", remove=["control"], order=[])
        self.client.request("patch", remove=["control"], order=[])
        self.assertEqual(self.client.request("stats")["segment_count"], 0)
        self.client.request("stats", reset=True)
        for _ in range(123):
            self.client.request("patch", upsert=[segment])
        stats = self.client.request("stats")
        self.assertEqual(stats["scene_updates"], 123)
        self.assertEqual(len(stats["scene_update_samples_ms"]), 120)
        self.assertTrue(all(sample > 0 for sample in stats["scene_update_samples_ms"]))

    def test_nonfinite_values_rejected_before_transport(self):
        before = self.client.diagnostics["requests_sent"]
        with self.assertRaises(ValueError):
            self.client.request("measure", text="x", size=float("nan"), mono=False)
        self.assertEqual(before, self.client.diagnostics["requests_sent"])

    def test_unknown_operations_are_recoverable(self):
        with self.assertRaises(NativeHostError):
            self.client.request("not_an_operation")
        self.assertIn("frames", self.client.request("stats"))

    def test_animation_metadata_is_closed_and_rejection_preserves_both_scene_forms(self):
        animation = {"property": "x", "from": 10, "to": 10, "duration": 1, "loop": False}
        shape = ["animated_rect", [10, 10, 20, 20], "#ff0000", 0, "", 0, animation]
        for operation in ("frame", "patch"):
            def arguments(command):
                if operation == "frame":
                    return {"commands": [["begin", "#000000"], command]}
                return {"upsert": [dict(id="shape", bounds=[10, 10, 20, 20], commands=[command])],
                        "order": ["shape"], "background": "#000000"}

            # Normal animation dictionaries remain repeatable retained commands.
            self.client.request(operation, **arguments(shape))
            self.client.request(operation, **arguments(shape))
            before = self.client.request("stats")
            for fields in ({"extra": None}, {"extra": 1}, {"extra": {"nested": {"value": 1}}},
                           {"loop": {"nested": 1}}):
                with self.subTest(operation=operation, fields=fields):
                    invalid = [*shape[:-1], {**animation, **fields}]
                    with self.assertRaises(NativeHostError):
                        self.client.request(operation, **arguments(invalid))
                    after = self.client.request("stats")
                    for key in ("scene_updates", "retained_scene_bytes", "command_count", "segment_count"):
                        self.assertEqual(after[key], before[key], key)
            # JSON permits duplicate keys. A second nested loop member must not
            # evade validation because the first loop member is a valid boolean.
            payload = json.dumps({"op": operation, **arguments(
                [*shape[:-1], {**animation, "extra": {"nested": 1}}])})
            payload = payload.replace('"extra":', '"loop":').encode("utf-8")
            with self.assertRaises(NativeHostError):
                self.client._request(9, payload, timeout=5)
            self.assertEqual(self.client.request("stats")["scene_updates"], before["scene_updates"])
            self.client.request("present")
            self.assertTrue(self.client.is_alive)

    def test_measure_many_matches_single_measures_and_rejects_an_oversized_batch(self):
        single = self.client.request("measure", text="café λ", size=16, mono=False)
        batch = self.client.request(
            "measure_many", texts=["café λ", "WWWW", ""], size=16, mono=True,
        )
        self.assertEqual(len(batch), 3)
        self.assertEqual(batch[0][1], single[1])
        self.assertEqual(batch[1][0], self.client.request(
            "measure", text="WWWW", size=16, mono=True)[0])
        with self.assertRaises(NativeHostError):
            self.client.request("measure_many", texts=["x"] * 4097, size=16, mono=False)

    def test_measure_multiline_unicode_and_monospace(self):
        one = self.client.request("measure", text="café λ", size=16, mono=False)
        two = self.client.request("measure", text="café λ\ncafé λ", size=16, mono=False)
        self.assertGreater(one[0], 0)
        self.assertEqual(two, [one[0], 2 * one[1]])
        small = self.client.request("measure", text="iiii", size=16, mono=True)
        wide = self.client.request("measure", text="WWWW", size=16, mono=True)
        self.assertEqual(small[0], wide[0])

    def test_native_animation_repaints_without_python_updates(self):
        commands = [["begin", "#101010"], ["animated_rect", [0, 10, 40, 40], "#00ffaa", 8, "", 0,
                     {"property": "x", "from": 0, "to": 200, "duration": .2, "loop": True, "yoyo": True}]]
        self.client.request("frame", commands=commands)
        first = self.client.request("stats")
        time.sleep(.1)
        last = self.client.request("stats")
        self.assertGreater(last["frames"], first["frames"])
        self.assertEqual(last["scene_updates"], first["scene_updates"])

    def wait_for_presentation(self, after, *, settled=False):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            stats = self.client.request("stats")
            if stats["frames"] > after and (not settled or not stats["animating"]):
                return stats
            time.sleep(.005)
        self.fail(f"Native presentation did not {'settle' if settled else 'advance'}: {stats}")

    def assert_presentations_idle(self, stats):
        frames = stats["frames"]
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            time.sleep(.08)
            current = self.client.request("stats")["frames"]
            if current == frames:
                return
            frames = current
        self.assertEqual(self.client.request("stats")["frames"], frames)

    @staticmethod
    def finite_shape(duration=.1, loop=False):
        return [["begin", "#101010"], ["animated_rect", [0, 10, 10, 10], "#00ffaa", 0, "", 0,
            {"property": "x", "from": 0, "to": 300, "duration": duration, "loop": loop, "yoyo": False}]]

    def test_full_frame_finite_animation_presents_endpoint_then_sleeps(self):
        for fps in (4, 60, 120):
            with self.subTest(fps=fps):
                self.client.request("configure", continuous=False, fps_limit=fps)
                self.client.request("frame", commands=self.scene())
                time.sleep(.05)  # Drain startup events and the setup presentation.
                before = self.client.request("stats")
                self.client.request("frame", commands=self.finite_shape(.1))
                first = self.wait_for_presentation(before["frames"])
                # A low cap may make the first draw the animation endpoint.
                # Stats never forces a draw, unlike capture/present requests.
                final = (self.wait_for_presentation(first["frames"], settled=True)
                         if first["animating"] else first)
                self.assertEqual(final["scene_updates"], first["scene_updates"])
                self.assert_presentations_idle(final)

    def test_full_frame_transition_presents_endpoint_and_releases_snapshot(self):
        self.client.request("configure", continuous=False, fps_limit=4)
        self.client.request("frame", commands=self.scene())
        time.sleep(.05)
        before = self.client.request("stats")
        commands = self.scene()
        commands[1][2] = "#ff0000"
        self.client.request("frame", commands=commands, transition_seconds=.1)
        first = self.wait_for_presentation(before["frames"])
        if first["animating"]:
            self.assertGreater(first["retained_scene_bytes"], before["retained_scene_bytes"])
        final = (self.wait_for_presentation(first["frames"], settled=True)
                 if first["animating"] else first)
        self.assertEqual(final["retained_scene_bytes"], before["retained_scene_bytes"])
        self.assert_presentations_idle(final)
        self.client.request("frame", commands=self.scene(), transition_seconds=1)
        transitioning = self.wait_for_presentation(final["frames"])
        self.assertTrue(transitioning["animating"])
        self.assertGreater(transitioning["retained_scene_bytes"], before["retained_scene_bytes"])
        self.client.request("configure", reduce_motion=True)
        reduced = self.wait_for_presentation(transitioning["frames"], settled=True)
        self.assertEqual(reduced["retained_scene_bytes"], before["retained_scene_bytes"])
        self.assert_presentations_idle(reduced)

    def test_full_frame_animation_reduced_motion_resume_and_replacement(self):
        self.client.request("configure", continuous=False, fps_limit=60)
        self.client.request("frame", commands=self.finite_shape(.4))
        first = self.wait_for_presentation(0)
        self.assertTrue(first["animating"])
        self.client.request("configure", reduce_motion=True)
        reduced = self.wait_for_presentation(first["frames"], settled=True)
        self.assert_presentations_idle(reduced)
        self.client.request("configure", reduce_motion=False)
        resumed = self.wait_for_presentation(reduced["frames"])
        self.assertTrue(resumed["animating"])
        final = self.wait_for_presentation(resumed["frames"], settled=True)
        self.assert_presentations_idle(final)

        self.client.request("frame", commands=self.finite_shape(1, loop=True))
        looping = self.wait_for_presentation(final["frames"])
        later = self.wait_for_presentation(looping["frames"])
        self.assertTrue(later["animating"])
        self.client.request("patch", background="#101010", upsert=[], order=[])
        retained = self.wait_for_presentation(later["frames"], settled=True)
        self.assert_presentations_idle(retained)
        self.client.request("frame", commands=self.scene())
        replaced = self.wait_for_presentation(retained["frames"], settled=True)
        self.assert_presentations_idle(replaced)
        self.client.request("frame", commands=[])
        empty = self.wait_for_presentation(replaced["frames"], settled=True)
        for reduce in (True, False):
            self.client.request("configure", reduce_motion=reduce)
            empty = self.wait_for_presentation(empty["frames"], settled=True)
            self.assert_presentations_idle(empty)

    def test_close_is_idempotent(self):
        self.client.close()
        self.client.close()
        self.assertIsNotNone(self.client.process.poll())

    def test_disconnect_releases_the_native_window(self):
        self.client._disconnect()
        self.client.process.wait(timeout=3)


@unittest.skipUnless(EXE.is_file(), "Build the standalone C host first")
class NativeTerminalPatchProtocolTests(unittest.TestCase):
    def setUp(self):
        self.client = NativeClient(lambda event: None, executable=EXE)
        self.addCleanup(self.client.close)
        self.info = self.client.request("open", backend="terminal", hidden=True,
            headless=True, columns=8, rows=3, color="truecolor", continuous=False)
        self.left = dict(id="left", bounds=[0, 0, 16, 16],
            commands=[["rect", [0, 0, 16, 16], "#ff0000", 0, "", 0]])
        self.right = dict(id="right", bounds=[32, 0, 16, 16],
            commands=[["rect", [32, 0, 16, 16], "#00ff00", 0, "", 0]])

    def initial_scene(self):
        self.assertTrue(self.info["scene_patches"])
        self.client.request("patch", background="#000000",
                            upsert=[self.left, self.right], order=["left", "right"])

    def test_terminal_patch_updates_cells_and_reports_actual_backend_work(self):
        self.initial_scene()
        self.client.request("stats", reset=True)
        self.left["commands"][0][2] = "#0000ff"
        self.client.request("patch", upsert=[self.left])
        stats = self.client.request("stats")
        self.assertEqual(stats["scene_updates"], 1)
        self.assertEqual(stats["segment_count"], 2)
        self.assertEqual(stats["command_count"], 2)
        self.assertEqual(stats["commands_touched"], 1)
        self.assertEqual(stats["last_scene_commands_touched"], 1)
        self.assertEqual(stats["last_scene_commands_replayed"], 2)
        self.assertEqual(stats["last_scene_segments_tested"], 2)
        self.assertEqual(stats["cell_frames_composed"], 1)
        self.assertEqual(stats["ansi_frames_encoded"], 1)
        self.assertGreater(stats["ansi_bytes_encoded"], 0)
        self.assertEqual(stats["terminal_bytes_written"], 0)
        self.assertEqual(stats["terminal_output_pending_bytes"], 0)
        self.assertGreater(stats["last_scene_update_seconds"], 0)
        self.assertGreaterEqual(stats["last_scene_update_seconds"], stats["last_scene_commit_seconds"])
        self.assertEqual(len(stats["scene_update_samples_ms"]), 1)
        cells = self.client.request("snapshot")["cells"]
        self.assertEqual(cells[0]["background"], [0, 0, 255])
        self.assertEqual(cells[4]["background"], [0, 255, 0])

    def test_cached_terminal_present_and_stats_reset_preserve_retained_scene(self):
        self.initial_scene()
        before = self.client.request("stats")
        for _ in range(3):
            self.client.request("present")
        after = self.client.request("stats")
        self.assertEqual(after["frames"], before["frames"] + 3)
        self.assertEqual(after["scene_updates"], before["scene_updates"])
        self.assertEqual(after["commands_touched"], before["commands_touched"])
        self.assertEqual(after["commands_replayed"], before["commands_replayed"])
        self.assertEqual(after["segments_tested"], before["segments_tested"])
        self.assertEqual(after["cell_frames_composed"], before["cell_frames_composed"])
        self.assertEqual(after["ansi_frames_encoded"], before["ansi_frames_encoded"])
        self.assertEqual(after["ansi_bytes_encoded"], before["ansi_bytes_encoded"])
        self.assertGreater(after["cell_cache_hits"], before["cell_cache_hits"])
        self.assertEqual(after["last_scene_commands_replayed"], before["last_scene_commands_replayed"])
        self.assertEqual(after["scene_update_samples_ms"], before["scene_update_samples_ms"])
        cells = self.client.request("snapshot")["cells"]
        self.client.request("stats", reset=True)
        reset = self.client.request("stats")
        for key in ("frames", "scene_updates", "commands_touched", "commands_replayed",
                    "segments_tested", "last_scene_commands_touched", "last_scene_commands_replayed",
                    "last_scene_segments_tested", "cell_frames_composed", "cell_cache_hits",
                    "ansi_frames_encoded", "ansi_bytes_encoded", "terminal_bytes_written"):
            self.assertEqual(reset[key], 0, key)
        self.assertEqual(reset["segment_count"], 2)
        self.assertEqual(reset["command_count"], 2)
        self.assertEqual(reset["scene_update_samples_ms"], [])
        self.assertEqual(self.client.request("snapshot")["cells"], cells)

    def test_invalid_terminal_patch_preserves_cells_and_host_update_counters(self):
        self.initial_scene()
        before = self.client.request("stats")
        cells = self.client.request("snapshot")["cells"]
        with self.assertRaises(NativeHostError):
            self.client.request("patch", upsert=[dict(id="left", bounds=[0, 0, 16, 16],
                                                      commands=[["unknown"]])])
        after = self.client.request("stats")
        self.assertEqual(after["scene_updates"], before["scene_updates"])
        self.assertEqual(after["frames"], before["frames"])
        self.assertEqual(after["scene_update_samples_ms"], before["scene_update_samples_ms"])
        self.assertEqual(self.client.request("snapshot")["cells"], cells)


@unittest.skipUnless(EXE.is_file(), "Build the standalone C host first")
class NativeImageReloadTests(unittest.TestCase):
    def test_reload_refreshes_shared_sources_and_retries_failures_without_resubmission(self):
        directory = PACKAGE.parents[1] / "work" / "image-reload-tests" / uuid4().hex
        directory.mkdir(parents=True)
        source, other, capture = (directory / name for name in ("preview.png", "other.png", "frame.png"))
        self.addCleanup(directory.rmdir)
        for path in (source, other, capture):
            self.addCleanup(path.unlink, missing_ok=True)
        for backend in ("window", "terminal"):
            for retained in (False, True):
                for missing in (False, True):
                    with self.subTest(backend=backend, retained=retained, missing=missing):
                        client = NativeClient(lambda event: None, executable=EXE)
                        try:
                            info = client.request("open", backend=backend, hidden=True, headless=True,
                                width=160, height=96, color="truecolor", scale=1, vsync=False,
                                resizable=False, font_dir=str(PACKAGE / "assets"))
                            if not info.get("images", True):
                                self.skipTest("Build with SDL_image to test image reload")
                            source.unlink(missing_ok=True)
                            if not missing:
                                source.write_bytes(encode_png(1, 1, bytes((255, 0, 0, 255))))
                            other.write_bytes(encode_png(1, 1, bytes((0, 255, 0, 255))))
                            commands = [["image", str(path), [x, 0, 16, 16], "#ffffff", "stretch"]
                                        for path, x in ((source, 0), (source, 32), (other, 64))]
                            if retained:
                                client.request("patch", background="#000000", upsert=[
                                    dict(id=str(i), bounds=[i * 32, 0, 16, 16], commands=[command])
                                    for i, command in enumerate(commands)], order=["0", "1", "2"])
                            else:
                                client.request("frame", commands=[["begin", "#000000"], *commands])
                            self.wait_for_frame(client, 0)
                            before = self.colors(client, backend, capture)
                            if not missing:
                                self.assertEqual(before, [(255, 0, 0), (255, 0, 0), (0, 255, 0)])
                            source.write_bytes(encode_png(1, 1, bytes((0, 0, 255, 255))))
                            other.write_bytes(encode_png(1, 1, bytes((0, 0, 255, 255))))
                            client.request("present")
                            self.assertEqual(self.colors(client, backend, capture), before)
                            if backend == "terminal":
                                client.request("configure", fps_limit=1)
                                client.request("present")
                            prior = client.request("stats")
                            reloaded = client.request("reload_image", source=str(source))
                            self.assertEqual(reloaded["resource_revision"], prior["resource_revision"] + 1)
                            if backend == "terminal":
                                self.assertEqual(self.colors(client, backend, capture), before)
                            after = self.wait_for_frame(client, prior["frames"])
                            self.assertEqual(after["scene_updates"], prior["scene_updates"])
                            self.assertEqual(self.colors(client, backend, capture),
                                             [(0, 0, 255), (0, 0, 255), (0, 255, 0)])
                            with self.assertRaises(NativeHostError):
                                client.request("reload_image", source=None)
                            self.assertEqual(client.request("stats")["resource_revision"],
                                             reloaded["resource_revision"])
                        finally:
                            client.close()

    def wait_for_frame(self, client, before):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            stats = client.request("stats")
            if stats["frames"] > before:
                return stats
            time.sleep(.005)
        self.fail("Reload did not schedule a native presentation")

    @staticmethod
    def colors(client, backend, path):
        if backend == "terminal":
            cells = client.request("snapshot")["cells"]
            return [tuple(cells[x // 8]["background"]) for x in (8, 40, 72)]
        client.request("capture", path=str(path))
        width, _, pixels = decode_png(path.read_bytes())
        return [tuple(pixels[(8 * width + x) * 4:(8 * width + x) * 4 + 3]) for x in (8, 40, 72)]


if __name__ == "__main__":
    unittest.main()
