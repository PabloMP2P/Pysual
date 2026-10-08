"""Bounded cold image work must not block the terminal's UI owner."""
import asyncio
import threading
import unittest
from unittest.mock import patch

from _terminal import MemoryTerminal, until
from _ui_testcase import AsyncUIOwnerTestCase
from pysual import App, Image, Rect, TextBox
from pysual.backends._term_images import AsyncImageCache, ImageCache
from pysual.backends.term_text import TermTextHost
from pysual.runtime import Runtime

RED = (1, 1, b"\xff\x00\x00\xff")
BLUE = (1, 1, b"\x00\x00\xff\xff")


class ImageLoadingTests(unittest.TestCase):
    def cache(self):
        result = AsyncImageCache()
        self.addCleanup(result.close)
        return result

    def complete(self, cache):
        cache._pending[1].exception(timeout=2)
        self.assertTrue(cache.poll())

    def test_plain_cache_stays_synchronous_and_hits_do_not_decode_again(self):
        cache = ImageCache()
        with patch.object(cache, "_decode", return_value=RED) as decode:
            self.assertEqual(cache.get("one"), RED)
            self.assertEqual(cache.get("one"), RED)
        self.assertEqual(decode.call_count, 1)
        cache.clear()
        self.assertEqual(cache.byte_size, 0)

    def test_one_pending_job_no_backlog_and_only_poll_publishes(self):
        cache = self.cache()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        worker_ids = []
        def decode(source):
            worker_ids.append(threading.get_ident())
            entered.set()
            self.assertTrue(release.wait(2))
            return RED if source == "one" else BLUE
        with patch.object(cache, "_decode", side_effect=decode) as load:
            self.assertIsNone(cache.get("one"))
            self.assertTrue(entered.wait(2))
            first = cache._pending
            for _ in range(5):
                self.assertIsNone(cache.get("one"))
                self.assertIsNone(cache.get("two"))
            self.assertIs(cache._pending, first)
            self.assertEqual(load.call_count, 1)
            release.set()
            first[1].result(timeout=2)
            self.assertEqual(cache.byte_size, 0)
            self.assertIsNone(cache.get("one"))
            self.complete(cache)
            self.assertEqual(cache.get("one"), RED)
            self.assertIsNone(cache.get("two"))
            self.complete(cache)
            self.assertEqual(cache.get("two"), BLUE)
            self.assertFalse(cache.poll())
            self.assertEqual(load.call_count, 2)
        self.assertTrue(all(value != threading.get_ident() for value in worker_ids))

    def test_failed_load_is_remembered_without_retry_or_repaint_loop(self):
        cache = self.cache()
        with patch.object(cache, "_decode", side_effect=ValueError("broken PNG")) as load:
            self.assertIsNone(cache.get("bad"))
            self.complete(cache)
            for _ in range(5):
                with self.assertRaisesRegex(ValueError, "broken PNG"):
                    cache.get("bad")
                self.assertFalse(cache.poll())
            self.assertEqual(load.call_count, 1)
            self.assertLessEqual(cache._failure_bytes, cache.MAX_BYTES)

    def test_capacity_pressure_uses_original_lru_without_repeated_async_repaints(self):
        for budget, entries in ((12, 64), (1024, 1)):
            with self.subTest(budget=budget, entries=entries):
                cache = self.cache()
                cache.MAX_BYTES, cache.MAX_ENTRIES = budget, entries
                with patch.object(cache, "_decode", return_value=RED):
                    self.assertIsNone(cache.get("a"))
                    self.complete(cache)
                    self.assertEqual(cache.get("a"), RED)
                    self.assertIsNone(cache.get("b"))
                    self.complete(cache)
                    self.assertTrue(cache._synchronous)
                    for _ in range(5):
                        self.assertEqual([cache.get(source) for source in ("a", "b")], [RED, RED])
                        self.assertFalse(cache.poll())
                        self.assertIsNone(cache._pending)
                    self.assertLessEqual(cache.byte_size, budget)
                    self.assertLessEqual(len(cache._entries), entries)

    def test_one_uncacheable_completion_is_released_after_paint(self):
        cache = self.cache()
        cache.MAX_BYTES = 1
        with patch.object(cache, "_decode", return_value=RED) as load:
            self.assertIsNone(cache.get("one"))
            self.complete(cache)
            self.assertTrue(cache._synchronous)
            self.assertEqual(cache.get("one"), RED)
            self.assertEqual(load.call_count, 1)
            self.assertEqual(cache.byte_size, 0)
            self.assertIsNone(cache._pending)
            self.assertIsNone(cache._executor)
            cache.finish_frame()
            self.assertIsNone(cache._uncached)
            self.assertEqual(cache.get("one"), RED)
            self.assertEqual(load.call_count, 2)
            self.assertFalse(cache.poll())

    def test_source_and_failed_entry_budgets_are_bounded(self):
        cache = self.cache()
        cache.MAX_SOURCE_CHARS = 3
        with self.assertRaisesRegex(ValueError, "input budget"):
            cache.get("long")
        self.assertIsNone(cache._executor)
        cache.MAX_BYTES = 16
        with patch.object(cache, "_decode", side_effect=ValueError("bad")):
            cache.get("a")
            self.complete(cache)
            cache.get("b")
            self.complete(cache)
        self.assertTrue(cache._synchronous)
        self.assertLessEqual(cache._failure_bytes, 16)
        self.assertIsNone(cache._pending)
        cache.finish_frame()
        self.assertIsNone(cache._uncached)

    def test_clear_reuses_cache_and_close_discards_a_late_result(self):
        cache = self.cache()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def decode(source):
            entered.set()
            self.assertTrue(release.wait(2))
            return RED
        with patch.object(cache, "_decode", side_effect=decode):
            cache.get("old")
            self.assertTrue(entered.wait(2))
            future = cache._pending[1]
            cache.close()
            self.assertFalse(future.done())
            release.set()
            future.result(timeout=2)
            self.assertFalse(cache.poll())
            self.assertEqual(cache.byte_size, 0)
            self.assertIsNone(cache.get("closed"))
        cache.clear()
        with patch.object(cache, "_decode", return_value=BLUE):
            self.assertIsNone(cache.get("new"))
            self.complete(cache)
            self.assertEqual(cache.get("new"), BLUE)


class TerminalImageRuntimeTests(AsyncUIOwnerTestCase):
    async def test_pending_image_keeps_input_live_and_repaints_cached_placeholder(self):
        entered, release = threading.Event(), threading.Event()
        def decode(source):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Owner did not release the test decoder")
            return RED
        app = App(width=160, height=96, layout="absolute", reduce_motion=True)
        Image(parent=app, source="synthetic", width=80, height=32, cache_paint=True)
        entry = TextBox(parent=app, text="", top=48, width=120, height=32)
        io = MemoryTerminal(20, 6)
        host = TermTextHost(io=io, probe_timeout=0, color="truecolor")
        runtime = Runtime(app, host)
        with patch.object(AsyncImageCache, "_decode", side_effect=decode) as load:
            task = asyncio.create_task(runtime.main())
            try:
                await until(lambda: app.is_open and entered.is_set())
                entry.focus()
                io.incoming = b"z"
                await until(lambda: entry.text == "z")
                self.assertIn("[image]", "\n".join(host._renderer.snapshot_rows()))
                # Stabilize the placeholder so render-cache reuse is exercised.
                for _ in range(3):
                    runtime.invalidate()
                    await asyncio.sleep(.025)
                self.assertGreater(runtime.render_cache.stats.hits, 0)
                before = host.resource_revision, host._renderer.resource_revision
                release.set()
                await until(lambda: any(cell.foreground == (255, 0, 0) for cell in host._renderer.cells))
                self.assertGreater(host.resource_revision, before[0])
                self.assertGreater(host._renderer.resource_revision, before[1])
                self.assertNotIn("[image]", "\n".join(host._renderer.snapshot_rows()))
                self.assertEqual(load.call_count, 1)
                self.assertFalse(task.done())
            finally:
                release.set()
                runtime._stop.set()
                await task
                app.destroy()

    async def test_closed_host_reopens_without_publishing_old_image(self):
        entered, release = threading.Event(), threading.Event()
        def decode(source):
            if source == "old":
                entered.set()
                if not release.wait(5):
                    raise AssertionError("Test did not release old decoder")
                return RED
            return BLUE
        host = TermTextHost(io=MemoryTerminal(10, 3), probe_timeout=0)
        with patch.object(AsyncImageCache, "_decode", side_effect=decode):
            try:
                host.open("old", 80, 48, False, 1)
                host.image("old", Rect(0, 0, 80, 32))
                await until(entered.is_set)
                old = host._renderer._images
                future = old._pending[1]
                host.close()
                self.assertFalse(future.done())
                host.open("new", 80, 48, False, 1)
                host.begin("#000000")
                host.image("new", Rect(0, 0, 80, 32))
                release.set()
                await until(lambda: future.done() and host._renderer._images._pending[1].done())
                host.poll()
                self.assertEqual(old.byte_size, 0)
                self.assertEqual(host._renderer._images.get("new"), BLUE)
                self.assertNotIn("old", host._renderer._images._entries)
            finally:
                release.set()
                host.close()
