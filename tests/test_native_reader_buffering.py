"""Framing and failure contracts for the bounded burst reader."""
from collections import deque
import json
import threading

import pytest

from pysual.backends import _native_client as protocol


def frame(payload, opcode=101, request=0):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return protocol.HEADER.pack(b"PXN1", opcode, request, len(raw)) + raw


class SocketChunks:
    def __init__(self, chunks):
        self.chunks = deque(chunks)
        self.read_sizes = []
        self.closed = False

    def recv(self, size):
        assert size > 0
        self.read_sizes.append(size)
        if not self.chunks:
            return b""
        chunk = self.chunks.popleft()
        if isinstance(chunk, Exception):
            raise chunk
        if len(chunk) > size:
            self.chunks.appendleft(chunk[size:])
        return chunk[:size]

    def shutdown(self, how):
        pass

    def close(self):
        self.closed = True


def run_reader(monkeypatch, chunks, pending=None):
    events = []
    client = protocol.NativeClient(events.append)
    sock = client._socket = SocketChunks(chunks)
    if pending is not None:
        client._pending = pending
    monkeypatch.setattr(protocol.select, "select", lambda reads, writes, errors: (reads, writes, errors))
    client._read_loop()
    assert sock.closed
    assert client._socket is None
    assert client.diagnostics["closed_events"] == 1
    return client, events, sock


def test_one_read_dispatches_multiple_events_and_acknowledgements_in_order(monkeypatch):
    acknowledgement = {"event": threading.Event()}
    expected = [{"kind": "motion", "x": 1}, {"kind": "text", "text": "café λ"}]
    messages = [frame(expected[0]), frame({"ok": True, "result": 42}, 200, 7),
                frame(expected[1]), frame({"reason": "closed"}, 102)]
    client, events, sock = run_reader(monkeypatch, [b"".join(messages)], {7: acknowledgement})
    assert events == expected
    assert acknowledgement["event"].is_set()
    assert acknowledgement["result"] == 42
    assert len(sock.read_sizes) == 1
    assert client.diagnostics["bytes_received"] == sum(map(len, messages))
    assert client.diagnostics["replies_received"] == 1
    assert client.diagnostics["input_events"] == 2
    assert client._transport_error is None


def test_fragmented_header_payload_and_multibyte_text_survive_would_block(monkeypatch):
    expected = {"kind": "text", "text": "café λ"}
    message = frame(expected)
    chunks = [message[:3], BlockingIOError(), message[3:17]]
    chunks.extend(bytes([value]) for value in message[17:])
    chunks.append(frame({"reason": "closed"}, 102))
    client, events, _ = run_reader(monkeypatch, chunks)
    assert events == [expected]
    assert client._transport_error is None


def test_payload_larger_than_read_chunk_and_following_frame(monkeypatch):
    expected = {"kind": "text", "text": "x" * (protocol.RECEIVE_CHUNK * 2 + 17)}
    client, events, sock = run_reader(
        monkeypatch, [frame(expected) + frame({"reason": "closed"}, 102)])
    assert events == [expected]
    assert len(sock.read_sizes) == 3
    assert max(sock.read_sizes) <= protocol.RECEIVE_CHUNK
    assert client._transport_error is None


@pytest.mark.parametrize("partial", [b"", b"PX", frame({"kind": "text"})[:-1]])
def test_eof_fails_pending_and_reports_one_terminal_error(monkeypatch, partial):
    pending = {"event": threading.Event()}
    client, events, _ = run_reader(monkeypatch, [partial] if partial else [], {1: pending})
    assert pending["event"].is_set()
    assert isinstance(pending["error"], protocol.NativeHostError)
    assert events == [{"kind": "error", "text": "Native host disconnected"}]
    assert client._pending == {}


@pytest.mark.parametrize("header", [
    protocol.HEADER.pack(b"NOPE", 101, 0, 100),
    protocol.HEADER.pack(b"PXN1", 101, 0, protocol.MAX_PAYLOAD + 1),
])
def test_invalid_header_rejected_before_waiting_for_payload(monkeypatch, header):
    client, events, sock = run_reader(monkeypatch, [header, b"should not be read"])
    assert len(sock.read_sizes) == 1
    assert events == [{"kind": "error", "text": "Invalid response framing"}]
    assert client.diagnostics["bytes_received"] == 0


@pytest.mark.parametrize("invalid,error", [
    (frame([]), "Host response is not an object"),
    (frame({}, 999), "Unexpected host message"),
    (frame({}, 101, 1), "Unexpected host message"),
    (frame({"ok": True}, 200, 1), "Unexpected or duplicate host acknowledgement"),
])
def test_invalid_message_stops_later_frames(monkeypatch, invalid, error):
    client, events, _ = run_reader(monkeypatch, [invalid + frame({"kind": "motion"})])
    assert events == [{"kind": "error", "text": error}]
    assert client.diagnostics["input_events"] == 0


def test_duplicate_acknowledgement_preserves_first_result_and_fails_remaining(monkeypatch):
    first, second = {"event": threading.Event()}, {"event": threading.Event()}
    ack = frame({"ok": True, "result": 42}, 200, 1)
    _, events, _ = run_reader(monkeypatch, [ack + ack], {1: first, 2: second})
    assert first["result"] == 42 and "error" not in first
    assert first["event"].is_set() and second["event"].is_set()
    assert "duplicate" in str(second["error"])
    assert len(events) == 1
