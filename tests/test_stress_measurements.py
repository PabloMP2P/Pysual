"""Stress statistics distinguish elapsed throughput from retained work samples."""

import pytest

from examples.stress_test import Measurements, PRESETS, TIMING_CAPACITY, format_fps_readout, percentiles
from pysual import Container, FrameTiming, Label
from pysual._engine import call
from pysual.layout import arrange
from pysual.schema import _Slot
from pysual.theme import dark


def timing(number, completed, *, interval=0.0, layout=0.001, paint=0.002,
           update=0.004, routing=0.0):
    return FrameTiming(number, completed, interval, layout, paint, update, routing)


def test_submission_rate_includes_idle_tail_without_inventing_timing_samples():
    measured = Measurements(10.0, frames=100)
    measured.collect([timing(101, 10.1), timing(102, 10.2)])

    active = measured.summary(11.0, 120)
    idle = measured.summary(15.0, 120)

    assert active["scene_submissions_per_second"] == 20
    assert idle["scene_submissions"] == 20
    assert idle["scene_submissions_per_second"] == 4
    assert idle["timing_samples"] == 2
    assert idle["timings_ms"]["scene_update"] == {"p50": 4, "p95": 4, "p99": 4}
    # An entirely idle window has a real zero rate and no latency distribution.
    empty = Measurements(20.0, frames=120).summary(25.0, 120)
    assert empty["scene_submissions_per_second"] == 0
    assert empty["full_cycle_fps"] == 0
    assert empty["backend_fps"] == 0
    assert empty["backend_fps_source"] == "scene_submissions"
    assert empty["timings_ms"]["scene_update"] == {"p50": None, "p95": None, "p99": None}


def test_warmup_excludes_prior_and_crossing_work_but_not_a_later_idle_interval():
    measured = Measurements(10.0, frames=10)
    measured.collect([
        timing(9, 9.0, update=0.5),
        timing(10, 9.9, update=0.5),
        timing(11, 10.125, update=0.25),  # Started before the boundary.
        timing(12, 10.25, update=0.25),  # Started exactly at the boundary.
        # The prior presentation interval crosses warmup; the measured work
        # itself does not. It must remain in the update-cost distribution.
        timing(13, 10.5, interval=3.0, update=0.125),
    ])
    summary = measured.summary(11.0, 13)
    assert summary["timing_samples_seen"] == 2
    assert summary["timing_samples_dropped_before_collection"] == 0
    assert summary["timings_ms"]["scene_update"]["p50"] == pytest.approx(187.5)
    assert summary["scene_submissions"] == 3


def test_new_window_uses_its_own_baseline_and_rejects_old_or_duplicate_records():
    previous = Measurements(0.0)
    previous.collect([timing(1, 1.0, update=0.5)])
    previous.mutation(0.4, 200)

    current = Measurements(10.0, frames=100)
    sample = timing(101, 10.1, update=0.008)
    current.collect([timing(99, 9.9), sample, sample])
    summary = current.summary(12.0, 104)
    assert summary["scene_submissions"] == 4
    assert summary["scene_submissions_per_second"] == 2
    assert summary["timing_samples_seen"] == 1
    assert summary["timing_samples_dropped_before_collection"] == 0
    assert summary["timings_ms"]["scene_update"]["p99"] == 8
    assert summary["mutation_ticks"] == 0
    assert summary["controls_touched"] == 0


def test_missing_engine_records_are_separate_from_bounded_history_eviction():
    measured = Measurements(0.0, frames=10)
    # Engine ring loss removes 11 and 12; the local history later fills as well.
    count = TIMING_CAPACITY + 3
    measured.collect(timing(number, float(number)) for number in range(13, 13 + count))
    measured.collect([timing(13, 13.0)])  # Re-reading an old record adds no loss.
    final_number = 12 + count
    summary = measured.summary(float(final_number + 1), final_number)
    assert summary["timing_samples_seen"] == count
    assert summary["timing_samples"] == TIMING_CAPACITY
    assert summary["timing_samples_dropped_before_collection"] == 2
    assert summary["timing_samples_evicted"] == 3
    # A complete counter remains useful even when latency records were lost.
    assert summary["scene_submissions"] == count + 2
    assert summary["scene_submissions_per_second"] == pytest.approx(
        (count + 2) / (final_number + 1)
    )


@pytest.mark.parametrize(("values", "expected"), [
    ([], {"p50": None, "p95": None, "p99": None}),
    ([7], {"p50": 7, "p95": 7, "p99": 7}),
    ([40, 10, 30, 20], {"p50": 25, "p95": 38.5, "p99": 39.7}),
    ([0, 0, 100], {"p50": 0, "p95": 90, "p99": 98}),
])
def test_percentiles_use_inclusive_interpolation(values, expected):
    assert percentiles(iter(values)) == pytest.approx(expected)


def test_native_terminal_rates_keep_python_native_cells_and_output_distinct():
    baseline = {
        "renderer": "native-cells", "elapsed_seconds": 100.0,
        "frames": 1000, "scene_updates": 20, "scene_update_seconds": 0.2,
        "cell_frames_composed": 20, "ansi_frames_encoded": 15,
        "terminal_bytes_written": 1024,
    }
    native = {
        **baseline, "elapsed_seconds": 102.0, "frames": 21000,
        "scene_updates": 24, "scene_update_seconds": 0.3,
        "cell_frames_composed": 24, "ansi_frames_encoded": 17,
        "terminal_bytes_written": 5120,
    }
    measured = Measurements(10.0, frames=50, native=baseline)
    summary = measured.summary(15.0, 60, native=native)
    assert summary["scene_submissions_per_second"] == 2
    assert summary["full_cycle_fps"] == 2
    assert summary["backend_fps"] == 10000
    assert summary["backend_fps_source"] == "native_presentations"
    assert summary["native"]["elapsed_seconds"] == 2
    rates = summary["native"]["counters"]
    assert rates["frames"] == {"count": 20000, "per_second": 10000}
    assert rates["scene_updates"] == {"count": 4, "per_second": 2}
    assert rates["cell_frames_composed"] == {"count": 4, "per_second": 2}
    assert rates["ansi_frames_encoded"] == {"count": 2, "per_second": 1}
    assert rates["terminal_bytes_written"] == {"count": 4096, "per_second": 2048}
    assert summary["native"]["scene_update_mean_ms"] == pytest.approx(25)
    assert summary["timing_samples"] == 0  # Native totals are not Python timings.


def test_cached_native_presentations_have_no_fabricated_scene_update_duration():
    baseline = {"renderer": "window", "elapsed_seconds": 1, "frames": 20,
                "scene_updates": 3, "scene_update_seconds": 0.12}
    latest = {**baseline, "elapsed_seconds": 3, "frames": 20020}
    summary = Measurements(0, native=baseline).summary(2, 0, latest)
    assert summary["scene_submissions_per_second"] == 0
    assert summary["full_cycle_fps"] == 0
    assert summary["backend_fps"] == 10000
    assert summary["backend_fps_source"] == "native_presentations"
    assert summary["native"]["counters"]["frames"]["per_second"] == 10000
    assert summary["native"]["counters"]["scene_updates"]["count"] == 0
    assert summary["native"]["scene_update_mean_ms"] is None
    assert "terminal_bytes_written" not in summary["native"]["counters"]


def test_native_ring_ignores_pre_window_timings_when_no_new_scene_was_committed():
    baseline = {"elapsed_seconds": 10, "frames": 100, "scene_updates": 8,
                "scene_update_seconds": .4, "scene_update_samples_ms": [200, 100]}
    latest = {**baseline, "elapsed_seconds": 12, "frames": 10000}
    native = Measurements(10, native=baseline).summary(12, 0, latest)["native"]
    assert native["scene_update_recent_ms"] == {"p50": None, "p95": None, "p99": None}
    assert native["scene_update_timing_samples"] == 0
    assert native["scene_update_timing_samples_missing"] == 0
    assert native["scene_update_mean_ms"] is None


def test_native_ring_uses_only_post_boundary_tail_when_old_entries_remain():
    baseline = {"elapsed_seconds": 10, "frames": 100, "scene_updates": 8,
                "scene_update_seconds": .4, "scene_update_samples_ms": [200, 100]}
    latest = {**baseline, "elapsed_seconds": 12, "frames": 200,
              "scene_updates": 10, "scene_update_seconds": .5,
              "scene_update_samples_ms": [200, 100, 10, 90]}
    native = Measurements(10, native=baseline).summary(12, 2, latest)["native"]
    assert native["scene_update_recent_ms"] == pytest.approx({"p50": 50, "p95": 86, "p99": 89.2})
    assert native["scene_update_timing_samples"] == 2
    assert native["scene_update_timing_samples_missing"] == 0
    assert native["scene_update_mean_ms"] == pytest.approx(50)


def test_native_ring_loss_does_not_change_total_rate_or_mean_denominator():
    baseline = {"elapsed_seconds": 10, "frames": 100, "scene_updates": 20,
                "scene_update_seconds": .5}
    latest = {"elapsed_seconds": 20, "frames": 10100, "scene_updates": 220,
              "scene_update_seconds": 10.5, "scene_update_samples_ms": list(range(1, 121))}
    native = Measurements(10, native=baseline).summary(20, 200, latest)["native"]
    assert native["scene_update_timing_samples"] == 120
    assert native["scene_update_timing_samples_missing"] == 80
    assert native["scene_update_recent_ms"] == pytest.approx({"p50": 60.5, "p95": 114.05, "p99": 118.81})
    assert native["scene_update_mean_ms"] == 50  # 10 seconds / all 200 updates.
    assert native["counters"]["scene_updates"] == {"count": 200, "per_second": 20}


def test_mutation_cost_and_input_cost_are_not_folded_into_submission_timing():
    measured = Measurements(10.0)
    measured.mutation(0.002, 5)
    measured.mutation(0.004, 1)
    measured.collect([timing(1, 11.0, layout=.003, paint=.005, update=.010, routing=.020)])
    summary = measured.summary(12.0, 1)
    assert summary["mutation_ticks"] == 2
    assert summary["mutation_ticks_per_second"] == 1
    assert summary["controls_touched"] == 6
    assert summary["mutation_ms"]["p50"] == 3
    assert summary["timings_ms"]["python_layout"]["p50"] == 3
    assert summary["timings_ms"]["paint_and_submit"]["p50"] == 5
    assert summary["timings_ms"]["scene_update"]["p50"] == 10
    assert summary["timings_ms"]["input_routing"]["p50"] == 20


@pytest.mark.parametrize("name", tuple(PRESETS))
def test_fps_readout_names_backend_and_full_cycle_for_every_preset(name):
    controls = PRESETS[name]
    assert format_fps_readout(controls, None) == f"Preset {name} · Backend FPS — · Full cycle FPS —"
    shown = format_fps_readout(controls, {"backend_fps": 120, "full_cycle_fps": 36.25})
    assert shown == f"Preset {name} · Backend FPS 120.0 · Full cycle FPS 36.2"


def test_fps_readout_uses_the_control_count_when_it_is_not_a_preset():
    shown = format_fps_readout(17, {"backend_fps": 0, "full_cycle_fps": 8})
    assert shown == "17 controls · Backend FPS 0.0 · Full cycle FPS 8.0"


def test_relayout_reads_committed_values_instead_of_public_descriptors():
    """A 400-child grid used to cross the public property gateway thousands of times."""

    def run():
        root = Container(
            layout="grid", columns=20, spacing=0, padding=0,
            width=800, height=600, theme_override=dark(),
        )
        labels = []
        for index in range(400):
            label = Label(text=f"Item {index}")
            root.add(label)
            labels.append(label)

        class Host:
            size = (800, 600)

            def measure(self, text, size, mono=False):
                return len(text) * size * 0.5, size * 1.2

        host = Host()
        arrange(root, host)
        labels[0].text = "changed"
        reads = 0
        original = _Slot.__get__

        def counting(self, obj, owner=None):
            nonlocal reads
            if obj is not None:
                reads += 1
            return original(self, obj, owner)

        _Slot.__get__ = counting
        try:
            arrange(root, host)
        finally:
            _Slot.__get__ = original
        assert reads < 40

    call(run)
