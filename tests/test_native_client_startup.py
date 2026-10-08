"""Startup waits for protocol progress; real failures still release the caller."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from pysual.backends._native_client import NativeClient, NativeHostError


def test_missing_helper_explains_installed_and_checkout_options(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = NativeClient(executable=tmp_path / "missing-host")
    try:
        with pytest.raises(NativeHostError) as caught:
            client.request("open")
        message = str(caught.value)
        assert "matching native wheel" in message
        assert "python -m pysual native from the repository checkout" in message
        assert "--backend web" in message
        assert "--backend terminal --terminal python" in message
        assert "docs/building.md" in message
        assert client.process is None
    finally:
        client.close()


@pytest.fixture
def delayed_helper(tmp_path, monkeypatch):
    helper = tmp_path / "delayed_helper.py"
    helper.write_text(textwrap.dedent('''
        import argparse, json, socket, struct, time
        from pathlib import Path
        parser = argparse.ArgumentParser()
        parser.add_argument('--port', type=int)
        parser.add_argument('--token')
        parser.add_argument('--mode')
        parser.add_argument('--ready-file')
        args = parser.parse_args()
        time.sleep(0.08)
        if args.mode == 'exit_startup':
            raise SystemExit(7)
        header = struct.Struct('<4sIII')
        with socket.create_connection(('127.0.0.1', args.port)) as sock:
            handshake = header.pack(b'PXN1', 1, 0, 64) + args.token.encode('ascii')
            if args.mode in ('slow_handshake', 'exit_handshake', 'close_handshake'):
                sock.sendall(handshake[:7])
                if args.mode == 'exit_handshake':
                    time.sleep(0.08)
                    raise SystemExit(9)
                if args.mode == 'close_handshake':
                    Path(args.ready_file).write_text('partial handshake sent')
                    time.sleep(30)  # Explicit client cleanup terminates this child.
                time.sleep(2.2)
                sock.sendall(handshake[7:32])
                time.sleep(0.08)
                sock.sendall(handshake[32:])
            else:
                sock.sendall(handshake)
            def exact(length):
                result = bytearray()
                while len(result) < length:
                    part = sock.recv(length - len(result))
                    if not part:
                        raise SystemExit(0)
                    result.extend(part)
                return result
            while True:
                magic, opcode, request, length = header.unpack(exact(header.size))
                op = json.loads(exact(length))['op']
                if op == 'frame' and args.mode == 'exit_frame':
                    raise SystemExit(8)
                if op in ('open', 'measure', 'frame', 'slow_update'):
                    time.sleep(0.08)
                if op == 'open' and args.mode == 'reject_open':
                    body = {'ok': False, 'error': 'startup refused'}
                else:
                    body = {'ok': True, 'result': {'operation': op}}
                data = json.dumps(body).encode('utf-8')
                sock.sendall(header.pack(b'PXN1', 200, request, len(data)) + data)
                if op == 'close':
                    raise SystemExit(0)
    '''), encoding="utf-8")
    popen = subprocess.Popen
    ready_file = tmp_path / 'handshake-ready'

    def start(mode="normal", on_event=None):
        def launch(command, **kwargs):
            return popen([sys.executable, str(helper), *command[1:], "--mode", mode,
                          "--ready-file", str(ready_file)], **kwargs)
        monkeypatch.setattr(subprocess, "Popen", launch)
        return NativeClient(on_event, executable=Path(sys.executable), timeout=0.03)

    start.ready_file = ready_file
    return start


def test_slow_connection_open_measure_and_first_frame_complete(delayed_helper):
    client = delayed_helper()
    try:
        for op in ("open", "measure", "frame"):
            assert client.request(op) == {"operation": op}
        # Ordinary request protection resumes after the first content frame.
        with pytest.raises(TimeoutError, match="acknowledgement timed out"):
            client.request("slow_update")
        assert client.diagnostics["closed"]
    finally:
        client.close()


def test_partial_authenticated_handshake_can_take_longer_than_two_seconds(delayed_helper):
    client = delayed_helper('slow_handshake')
    try:
        assert client.request('open') == {'operation': 'open'}
        assert client.request('frame') == {'operation': 'frame'}
    finally:
        client.close()


@pytest.mark.parametrize("mode,operation,message", [
    ("exit_startup", "open", "exited during startup"),
    ("exit_handshake", "open", "exited during startup"),
    ("exit_frame", "frame", "disconnected"),
    ("reject_open", "open", "startup refused"),
])
def test_actual_startup_failures_propagate(delayed_helper, mode, operation, message):
    client = delayed_helper(mode)
    try:
        if operation != "open":
            client.request("open")
        with pytest.raises(NativeHostError, match=message):
            client.request(operation)
    finally:
        client.close()


def test_close_does_not_wait_behind_an_opening_send(delayed_helper, monkeypatch):
    events = []
    client = delayed_helper(on_event=events.append)
    entered, release = threading.Event(), threading.Event()
    try:
        client.request("open")

        def hold_send(frame, timeout):
            entered.set()
            release.wait(5)

        monkeypatch.setattr(client, "_send", hold_send)
        with ThreadPoolExecutor(2) as pool:
            opening = pool.submit(client.request, "frame")
            try:
                assert entered.wait(1)
                pool.submit(client.close).result(2)
                assert client.diagnostics["closed"]
                assert not events  # Intentional cleanup is not a transport error.
            finally:
                release.set()
            with pytest.raises(NativeHostError):
                opening.result(2)
    finally:
        release.set()
        client.close()


def test_close_interrupts_a_partial_handshake(delayed_helper):
    events = []
    client = delayed_helper('close_handshake', on_event=events.append)
    try:
        with ThreadPoolExecutor(2) as pool:
            opening = pool.submit(client.request, 'open')
            try:
                deadline = time.monotonic() + 3
                while not delayed_helper.ready_file.exists():
                    assert time.monotonic() < deadline, 'helper never sent its partial handshake'
                    time.sleep(0.005)
                pool.submit(client.close).result(2)
                with pytest.raises(NativeHostError, match='closed during startup'):
                    opening.result(2)
                assert not client.is_alive
                assert not events
            finally:
                client.close()
    finally:
        client.close()
