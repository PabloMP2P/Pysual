"""Public snapshots keep scene submissions separate from optional host counters."""

from dataclasses import FrozenInstanceError
import time

import pytest

from pysual import App, RenderDiagnostics, terminal
from pysual._engine import is_ui_thread
from pysual.backends._native_client import native_executable
from pysual.backends.native import NativeHost
from pysual.backends.web import WebHost
from pysual.errors import LifecycleError
from test_library import RecordingHost


class CountingHost(RecordingHost):
    native_retained = True
    backend = "window"

    def __init__(self):
        super().__init__()
        self.stats_calls = 0
        self.presentations = 40

    def native_stats(self):
        assert is_ui_thread(), "Native diagnostics must run on the UI owner"
        self.stats_calls += 1
        return {"frames": self.presentations}


def test_snapshot_is_immutable_explicit_and_does_not_render_or_poll():
    class Demo(App):
        def build(self):
            self.before_open = self.diagnostics(include_native=True)

    app, host = Demo(reduce_motion=True), CountingHost()
    try:
        assert app.diagnostics(include_native=True) == RenderDiagnostics()
        app.run(backend=host)
        assert app.before_open.scene_submissions == 0
        assert app.before_open.native_presentations is None
        assert host.stats_calls == 0
        initial = app.diagnostics()
        assert initial.scene_submissions == 1
        assert initial.native_presentations is None
        assert (initial.backend, initial.host_name) == ("window", "CountingHost")
        with pytest.raises(FrozenInstanceError):
            initial.scene_submissions = 200

        # Calls from this non-owner thread must remain read-only, even when idle.
        time.sleep(.05)
        for _ in range(3):
            assert app.diagnostics() == initial
        assert host.stats_calls == 0
        sampled = app.diagnostics(include_native=True)
        assert sampled.scene_submissions == 1
        assert sampled.native_presentations == 40
        assert host.stats_calls == 1
        host.presentations = 55
        assert sampled.native_presentations == 40  # A value, not a live view.
        assert app.frame_count == 55  # Preserve the pre-existing native behavior.
        assert host.stats_calls == 2
        assert app.diagnostics().scene_submissions == 1

        app.request_frame()
        deadline = time.monotonic() + 5
        while app.diagnostics().scene_submissions == 1:
            assert time.monotonic() < deadline
            time.sleep(.005)
        assert initial.scene_submissions == 1
        assert host.stats_calls == 2
        app.close()
        app.wait(timeout=5)
        assert app.diagnostics(include_native=True) == RenderDiagnostics()
        assert host.stats_calls == 2
    finally:
        app.destroy()
    with pytest.raises(LifecycleError):
        app.diagnostics()


@pytest.mark.parametrize("kind", ("web", "python-terminal", "window", "c-terminal"))
def test_real_host_identity_and_optional_presentation_counts(kind):
    if kind in ("window", "c-terminal") and not native_executable().is_file():
        pytest.skip("Build the native helper")
    if kind == "web":
        host = WebHost(open_browser=False)
        expected = ("LiveSVGHost", "web", "live", None, None)
    elif kind == "window":
        host = NativeHost(hidden=True, vsync=False)
        expected = ("NativeHost", "window", None, None, True)
    else:
        renderer = "python" if kind == "python-terminal" else "c"
        host = terminal(renderer=renderer, hidden=True)
        expected = ("TerminalHost", "terminal", None, renderer, True)
    app = App(width=320, height=160, reduce_motion=True)
    try:
        app.run(backend=host)
        basic = app.diagnostics()
        assert (basic.host_name, basic.backend, basic.web_execution,
                basic.terminal_renderer, basic.hidden) == expected
        assert basic.scene_submissions >= 1
        assert basic.native_presentations is None
        sampled = app.diagnostics(include_native=True)
        if kind in ("window", "c-terminal"):
            assert sampled.native_presentations >= 1
            assert app.frame_count >= sampled.native_presentations
        else:
            assert sampled.native_presentations is None
            assert app.frame_count == sampled.scene_submissions
        # Native startup viewport/expose events may request another real frame.
        assert sampled.scene_submissions >= basic.scene_submissions
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()


def test_managed_terminal_reports_selected_adapter_without_exposing_wrapper(monkeypatch):
    import pysual.backends as backends

    host = terminal(renderer="python", hidden=True)
    monkeypatch.setattr(backends, "create_host", lambda name: host)
    app = App(width=320, height=160, reduce_motion=True)
    try:
        app.run(backend="terminal")
        snapshot = app.diagnostics(include_native=True)
        assert snapshot.host_name == "TerminalHost"
        assert snapshot.backend == "terminal"
        assert snapshot.terminal_renderer == "python"
        assert snapshot.native_presentations is None
        assert snapshot.hidden is True
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()
