"""Exercise the stress example through the public application lifecycle."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from examples.stress_test import MIXES, PRESETS, StressConfig, StressTest
from pysual import terminal
from pysual._engine import call
from pysual.backends._native_client import native_executable
from pysual.backends.native import NativeHost
from pysual.backends.web import WebHost
from pysual.host import TextFile
from test_library import RecordingHost


class StressRecordingHost(RecordingHost):
    capabilities = RecordingHost.capabilities | {"image_fit"}

    def image(self, *args, **kwargs):
        pass


@contextmanager
def opened(config, host=None):
    app = StressTest(config)
    try:
        app.run(backend=host if host is not None else StressRecordingHost())
        yield app
    finally:
        app.close()
        try:
            app.wait(timeout=8)
        finally:
            app.destroy()


def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Stress example did not finish its operation")
        time.sleep(.005)


def test_custom_count_clears_preset_selection():
    with opened(StressConfig(controls=250, mix='forms', workload='static')) as app:
        assert len(app.field.children) == 250
        assert app.preset_choice.selected_index == -1
        app.apply_configuration(replace(app.configuration, controls=500))
        assert app.preset_choice.selected_index == tuple(PRESETS.values()).index(500)
        app.apply_configuration(replace(app.configuration, controls=250))
        assert app.preset_choice.selected_index == -1


@pytest.mark.parametrize("changes", [
    {"controls": -1}, {"controls": True}, {"controls": 20_001},
    {"mix": "unknown"}, {"workload": "unknown"},
    {"fraction": -0.1}, {"fraction": 1.1}, {"fraction": math.nan},
    {"rate": 0}, {"rate": math.inf}, {"duration": -1}, {"warmup": -1},
    {"seed": -1}, {"fps_limit": -1}, {"redraw": -1},
    {"full_loop": True, "redraw": None},
])
def test_invalid_workloads_fail_before_opening_a_host(changes):
    with pytest.raises(ValueError):
        StressConfig(**changes)


@pytest.mark.parametrize("mix", tuple(MIXES))
def test_each_mix_builds_the_requested_scene_with_portable_painting(mix):
    count = 2 * len(MIXES[mix])
    with opened(StressConfig(controls=count, mix=mix, workload="static")) as app:
        report = app.snapshot_report()
        assert report["stress_controls"] == count
        assert sum(report["control_types"].values()) == count
        assert len(report["control_types"]) == len(MIXES[mix])
        assert not report["resource_errors"]
        assert call(lambda: all(c.parent is app.field for c in app._cells))
        assert len(app.field.children) == count
        json.dumps(report, allow_nan=False)


@pytest.mark.parametrize("workload,breadth", [
    ("paint", 5), ("layout", 5), ("property", 5), ("local-update", 1),
])
def test_seeded_mutations_touch_exactly_the_requested_breadth(workload, breadth):
    config = StressConfig(controls=20, workload="static", fraction=.25, seed=97)
    with opened(config) as app:
        # Keep the asynchronous driver idle while testing explicit ticks.
        app.pause_button.activate()
        until(lambda: app.pause_button.text == "Resume")
        app.apply_configuration(replace(config, workload=workload))
        first = app.step_workload()
        assert len(first) == len(set(first)) == breadth
        assert all(0 <= index < 20 for index in first)
        second = app.step_workload()
        assert not set(first) & set(second)
        app.apply_configuration(replace(config, workload=workload))
        assert app.step_workload() == first
        assert app.step_workload() == second


def test_sample_shows_backend_and_full_cycle_fps_for_the_active_preset():
    with opened(StressConfig(controls=PRESETS["small"], workload="static", warmup=.01)) as app:
        until(lambda: not app.snapshot_report()["warming_up"])
        app.sample_button.activate()
        until(lambda: "—" not in app.fps_readout.text)
        prefix, cycle = app.fps_readout.text.split(" · Full cycle FPS ")
        assert prefix.startswith("Preset small · Backend FPS ")
        float(prefix.removeprefix("Preset small · Backend FPS "))
        float(cycle)


def test_live_metrics_start_enabled():
    config = StressConfig()
    assert config.telemetry is True
    assert config.controls == PRESETS["large"]
    assert config.redraw == 0
    assert config.vsync is False
    with opened(StressConfig(controls=1, workload="static", warmup=30)) as app:
        assert app.telemetry.checked
        assert app.configuration.telemetry
        assert not hasattr(app, "apply_button")


def test_preset_changes_apply_without_a_button():
    with opened(StressConfig(controls=PRESETS["small"], workload="static", telemetry=False, warmup=30)) as app:
        def choose_medium():
            app.preset_choice._origin = "user"
            try:
                app.preset_choice.selected_index = tuple(PRESETS).index("medium")
            finally:
                app.preset_choice._origin = "program"

        call(choose_medium)
        until(lambda: app.configuration.controls == PRESETS["medium"], timeout=30)
        assert len(app.field.children) == PRESETS["medium"]
        assert app.fps_readout.text == "Preset medium · Backend FPS — · Full cycle FPS —"


def test_apply_pause_resume_and_idle_sampling_use_the_real_event_bindings():
    with opened(StressConfig(controls=8, workload="static", telemetry=False, redraw=None, warmup=.01)) as app:
        until(lambda: not app.snapshot_report()["warming_up"])
        time.sleep(.05)
        before = call(lambda: app._runtime.frames)
        time.sleep(.3)
        assert call(lambda: app._runtime.frames) == before

        app.count_input.value = 17
        call(app._commit_editors)
        until(lambda: app.configuration.controls == 17)
        assert len(app.field.children) == 17
        app.pause_button.activate()
        until(lambda: app.pause_button.text == "Resume")
        report = app.snapshot_report()
        time.sleep(.08)
        assert app.snapshot_report() == report
        app.pause_button.activate()
        until(lambda: app.pause_button.text == "Pause")
        until(lambda: not app.snapshot_report()["warming_up"])
        assert app.snapshot_report()["measurements"]["elapsed_seconds"] < .5


def test_resizing_restarts_measurements_for_the_new_viewport():
    with opened(StressConfig(controls=8, workload="static", warmup=.03)) as app:
        until(lambda: not app.snapshot_report()["warming_up"])
        started = call(lambda: app._measurements.started_at)
        app.width = 900
        until(lambda: app.viewport.width == 900)
        until(lambda: call(lambda: app._measurements is not None
                          and app._measurements.started_at > started))
        assert app.snapshot_report()["viewport"]["width"] == 900


def test_export_serializes_requests_and_recovers_from_save_errors_and_cancellation():
    class SavingHost(StressRecordingHost):
        capabilities = StressRecordingHost.capabilities | {"text_files"}
        calls = 0

        async def save_text_file(self, text, suggested_name, location):
            self.calls += 1
            if self.calls == 1:
                self.release = asyncio.Event()
                await self.release.wait()
                raise PermissionError("Destination is read-only")
            if self.calls == 2:
                return None
            self.saved = json.loads(text)
            return TextFile(suggested_name, text)

    host = SavingHost()
    with opened(StressConfig(controls=8, workload="static"), host) as app:
        app.export_button.activate()
        until(lambda: host.calls == 1)
        assert not app.export_button.enabled
        app.export_button.activate()
        time.sleep(.03)
        assert host.calls == 1
        call(host.release.set)
        until(lambda: app.export_button.enabled)
        assert "read-only" in app.status.text
        assert app.is_open
        app.export_button.activate()
        until(lambda: app.status.text == "Export cancelled.")
        assert app.export_button.enabled
        app.export_button.activate()
        until(lambda: app.status.text == "Report saved.")
        assert host.saved["stress_controls"] == 8
        assert app.is_open


@pytest.mark.parametrize("changes", [
    {"rate": .001}, {"redraw": .0001}, {"cache_mib": 5000}, {"controls": 0},
])
def test_valid_boundary_options_can_be_displayed_by_the_dashboard(changes):
    config = replace(StressConfig(controls=8, workload="static"), **changes)
    with opened(config) as app:
        assert app.configuration == config
        assert not app.snapshot_report()["resource_errors"]


@pytest.mark.parametrize("backend", ["python-terminal", "web", "window", "c-terminal"])
def test_timed_mixed_workload_closes_and_retains_a_report(backend):
    if backend in ("window", "c-terminal") and not native_executable().is_file():
        pytest.skip("Build the native helper")
    if backend == "python-terminal":
        host = terminal(renderer="python", hidden=True)
    elif backend == "c-terminal":
        host = terminal(renderer="c", hidden=True)
    elif backend == "window":
        host = NativeHost(hidden=True, vsync=False)
    else:
        host = WebHost(open_browser=False)
    config = StressConfig(controls=36, workload="property", fraction=1,
                          rate=20, warmup=.03, duration=.3)
    with opened(config, host) as app:
        app.wait(timeout=8)
        assert not app.is_open
        report = app.snapshot_report()
        stats = report["measurements"]
        assert stats["elapsed_seconds"] >= config.duration
        assert stats["mutation_ticks"] > 0
        assert stats["scene_submissions"] > 0
        assert not report["resource_errors"]
        assert (stats["native"] is not None) == (backend in ("window", "c-terminal"))
        assert app.snapshot_report() == report
        json.dumps(report, allow_nan=False)


def test_cli_documents_options_and_rejects_invalid_runs_without_opening_ui():
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONPATH=str(root / "src"))
    script = [sys.executable, str(root / "examples/stress_test.py")]
    result = subprocess.run([*script, "--help"], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    for option in ("--mix", "--duration", "--full-loop", "--report", "--fraction", "--all-presets", "--no-telemetry"):
        assert option in result.stdout
    assert "default: large" in result.stdout
    assert "default: continuous" in result.stdout
    assert "default off" in result.stdout
    result = subprocess.run([*script, "--controls", "-1"], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "controls" in result.stderr
    result = subprocess.run([*script, "--all-presets"], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "duration" in result.stderr
    result = subprocess.run([*script, "--all-presets", "--duration", "1", "--controls", "10"],
                            env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "controls" in result.stderr
