"""Behavioral and scaling regressions for shared-core performance paths."""

import asyncio
from concurrent.futures import Future
from contextvars import copy_context
import random
import threading
from types import SimpleNamespace
from unittest.mock import patch

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost
from test_retained_paint import PatchRecordingHost

from pysual import App, Button, Container, Control, Label, Rect
from pysual._engine import Engine, _Request
from pysual._modality import ModalManager, contains
from pysual.backends._web_native import LiveSVGHost
from pysual.input import Router, _HitIndex
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.runtime import Runtime


class CorePerformanceTests(UIOwnerTestCase):
    def test_visibility_reads_each_ordinary_enabled_flag_once(self):
        reads = []

        class Values(dict):
            def __getitem__(self, key):
                if key == "enabled":
                    reads.append(key)
                return super().__getitem__(key)

        root = node = Container()
        self.addCleanup(root.destroy)
        nodes = [node]
        for _ in range(63):
            node = Container(parent=node)
            nodes.append(node)
        for control in nodes:
            control._values = Values(control._values)
        self.assertTrue(Router(None)._visible(node))
        self.assertEqual(len(reads), len(nodes))

    def test_custom_enabled_policy_can_override_its_local_flag_but_not_ancestors(self):
        class Policy(Container):
            @property
            def effective_enabled(self):
                return self._allowed

        root = Container()
        self.addCleanup(root.destroy)
        parent = Policy(parent=root, enabled=False)
        child = Button(parent=parent)
        router = Router(None)
        parent._allowed = True
        self.assertTrue(router._visible(child))
        parent._allowed = False
        self.assertFalse(router._visible(child))
        parent._allowed = True
        root.enabled = False
        self.assertFalse(router._visible(child))

    def test_modal_rank_map_matches_ancestry_sort(self):
        randomizer = random.Random(2718)
        root = Container()
        self.addCleanup(root.destroy)
        nodes = [root]
        for _ in range(120):
            control = Container(parent=randomizer.choice(nodes))
            control._overlay = randomizer.choice((False, True))
            nodes.append(control)
        runtime = SimpleNamespace(popup=None)
        manager = ModalManager(runtime)
        for _ in range(100):
            modals = randomizer.sample(nodes, randomizer.randrange(6))
            runtime.popup = randomizer.choice([None, *nodes])
            manager._openings = [SimpleNamespace(control=c, presentation=SimpleNamespace(modal=True))
                                 for c in modals]
            for parent in nodes:
                def priority(child):
                    rank = max((i + 1 for i, modal in enumerate(modals)
                                if contains(child, modal)), default=0)
                    if runtime.popup is not None and contains(child, runtime.popup):
                        rank = len(modals) + 1
                    return rank, child._overlay
                self.assertEqual(manager.paint_order(parent.children),
                                 sorted(parent.children, key=priority))

    def test_incremental_hit_geometry_matches_fresh_index(self):
        app = App(width=640, height=400)
        host = RecordingHost()
        host.size = (640, 400)
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(app.destroy)
        self.addCleanup(setattr, app, "_runtime", None)
        stable = Container(parent=app, width=640, height=400)
        for index in range(150):
            Button(parent=stable, left=index % 20 * 30, top=index // 20 * 30,
                   width=25, height=25)
        active = Container(parent=app, left=50, top=260, width=100, height=100)
        child = Button(parent=active, left=60, width=70, height=40)
        arrange(app, host)
        initial = runtime.router._hit_index
        rng = random.Random(313)
        for left, width, height, top in ((80, 100, 100, 260), (150, 80, 100, 260), (0, 600, 350, 0), (200, 90, 100, 260)):
            active.update(left=left, width=width, height=height, top=top)
            arrange(app, host)
            self.assertIs(runtime.router._hit_index, initial)
            fresh = _HitIndex(app, runtime._modality)
            points = [(rng.randrange(640), rng.randrange(400)) for _ in range(80)]
            points += [(child._clip.x + 1, child._clip.y + 1), (left + width + 1, 270)]
            for point in points:
                self.assertEqual(list(initial.candidates(*point)), list(fresh.candidates(*point)))
        child.visible = False
        arrange(app, host)
        self.assertIsNot(runtime.router._hit_index, initial)
        child.visible = True
        child.left = -200
        arrange(app, host)
        child.left = 0
        arrange(app, host)
        self.assertIs(runtime.router.hit(app, active._rect.x + 1, active._rect.y + 1), child)

    def test_retained_local_layout_preserves_metadata_and_scene(self):
        app, host = App(width=640, height=400), PatchRecordingHost()
        runtime = Runtime(app, host)
        app._runtime = runtime
        retained = runtime._retained_paint = RetainedPaintTree(host)
        self.addCleanup(app.destroy)
        self.addCleanup(setattr, app, "_runtime", None)
        stable = Container(parent=app, width=400, height=300)
        for index in range(100):
            Label(parent=stable, text=str(index), width=30, height=20,
                  left=index % 10 * 35, top=index // 10 * 25)
        target = Button(parent=app, top=330, width=120, height=35)
        runtime._paint_frame()
        records, controls, order = retained._records, retained._control_order, retained._order
        for properties in ({"text": "Changed"}, {"left": 40}, {"width": 150}):
            target.update(**properties)
            runtime._paint_frame()
            self.assertIs(retained._records, records)
            self.assertIs(retained._control_order, controls)
            self.assertIs(retained._order, order)
            snapshot = host.snapshot()
            paint_tree(app, host)
            self.assertEqual(snapshot, tuple(host._drawing))
        target.visible = False
        runtime._paint_frame()
        self.assertNotIn(target, retained._records)

    def test_worker_slice_count_time_fifo_and_cancellation(self):
        # A deterministic owner inbox avoids wall-clock thresholds and races.
        for clock, expected in ((lambda: 0, 64), (iter((0, 0.005)).__next__, 1)):
            engine = Engine.__new__(Engine)
            engine._lock = threading.Lock()
            engine._ident = threading.get_ident()
            engine._active_requests = 0
            engine.calls_completed = 0
            from collections import deque
            delivered = []
            futures = [Future() for _ in range(200)]
            futures[-1].cancel()
            engine._inbox = deque(_Request(lambda i=i: delivered.append(i), future, copy_context())
                                  for i, future in enumerate(futures))
            with patch("pysual._engine.monotonic", side_effect=clock):
                engine.drain_pending()
            self.assertEqual(delivered, list(range(expected)))
            while engine._inbox:
                engine.drain_pending()
            self.assertEqual(delivered, list(range(199)))
            self.assertEqual(engine._active_requests, 0)
            self.assertEqual(engine.calls_completed, 200)


class SchedulingPerformanceTests(AsyncUIOwnerTestCase):
    async def test_input_burst_preserves_order_and_allows_frames_and_handlers(self):
        for limit in (1000, 4):
            app = App(width=640, height=400, reduce_motion=True, fps_limit=None,
                      redraw_interval=None, max_pending_handlers=limit)
            host = RecordingHost()
            runtime = Runtime(app, host)
            app._runtime = runtime
            app._wake = runtime.invalidate
            app._attach_dispatcher(runtime.dispatcher)
            target = Button(parent=app, width=100, height=100)
            runtime._paint_frame()
            received, frame_counts = [], []
            positions = [float(i % 80 + 1) for i in range(1500)]
            def receive(event):
                received.append(event.x)
                frame_counts.append(runtime.frames)
                target.invalidate()
                if len(received) == len(positions):
                    runtime._stop.set()
            target.pointer_move.connect(receive)
            host.events = [Input("pointer_move", x=x, y=10) for x in positions]
            try:
                await asyncio.wait_for(runtime._pump(), 5)
                await runtime.dispatcher.drain()
                self.assertEqual(received, positions)
                self.assertGreater(max(frame_counts), min(frame_counts))
                self.assertEqual(runtime.dispatcher.pending, 0)
            finally:
                await runtime.dispatcher.shutdown()
                app._runtime = None
                app.destroy()

    async def test_live_web_wakes_owner_and_keeps_disconnect_deadlines(self):
        host = LiveSVGHost(open_browser=False)
        host._closed = False
        wakes = []
        host.set_wake_callback(lambda: wakes.append(threading.get_ident()))
        owner = threading.get_ident()
        with patch("pysual.backends._web_native.time.monotonic", return_value=100):
            await asyncio.to_thread(host._receive, {"events": [{"kind": "pointer_down", "x": 2}]})
            await asyncio.sleep(0)
            self.assertEqual(wakes, [owner])
            self.assertEqual(host.next_poll_delay, 60)
            self.assertEqual(host.poll()[0].kind, "pointer_down")
            host._receive({"disconnect": True})
            self.assertEqual(host.next_poll_delay, 2)
        with patch("pysual.backends._web_native.time.monotonic", return_value=102):
            self.assertEqual(host.poll()[0].kind, "close")
        with patch("pysual.backends._web_native.time.monotonic", return_value=160):
            self.assertEqual(host.next_poll_delay, 0)
            self.assertEqual(host.poll()[0].kind, "close")
        before = len(wakes)
        host.close()
        await asyncio.sleep(0)
        self.assertEqual(len(wakes), before)

    async def test_event_driven_idle_wait_includes_host_housekeeping(self):
        app = App(width=640, height=400, reduce_motion=True, redraw_interval=None)
        host = RecordingHost()
        host.event_driven = True
        host.next_poll_delay = 0.125
        runtime = Runtime(app, host)
        app._runtime = runtime
        runtime._paint_frame()
        waits = []
        async def wait(delay):
            waits.append(delay)
            runtime._stop.set()
        runtime._wait = wait
        try:
            await runtime._pump()
            self.assertGreater(waits[0], 0.1)
            self.assertLessEqual(waits[0], 0.125)
        finally:
            app._runtime = None
            app.destroy()
