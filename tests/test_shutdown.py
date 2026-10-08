"""Engine shutdown is isolated so its terminal lifecycle cannot affect pytest."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("cleanup_delay", [0, .05, 1.2])
def test_shutdown_closes_host_and_settles_opening_during_close_cancellation(cleanup_delay):
    root = Path(__file__).resolve().parents[1]
    source = f'''
import asyncio, json, sys, threading
sys.path[:0] = [{str(root / "src")!r}, {str(root / "tests")!r}]
from pysual import App, shutdown
from pysual._engine import call
from test_library import RecordingHost
entered = threading.Event()
class Demo(App):
    async def Demo_on_closing(self, event):
        entered.set()
        try:
            await asyncio.sleep(30)
        finally:
            await asyncio.sleep({cleanup_delay!r})
app, host = Demo(), RecordingHost()
app.run(backend=host)
opening = call(app._capture_presentation)
app.close()
assert entered.wait(2)
error = None
try:
    shutdown()
except BaseException as exc:
    error = type(exc).__name__
print(json.dumps(dict(closed=host.closed, done=opening.done, error=error)))
'''
    completed = subprocess.run([sys.executable, "-I", "-c", source],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    outcome = json.loads(completed.stdout)
    assert outcome["closed"]
    assert outcome["done"]
    if cleanup_delay < 1:
        assert outcome["error"] is None
    else:
        assert outcome["error"] == "ExceptionGroup"
