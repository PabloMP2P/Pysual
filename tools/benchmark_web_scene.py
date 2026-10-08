"""Measure sparse SVG updates without browser, font or native dependencies.

Run with ``python tools/benchmark_web_scene.py`` from an installed checkout.
Each scene contains 2,000 detailed retained controls; one control changes per
frame. Timings include scene composition, publishing and transport encoding.
"""

from __future__ import annotations

import json
from statistics import median
from time import perf_counter

from _benchmark import metadata

from pysual.backends._web_svg import SVGRenderer
from pysual.geometry import Rect


def sparse_scene(*, images=False, count=2000, iterations=80):
    scene = SVGRenderer()
    source = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

    def control(index, value):
        bounds = Rect(index % 40 * 24, index // 40 * 20, 24, 20)
        scene.begin_segment(index, bounds)
        scene.clip(bounds)
        for layer in range(8):
            scene.rect(bounds.inset(layer / 4), "#19263c", 4, "#62759580")
        scene.rect(bounds.inset(3), value, 3)
        if images and index % 10 == 0:
            scene.image(source, bounds.inset(5))
        scene.end_segment()

    scene.begin_scene("#0c1220")
    for index in range(count):
        control(index, "#428cff")
    scene.end_scene(order=range(count))
    initial_bytes = len(json.dumps(scene.frame_packet(-1)))
    samples, packet_bytes = [], []
    for iteration in range(iterations + 10):
        revision = scene._revision
        started = perf_counter()
        scene.begin_scene("#0c1220")
        control(iteration % count, "#5ce0aa")
        scene.end_scene()
        packet = scene.frame_packet(revision)
        encoded = json.dumps(packet)
        elapsed = (perf_counter() - started) * 1000
        assert len(packet["updates"]) == 1
        assert not packet["image_sources"]
        if iteration >= 10:
            samples.append(elapsed)
            packet_bytes.append(len(encoded))
    return {
        "controls": count,
        "images": images,
        "backend": "SVGRenderer", "size": list(scene.size), "columns": 40, "cell_size": [24, 20],
        "warmup_iterations": 10, "samples": len(samples),
        "measurement_boundary": "Python scene composition, publishing and JSON encoding; no browser paint",
        "sparse_update_samples_ms": samples, "sparse_packet_samples_bytes": packet_bytes,
        "sparse_update_ms_p50": round(median(samples), 3),
        "sparse_update_ms_p95": round(sorted(samples)[int(len(samples) * .95)], 3),
        "initial_packet_bytes": initial_bytes,
        "sparse_packet_bytes_p50": median(packet_bytes),
    }


if __name__ == "__main__":
    print(json.dumps({"metadata": metadata(), "plain": sparse_scene(), "images": sparse_scene(images=True)}, indent=2))
