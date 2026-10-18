"""Fitting captions reuse metrics without changing elision or cache boundaries."""

from types import MethodType

import pytest

from pysual import Rect, dark
from pysual._text_layout import TextLayoutCache
from pysual.backends._web_svg import SVGRenderer
from pysual.painting import Painter


def metric_host(batched=False):
    host = SVGRenderer()
    host._text_measure_cache_limit = 0  # Synthetic metrics exercise the shared cache.
    calls, batches = [], []
    environment = {"factor": 1}

    def metric(text, size, mono=False):
        return len(text) * size / 2 * (2 if mono else 1) * environment["factor"], size

    def measure(text, size, mono=False):
        calls.append((text, size, mono))
        return metric(text, size, mono)

    def measure_many(texts, size, mono=False):
        texts = tuple(texts)
        batches.append((texts, size, mono))
        return [metric(text, size, mono) for text in texts]

    host.measure = measure
    if batched:
        host.measure_many = measure_many
    return host, calls, batches, environment


def painter(host):
    return Painter(host, Rect(0, 0, 300, 50), dark())


@pytest.mark.parametrize("batched", [False, True])
def test_cold_fitting_caption_resolves_metric_environment_and_font_once(batched, monkeypatch):
    host, calls, batches, _ = metric_host(batched)
    pen = painter(host)
    environments, faces = [], []
    prepare = host._text_layout_cache.prepare
    resolve = pen._type_size

    def counted_prepare(environment):
        environments.append(environment)
        return prepare(environment)

    def counted_resolve(**kwargs):
        faces.append(kwargs)
        return resolve(**kwargs)

    monkeypatch.setattr(host._text_layout_cache, "prepare", counted_prepare)
    monkeypatch.setattr(pen, "_type_size", counted_resolve)
    assert pen.elide("caption", 80, size=16) == "caption"
    assert calls == [("caption", 16, False)]
    assert batches == []
    assert len(environments) == len(faces) == 1
    assert host._text_layout_cache.get(("m", "caption", 16, False)) == (56, 16)


@pytest.mark.parametrize("batched", [False, True])
def test_fitting_caption_reuses_one_metric_across_widths(batched):
    host, calls, batches, _ = metric_host(batched)
    pen = painter(host)
    assert pen.measure("caption", size=16) == (56, 16)
    for width in (56, 80, 100, 160, 56):
        assert painter(host).elide("caption", width, size=16) == "caption"
    assert calls == [("caption", 16, False)]
    assert batches == []
    assert tuple(host._text_layout_cache._entries) == (("m", "caption", 16, False),)


@pytest.mark.parametrize("batched", [False, True])
def test_overflow_results_reuse_full_metric_and_width_specific_elisions(batched):
    host, calls, batches, _ = metric_host(batched)
    pen = painter(host)
    pen.measure("caption", size=16)
    assert pen.elide("caption", 32, size=16) == "cap…"
    assert pen.elide("caption", 24, size=16) == "ca…"
    before = len(calls), len(batches)
    for width, expected in ((32, "cap…"), (24, "ca…"), (56, "caption")):
        assert painter(host).elide("caption", width, size=16) == expected
    assert (len(calls), len(batches)) == before
    assert calls.count(("caption", 16, False)) == 1
    assert not any("caption" in texts for texts, _, _ in batches)
    assert ("e", "caption", 56, 16, False) not in host._text_layout_cache._entries


@pytest.mark.parametrize("batched", [False, True])
def test_cached_full_width_preserves_grapheme_boundary_truncation(batched):
    host, _, _, _ = metric_host(batched)
    pen = painter(host)
    text = "ae\u0301bc"
    assert pen.measure(text, size=16) == (40, 16)
    # A two-code-point prefix would split e + combining acute; keep only 'a'.
    assert pen.elide(text, 24, size=16) == "a…"
    assert pen.elide(text, 40, size=16) == text


@pytest.mark.parametrize("batched", [False, True])
def test_metric_reuse_isolated_by_font_size_and_family(batched):
    host, calls, _, _ = metric_host(batched)
    pen = painter(host)
    assert pen.elide("caption", 64, size=16, font_family="ui") == "caption"
    assert pen.elide("caption", 64, size=32, font_family="ui") == "cap…"
    assert pen.elide("caption", 64, size=16, font_family="mono") == "cap…"
    assert pen.elide("caption", 64, size=16, font_family="ui") == "caption"
    assert [call for call in calls if call[0] == "caption"] == [
        ("caption", 16, False), ("caption", 32, False), ("caption", 16, True),
    ]


@pytest.mark.parametrize("batched", [False, True])
def test_environment_change_expires_previously_fitting_width(batched):
    host, calls, _, environment = metric_host(batched)
    pen = painter(host)
    assert pen.elide("caption", 64, size=16) == "caption"
    environment["factor"] = 2
    host._resource_revision += 1
    assert pen.elide("caption", 64, size=16) == "cap…"
    assert calls.count(("caption", 16, False)) == 2


@pytest.mark.parametrize("batched", [False, True])
def test_cached_overflow_survives_eviction_of_its_full_metric(batched):
    host, calls, batches, _ = metric_host(batched)
    host._text_layout_cache.MAX_ENTRIES = 1
    pen = painter(host)
    assert pen.elide("caption", 32, size=16) == "cap…"
    assert tuple(host._text_layout_cache._entries) == (("e", "caption", 32, 16, False),)
    before = len(calls), len(batches)
    assert pen.elide("caption", 32, size=16) == "cap…"
    assert (len(calls), len(batches)) == before
    assert pen.elide("caption", 56, size=16) == "caption"
    assert tuple(host._text_layout_cache._entries) == (("m", "caption", 16, False),)


def test_oversized_cache_replacement_preserves_existing_result_and_budget():
    cache = TextLayoutCache()
    key = ("e", "caption", 32, 16, False)
    cache.store(key, "cap…")
    before = cache._bytes
    cache.store(key, "x" * (cache.MAX_TEXT + 1))
    assert cache.get(key) == "cap…"
    assert cache._bytes == before
    cache.store(("m", "x" * (cache.MAX_TEXT + 1), 16, False), (1, 1))
    assert cache._bytes == before
    cache.store(key, "ca…")
    assert cache.get(key) == "ca…"
    assert cache._bytes < before


@pytest.mark.parametrize("override", ["subclass", "instance", "class"])
def test_painter_measure_override_is_respected_after_metric_cache_warmup(override, monkeypatch):
    original = Painter.measure

    def wider(self, text, **kwargs):
        width, height = original(self, text, **kwargs)
        return width * 2, height

    class WiderPainter(Painter):
        measure = wider

    host, _, _, _ = metric_host()
    if override == "subclass":
        pen = WiderPainter(host, Rect(0, 0, 300, 50), dark())
    else:
        pen = painter(host)
        if override == "instance":
            monkeypatch.setattr(pen, "measure", MethodType(wider, pen))
        else:
            monkeypatch.setattr(Painter, "measure", wider)
    assert pen.measure("abcd", size=16) == (64, 16)
    assert pen.elide("abcd", 48, size=16) == "ab…"


@pytest.mark.parametrize("custom_first", [False, True])
def test_custom_painter_does_not_read_or_replace_ordinary_cached_elision(custom_first):
    class WiderPainter(Painter):
        def measure(self, text, **kwargs):
            width, height = super().measure(text, **kwargs)
            return width * 2, height

    host, _, _, _ = metric_host()
    ordinary = painter(host)
    custom = WiderPainter(host, Rect(0, 0, 300, 50), dark())
    ordered = ((custom, "a…"), (ordinary, "abc…")) if custom_first else (
        (ordinary, "abc…"), (custom, "a…"),
    )
    for pen, expected in (*ordered, *ordered):
        assert pen.elide("abcdef", 32, size=16) == expected
    assert host._text_layout_cache.get(("e", "abcdef", 32, 16, False)) == "abc…"
