"""Measure property updates, layout, cached painting and native transport.

Run from an installed development checkout: python tools/benchmark_core.py.
Prints one JSON object. Timings vary by machine; descriptor-read counts are
deterministic. An available native helper also enables hidden-window probes.
"""

from __future__ import annotations

import json
import time
from enum import Flag, IntFlag, auto

from _benchmark import metadata

from pysual import Container, Label
from pysual.geometry import Rect
from pysual.theme import dark
from pysual._engine import call
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.render_cache import RenderCache
from pysual.schema import _Slot


class _Host:
    capabilities = frozenset()
    size = (1200, 800)

    def __init__(self):
        self.frames = 0

    def measure(self, text, size, mono=False):
        return (len(text) * size * 0.5, size * 1.2)

    def begin(self, color):
        pass

    def present(self):
        self.frames += 1

    def clip(self, rect):
        pass

    def rect(self, *args, **kwargs):
        pass

    def text(self, *args, **kwargs):
        pass

    def line(self, *args, **kwargs):
        pass

    def image(self, *args, **kwargs):
        pass


def _percentile(samples, fraction):
    ordered = sorted(samples)
    index = (len(ordered) - 1) * fraction
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    weight = index - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _grid(count, columns=50):
    # Pin one theme. An unthemed tree rebuilds the default theme per control.
    root = Container(
        layout="grid", columns=columns, spacing=2, padding=0,
        width=1200, height=800, theme_override=dark(),
    )
    labels = []
    for index in range(count):
        label = Label(text=f"Item {index:04d}")
        root.add(label)
        labels.append(label)
    return root, labels


def _descriptor_reads(function):
    reads = 0
    original = _Slot.__get__

    def counting(self, obj, owner=None):
        nonlocal reads
        if obj is not None:
            reads += 1
        return original(self, obj, owner)

    _Slot.__get__ = counting
    try:
        function()
    finally:
        _Slot.__get__ = original
    return reads


def python_metrics():
    def run():
        root, labels = _grid(2000)
        host = _Host()
        samples = []
        for index in range(3000):
            started = time.perf_counter()
            labels[index % 2000].text = f"Value {index}"
            samples.append(time.perf_counter() - started)
        write_us = _percentile(samples, 0.5) * 1e6

        arrange(root, host)
        labels[0].text = "changed-once"
        def relayout():
            arrange(root, host)
        layout_reads = _descriptor_reads(relayout)
        layout_samples = []
        for _ in range(5):
            labels[0].text = f"changed-{time.perf_counter_ns()}"
            started = time.perf_counter()
            arrange(root, host)
            layout_samples.append((time.perf_counter() - started) * 1000)
        return {
            "scenario": {
                "backend": "synthetic measurement host", "controls": 2000,
                "size": list(host.size), "columns": 50,
                "property_write_warmup": 0, "property_write_samples": len(samples),
                "relayout_warmup": 1, "relayout_samples": len(layout_samples),
                "initial_arrange_excluded": True,
            },
            "property_write_samples_us": [sample * 1e6 for sample in samples],
            "relayout_samples_ms": layout_samples,
            "property_write_us_p50": round(write_us, 2),
            "grid_2000_relayout_ms_p50": round(_percentile(layout_samples, 0.5), 2),
            "grid_2000_relayout_descriptor_reads": layout_reads,
        }

    return call(run)


def paint_metrics():
    def run():
        from pysual.backends._web_svg import SVGRenderer
        from pysual.painting import RetainedPaintTree

        class Scene(SVGRenderer):
            capabilities = frozenset({
                "render_surfaces", "images", "image_fit", "scene_patches",
            })

            def present(self):
                self.publish_scene()

            def _resource_error(self, message):
                pass

        root, _labels = _grid(500, columns=25)
        host = Scene()
        host.reset_scene("bench", 1200, 800)
        host.size = (1200, 800)
        arrange(root, host)
        cache = RenderCache(host)
        retained = RetainedPaintTree(host)
        paint_tree(root, host, cache=cache, retained=retained)
        paint_tree(root, host, cache=cache, retained=retained)
        samples = []
        for _ in range(5):
            started = time.perf_counter()
            paint_tree(root, host, cache=cache, retained=retained)
            samples.append((time.perf_counter() - started) * 1000)
        stats = cache.stats
        web = {
            "scenario": {"backend": "SVGRenderer", "controls": 500,
                         "size": list(host.size), "columns": 25,
                         "warmup_frames": 2, "samples": len(samples)},
            "unchanged_frame_samples_ms": samples,
            "unchanged_frame_ms_p50": round(_percentile(samples, 0.5), 2),
            "revision": host._revision,
            "cache_hits": stats.hits,
            "cache_bypasses": stats.bypasses,
            "scene_patches": "scene_patches" in host.capabilities,
        }

        from pysual.backends.terminal import TerminalHost

        terminal = TerminalHost(renderer="python", hidden=True, color="none")
        terminal.open("bench", 1200, 800, False, 1)
        try:
            arrange(root, terminal)
            tcache = RenderCache(terminal)
            paint_tree(root, terminal, cache=tcache)
            paint_tree(root, terminal, cache=tcache)
            samples = []
            for _ in range(3):
                started = time.perf_counter()
                paint_tree(root, terminal, cache=tcache)
                samples.append((time.perf_counter() - started) * 1000)
            tstats = tcache.stats
            term = {
                "scenario": {"backend": "terminal", "renderer": "python",
                             "hidden": True, "color": "none", "controls": 500,
                             "size": list(terminal.size), "columns": 25,
                             "warmup_frames": 2, "samples": len(samples)},
                "unchanged_frame_samples_ms": samples,
                "unchanged_frame_ms_p50": round(_percentile(samples, 0.5), 2),
                "render_surfaces": "render_surfaces" in terminal.capabilities,
                "cache_hits": tstats.hits,
                "cache_bypasses": tstats.bypasses,
            }
        finally:
            terminal.close()
        return {"web_500": web, "terminal_python_500": term}

    return call(run)


def native_metrics():
    from pathlib import Path
    import os
    import sys

    package = Path(__file__).resolve().parents[1] / "src" / "pysual"
    exe = Path(os.environ.get(
        "PYSUAL_HOST",
        package / "bin" / ("pysual-host.exe" if sys.platform == "win32" else "pysual-host"),
    ))
    if not exe.is_file():
        return {"available": False, "skip_reason": f"Native helper not found: {exe}"}
    from pysual.backends.native import NativeHost

    def run():
        host = NativeHost(hidden=True, vsync=False, executable=exe)
        host.open("bench", 800, 600, False, 1)
        try:
            started = time.perf_counter()
            for index in range(2500):
                host.measure(f"label-{index:04d}-café", 16, False)
            cold_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            for index in range(2500):
                host.measure(f"label-{index:04d}-café", 16, False)
            warm_ms = (time.perf_counter() - started) * 1000
            host.begin("#20242d")
            host.rect(Rect(10, 10, 100, 40), "#52d9a1")
            host.text("Hello", 12, 20, "#ffffff", 16)
            started = time.perf_counter()
            host.present()
            present_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            host.present()
            present_again_ms = (time.perf_counter() - started) * 1000
            stats = host.native_stats()
            host.begin_scene("#101418")
            for index in range(80):
                host.begin_segment(f"c{index}", Rect(0, index * 4, 200, 4))
                host.rect(Rect(0, index * 4, 180, 3), "#336699")
                host.text(f"row {index}", 4, index * 4, "#ffffff", 12)
                host.end_segment()
            host.end_scene(order=[f"c{index}" for index in range(80)])
            host.begin_scene("#101418")
            for index in range(6):
                host.begin_segment(f"c{index}", Rect(0, index * 4, 200, 4))
                host.rect(Rect(0, index * 4, 180, 3), "#ffcc00")
                host.text("changed", 4, index * 4, "#000000", 12)
                host.end_segment()
            host.end_scene()
            segmented = host.native_stats()
            synced = NativeHost(hidden=True, vsync=True, executable=exe)
            synced.open("vsync", 640, 400, False, 1)
            try:
                synced.begin("#20242d")
                synced.rect(Rect(10, 10, 80, 30), "#52d9a1")
                synced.text("Hello", 12, 16, "#ffffff", 16)
                synced.present()
                vsync_samples = []
                for _ in range(4):
                    # Let the previous present finish so this measures the
                    # acknowledgement, not the wait behind an in-flight VSync.
                    time.sleep(0.04)
                    started = time.perf_counter()
                    synced.present()
                    vsync_samples.append((time.perf_counter() - started) * 1000)
                synced_info = dict(synced.info)
            finally:
                synced.close()
            return {
                "available": True,
                "scenario": {
                    "backend": "window", "renderer": host.info.get("renderer"),
                    "hidden": True, "size": list(host.size), "scale": 1,
                    "vsync_requested": False, "vsync_actual": host.info.get("vsync"),
                    "measure_calls_per_sample": 2500, "font_size": 16,
                    "measurement_warmup": 0, "cold_samples": 1, "warm_samples": 1,
                    "present_warmup": 0, "present_samples": 2,
                    "vsync_probe": {"requested": True, "actual": synced_info.get("vsync"),
                                    "renderer": synced_info.get("renderer"),
                                    "size": [640, 400], "warmup_frames": 1,
                                    "samples": len(vsync_samples), "idle_between_samples_s": 0.04},
                },
                "measurement_boundary": "Submission/acknowledgement; not GPU completion or display scanout",
                "measure_2500_cold_samples_ms": [cold_ms],
                "measure_2500_warm_samples_ms": [warm_ms],
                "present_samples_ms": [present_ms, present_again_ms],
                "vsync_present_samples_ms": vsync_samples,
                "vsync": host.info.get("vsync"),
                "measure_2500_cold_ms": round(cold_ms, 1),
                "measure_2500_warm_ms": round(warm_ms, 1),
                "metric_entries": len(host._metrics),
                "present_first_ms": round(present_ms, 2),
                "present_unchanged_ms": round(present_again_ms, 2),
                "vsync_present_ms_p50": round(_percentile(vsync_samples, 0.5), 2),
                "segments_tested": stats.get("last_scene_segments_tested"),
                "segment_count": stats.get("segment_count"),
                "disjoint_segments_tested": segmented.get("last_scene_segments_tested"),
                "disjoint_segment_count": segmented.get("segment_count"),
            }
        finally:
            host.close()

    return call(run)


def flag_probe():
    class Sample(IntFlag):
        NONE = 0
        A = auto()
        B = auto()

    inverted = ~Sample.A
    return {
        "intflag_invert": int(inverted),
        "intflag_and_int_type": type(Sample.A & 1).__name__,
        "member_type_is_class": type(Sample.A) is Sample,
        "flag_or_type": type(Flag).__name__,
    }


def main():
    report = {
        "metadata": metadata(),
        "flag": flag_probe(),
        "python": python_metrics(),
        "paint": paint_metrics(),
        "native": native_metrics(),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
