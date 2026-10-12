"""The owner gets one wake per batch, including poll/rebind/shutdown races."""
from collections import deque
import threading

from pysual.backends import native


class OwnerLoop:
    def __init__(self):
        self.callbacks = deque()
        self.scheduled = 0

    def call_soon_threadsafe(self, callback, *args):
        self.scheduled += 1
        self.callbacks.append((callback, args))

    def run(self):
        while self.callbacks:
            callback, args = self.callbacks.popleft()
            callback(*args)


def bind(monkeypatch, host, callback):
    loop = OwnerLoop()
    monkeypatch.setattr(native.asyncio, "get_running_loop", lambda: loop)
    host.set_wake_callback(callback)
    return loop


def test_one_wake_until_poll_even_after_wake_is_delivered(monkeypatch):
    host, wakes = native.NativeHost(), []
    loop = bind(monkeypatch, host, lambda: wakes.append(True))
    for x in range(1000):
        host._on_event({"kind": "motion", "x": x})
    assert loop.scheduled == 1
    loop.run()
    host._on_event({"kind": "motion", "x": 1000})
    assert loop.scheduled == 1
    assert len(wakes) == 1
    assert [event.x for event in host.poll()] == list(range(1001))
    host._on_event({"kind": "motion", "x": 1001})
    loop.run()
    assert loop.scheduled == len(wakes) == 2


def test_poll_invalidates_queued_wake_without_losing_next_batch(monkeypatch):
    host, wakes = native.NativeHost(), []
    loop = bind(monkeypatch, host, lambda: wakes.append(host.poll()))
    host._on_event({"kind": "motion", "x": 1})
    assert host.poll()[0].x == 1
    host._on_event({"kind": "motion", "x": 2})
    loop.run()
    assert len(wakes) == 1
    assert wakes[0][0].x == 2


def test_event_arriving_while_wake_polls_creates_a_new_wake(monkeypatch):
    host, batches = native.NativeHost(), []

    def receive():
        batches.append(host.poll())
        if len(batches) == 1:
            producer = threading.Thread(target=host._on_event, args=({"kind": "motion", "x": 2},))
            producer.start()
            producer.join(timeout=2)
            assert not producer.is_alive()

    loop = bind(monkeypatch, host, receive)
    host._on_event({"kind": "motion", "x": 1})
    loop.run()
    assert [[event.x for event in batch] for batch in batches] == [[1], [2]]


def test_rebind_wakes_pending_events_and_skips_previous_owner(monkeypatch):
    host, old_wakes, new_wakes = native.NativeHost(), [], []
    old_loop = bind(monkeypatch, host, lambda: old_wakes.append(True))
    host._on_event({"kind": "motion", "x": 1})
    new_loop = bind(monkeypatch, host, lambda: new_wakes.append(True))
    assert len(new_wakes) == 1
    old_loop.run()
    assert old_wakes == []
    host._on_event({"kind": "motion", "x": 2})
    assert new_loop.scheduled == 0
    assert len(host.poll()) == 2
    host._on_event({"kind": "motion", "x": 3})
    new_loop.run()
    assert len(new_wakes) == 2


def test_unbind_and_later_bind_preserve_queued_events(monkeypatch):
    host, wakes = native.NativeHost(), []
    loop = bind(monkeypatch, host, lambda: wakes.append(True))
    host._on_event({"kind": "motion", "x": 1})
    host.set_wake_callback(None)
    loop.run()
    host._on_event({"kind": "motion", "x": 2})
    assert wakes == []
    bind(monkeypatch, host, lambda: wakes.append(True))
    assert len(wakes) == 1
    assert [event.x for event in host.poll()] == [1, 2]


def test_overflow_retains_one_error_and_existing_batch_wake(monkeypatch):
    host, wakes = native.NativeHost(), []
    loop = bind(monkeypatch, host, lambda: wakes.append(host.poll()))
    for x in range(4098):
        host._on_event({"kind": "motion", "x": x})
    assert loop.scheduled == 1
    loop.run()
    assert len(wakes) == len(wakes[0]) == 1
    assert wakes[0][0].kind == "error"
    assert "4096" in wakes[0][0].text
    host._on_event({"kind": "motion"})
    assert host.poll() == []


def test_close_cancels_queued_wakes_and_events(monkeypatch):
    host, wakes = native.NativeHost(), []
    loop = bind(monkeypatch, host, lambda: wakes.append(True))
    host._on_event({"kind": "motion"})
    host.close()
    loop.run()
    host._on_event({"kind": "motion"})
    assert wakes == []
    assert host.poll() == []


def test_closed_owner_loop_does_not_prevent_rebinding(monkeypatch):
    host, wakes = native.NativeHost(), []
    loop = bind(monkeypatch, host, lambda: None)

    def closed_loop(*args):
        raise RuntimeError("Event loop is closed")

    loop.call_soon_threadsafe = closed_loop
    host._on_event({"kind": "motion"})
    bind(monkeypatch, host, lambda: wakes.append(host.poll()))
    assert len(wakes) == len(wakes[0]) == 1
