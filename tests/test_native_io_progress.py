"""Real host protocol progress across fragmented input and stalled readers.

Use ordinary JSON commands and public results. No performance thresholds or
test-only host switches/counters are needed. Windows backpressure is observed
through the test socket's receive window and stalled ingress, not inferred from
a fixed sleep.
"""
from contextlib import contextmanager
import ctypes
import json
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time

import pytest

from pysual.backends._native_client import native_executable
from pysual.backends._term_input import TerminalInput
from pysual.host import Input

ROOT = Path(__file__).resolve().parents[1]
EXE = native_executable()
HEADER = struct.Struct("<4sIII")
pytestmark = pytest.mark.skipif(not EXE.is_file(), reason="Build the standalone native host first")


class RawHost:
    def __init__(self, sock):
        self.sock = sock
        self.next_id = 1
        self.received_bytes = 0
        self.reply_size = 0
        self.events = []

    def exact(self, size):
        data = bytearray()
        while len(data) < size:
            part = self.sock.recv(size - len(data))
            assert part, "Host closed with a partial protocol frame"
            data.extend(part)
            self.received_bytes += len(part)
        return bytes(data)

    def receive(self):
        magic, opcode, request, size = HEADER.unpack(self.exact(HEADER.size))
        assert magic == b"PXN1" and size <= 16 * 1024 * 1024
        return opcode, request, self.exact(size)

    def packet(self, op, **fields):
        request = self.next_id
        self.next_id += 1
        data = json.dumps({"op": op, **fields}, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode("utf-8")
        return request, HEADER.pack(b"PXN1", 9, request, len(data)) + data

    def reply(self, request):
        while True:
            opcode, actual, raw = self.receive()
            if opcode == 101:
                assert actual == 0
                self.events.append(json.loads(raw))
                continue
            assert (opcode, actual) == (200, request)
            result = json.loads(raw)
            assert result["ok"] is True, result
            self.reply_size = HEADER.size + len(raw)
            return result["result"]

    def request(self, op, **fields):
        request, packet = self.packet(op, **fields)
        self.sock.sendall(packet)
        return self.reply(request)

    def collect_events(self, count):
        while len(self.events) < count:
            opcode, request, raw = self.receive()
            assert (opcode, request) == (101, 0)
            self.events.append(json.loads(raw))


@contextmanager
def raw_host(backend):
    token = secrets.token_hex(32)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(10)
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen([str(EXE), "--port", str(listener.getsockname()[1]), "--token", token],
        cwd=EXE.parent, creationflags=flags, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    sock = None
    try:
        sock, _ = listener.accept()
        sock.settimeout(20)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        host = RawHost(sock)
        assert host.receive() == (1, 0, token.encode("ascii"))
        fields = dict(backend=backend, width=160, height=96, hidden=True, vsync=False,
                      scale=1, continuous=False, font_dir=str(ROOT / "src/pysual/assets"))
        if backend == "terminal":
            fields.update(headless=True, columns=20, rows=6)
        host.request("open", **fields)
        yield host
        assert host.request("close") is None
        process.wait(timeout=5)
        assert process.returncode == 0, process.stderr.read().decode("utf-8", "replace")
    finally:
        listener.close()
        if sock is not None:
            sock.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        process.stderr.close()


@pytest.mark.parametrize("backend", ["window", "terminal"])
def test_fragmented_large_request_and_buffered_command_batches_make_progress(backend):
    with raw_host(backend) as host:
        request, packet = host.packet("configure", continuous=False, ignored_padding="λ" * 300_000)
        # Break the header and a multibyte UTF-8 character, then exceed two
        # 256 KiB receive budgets without asking the renderer to draw huge text.
        first_lambda = packet.index("λ".encode())
        cuts = [0, 3, 15, first_lambda + 1, first_lambda + 100_001, len(packet)]
        for start, end in zip(cuts, cuts[1:]):
            host.sock.sendall(packet[start:end])
        configured = host.reply(request)
        assert isinstance(configured, dict) if backend == "window" else configured is None

        requests, packets = zip(*(host.packet("stats") for _ in range(97)))
        host.sock.sendall(b"".join(packets))
        replies = [host.reply(request) for request in requests]
        assert all(isinstance(reply, dict) for reply in replies)
        assert [reply["requests"] for reply in replies] == list(
            range(replies[0]["requests"], replies[0]["requests"] + len(replies)))
        # No additional write was needed to release already-buffered commands
        # after the 32-command processing budget. A fresh wake must also work.
        assert host.request("frame", commands=[["begin", "#123456"]]) is None
        before = host.request("stats")
        assert host.request("present") is None
        assert host.request("stats")["frames"] > before["frames"]


def test_headless_terminal_preserves_input_events_across_command_batches():
    with raw_host("terminal") as host:
        expected = []
        oracle = TerminalInput()
        pieces = [f"\x1b[200~item-{index:03d} café\x1b[201~".encode() for index in range(97)]
        packets, requests = [], []
        for piece in pieces:
            expected.extend(oracle.feed(piece))
            request, packet = host.packet("feed_input", hex=piece.hex())
            requests.append(request)
            packets.append(packet)
        host.sock.sendall(b"".join(packets))
        for request in requests:
            assert host.reply(request) is None
        host.collect_events(len(expected))
        assert [Input(**event) for event in host.events] == expected
        stats = host.request("stats")
        assert stats["input_events"] == len(expected)
        assert [Input(**event) for event in host.events] == expected
        assert host.request("frame", commands=[["begin", "#123456"]]) is None
        assert host.request("present") is None


@pytest.mark.parametrize("backend", ["window", "terminal"])
def test_buffered_commands_do_not_bypass_continuous_frame_limit(backend):
    with raw_host(backend) as host:
        assert host.request("frame", commands=[["begin", "#123456"]]) is None
        assert host.request("present") is None
        host.request("configure", continuous=True, fps_limit=30, vsync=False)
        before = host.request("stats")
        deadline = time.monotonic() + .25
        while time.monotonic() < deadline:
            requests, packets = zip(*(host.packet("stats") for _ in range(97)))
            host.sock.sendall(b"".join(packets))
            for request in requests:
                host.reply(request)
        after = host.request("stats")
        elapsed = after["elapsed_seconds"] - before["elapsed_seconds"]
        frames = after["frames"] - before["frames"]
        assert frames > 0
        assert frames <= 30 * elapsed + 2
        assert after["scene_updates"] == before["scene_updates"]


def tcp_info(sock):
    # Read-only Windows TCP_INFO_v0, available since Windows 10 1703. This
    # avoids production instrumentation and needs no privileged packet capture.
    # https://learn.microsoft.com/windows/win32/api/mstcpip/ns-mstcpip-tcp_info_v0
    class TCPInfo(ctypes.Structure):
        _fields_ = [("State", ctypes.c_uint32), ("Mss", ctypes.c_uint32),
            ("ConnectionTimeMs", ctypes.c_uint64), ("TimestampsEnabled", ctypes.c_ubyte),
            *[(name, ctypes.c_uint32) for name in
              ("RttUs", "MinRttUs", "BytesInFlight", "Cwnd", "SndWnd", "RcvWnd", "RcvBuf")],
            ("BytesOut", ctypes.c_uint64), ("BytesIn", ctypes.c_uint64),
            *[(name, ctypes.c_uint32) for name in
              ("BytesReordered", "BytesRetrans", "FastRetrans", "DupAcksIn", "TimeoutEpisodes")],
            ("SynRetrans", ctypes.c_ubyte)]

    winsock = ctypes.WinDLL("Ws2_32.dll")
    winsock.WSAIoctl.argtypes = [ctypes.c_size_t, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p, ctypes.c_void_p]
    winsock.WSAIoctl.restype = ctypes.c_int
    winsock.WSAGetLastError.restype = ctypes.c_int
    version, returned, info = ctypes.c_uint32(0), ctypes.c_uint32(), TCPInfo()
    result = winsock.WSAIoctl(sock.fileno(), 0xD8000027, ctypes.byref(version), ctypes.sizeof(version),
        ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(returned), None, None)
    if result:
        error = winsock.WSAGetLastError()
        if error in (10022, 10045):
            pytest.skip("Windows TCP_INFO_v0 is unavailable")
        raise OSError(error, "Could not inspect the test socket's receive window")
    assert returned.value >= TCPInfo.RcvWnd.offset + ctypes.sizeof(ctypes.c_uint32)
    return info


@pytest.mark.skipif(sys.platform != "win32", reason="Windows network-event rearming")
def test_slow_reader_recovers_after_observed_receive_window_stall():
    with raw_host("window") as host:
        initial = tcp_info(host.sock)  # Check support before accumulating replies.
        host.request("stats")
        count = (32 * 1024 * 1024 + host.reply_size - 1) // host.reply_size
        assert 1000 < count < 100_000
        host.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        requests, packets = zip(*(host.packet("stats") for _ in range(count)))
        sender_errors = []

        def send_commands():
            try:
                host.sock.sendall(b"".join(packets))
            except OSError as error:
                sender_errors.append(error)

        # The sender must not prevent this reader from observing/releasing
        # pressure if the host temporarily stops accepting command bytes.
        sender = threading.Thread(target=send_commands, daemon=True)
        sender.start()
        try:
            # Do not consume response bytes. Observe actual TCP flow control,
            # rather than assuming a sleep or a reply count filled the socket.
            deadline = time.monotonic() + 10
            previous_bytes, stalled_since = None, None
            while True:
                now = time.monotonic()
                info = tcp_info(host.sock)
                # Windows can retain a nonzero sub-MSS window while avoiding
                # tiny segments. Observe both restricted flow control and
                # unchanged ingress, rather than requiring literal zero.
                restricted = info.BytesIn > initial.BytesIn and info.RcvWnd < info.Mss
                if restricted and info.BytesIn == previous_bytes:
                    if stalled_since is None:
                        stalled_since = now
                    if now - stalled_since >= .1:
                        break
                else:
                    stalled_since = None
                previous_bytes = info.BytesIn
                assert not sender_errors, sender_errors
                assert now < deadline, "Reader did not create a TCP receive-window stall"
                time.sleep(.005)
            host.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            previous = None
            for request in requests:
                reply = host.reply(request)
                if previous is not None:
                    assert reply["requests"] == previous + 1
                previous = reply["requests"]
            sender.join(timeout=5)
            assert not sender.is_alive(), "Command sender did not finish after the reader resumed"
            assert not sender_errors, sender_errors
        finally:
            if sender.is_alive():
                host.sock.shutdown(socket.SHUT_RDWR)
                sender.join(timeout=5)
        assert host.received_bytes > 16 * 1024 * 1024
        # A subsequent socket wake, ordinary ACK and presentation must survive
        # after the receive window reopens; every queued response arrived once.
        assert host.request("frame", commands=[["begin", "#abcdef"]]) is None
        before = host.request("stats")
        assert host.request("present") is None
        assert host.request("stats")["frames"] > before["frames"]
