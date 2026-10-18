"""SVG uses the real bundled-font LRU and retains only useful shared entries."""

from types import MethodType

import pytest

from pysual import Rect, dark
from pysual.backends import _font
from pysual.backends._web_svg import SVGRenderer
from pysual.painting import Painter


@pytest.fixture(autouse=True)
def reset_metric_lru():
    _font._measure_cached.cache_clear()
    yield
    _font._measure_cached.cache_clear()


def painter(host):
    return Painter(host, Rect(0, 0, 300, 50), dark())


@pytest.mark.parametrize("text", ["AVATAR café e\u0301", "x" * _font.MEASURE_CACHE_TEXT_LIMIT])
def test_fitting_captions_use_font_lru_without_shared_cache_preparation(text, monkeypatch):
    host = SVGRenderer()
    pen = painter(host)

    def unexpected_prepare(_environment):
        raise AssertionError("A fitting SVG caption should use its font cache directly")

    monkeypatch.setattr(host._text_layout_cache, "prepare", unexpected_prepare)
    measured = pen.measure(text, size=16)
    assert _font._measure_cached.cache_info().misses == 1
    assert pen.measure(text, size=16) == measured
    for width in (measured[0], measured[0] + 10, measured[0] + 100):
        assert pen.elide(text, width, size=16) == text
    info = _font._measure_cached.cache_info()
    assert info.misses == 1 and info.hits == 4
    assert not host._text_layout_cache._entries


def test_overflow_reuses_font_metrics_and_shared_fitted_result():
    host = SVGRenderer()
    pen = painter(host)
    text = "abcdefghijklmno"
    width = _font.measure("abcd…", 16, True)[0]
    assert pen.elide(text, width, size=16, font_family="mono") == "abcd…"
    key = ("e", text, width, 16, True)
    assert host._text_layout_cache.get(key) == "abcd…"
    assert not any(key[0] == "m" for key in host._text_layout_cache._entries)
    before = _font._measure_cached.cache_info()
    assert painter(host).elide(text, width, size=16, font_family="mono") == "abcd…"
    after = _font._measure_cached.cache_info()
    assert after.misses == before.misses
    assert after.hits == before.hits + 1  # Only the cached full width is consulted.


def test_text_above_font_lru_limit_keeps_shared_measurement_cache(monkeypatch):
    host = SVGRenderer()
    pen = painter(host)
    text = "x" * (_font.MEASURE_CACHE_TEXT_LIMIT + 1)
    calls = []
    measure = host.measure

    def counted(*args):
        calls.append(args)
        return measure(*args)

    monkeypatch.setattr(host, "measure", counted)
    expected = pen.measure(text, size=16)
    assert pen.measure(text, size=16) == expected
    assert pen.elide(text, expected[0], size=16) == text
    assert len(calls) == 1
    assert host._text_layout_cache.get(("m", text, 16, False)) == expected
    info = _font._measure_cached.cache_info()
    assert info.hits == info.misses == 0


def test_resource_change_refreshes_overflow_using_updated_host_metrics(monkeypatch):
    host = SVGRenderer()
    pen = painter(host)
    text = "abcdefghijklmno"
    width = _font.measure("abcde…", 16, True)[0]
    assert pen.elide(text, width, size=16, font_family="mono") == "abcde…"
    original = host.measure

    def doubled(*args):
        measured_width, height = original(*args)
        return measured_width * 2, height

    monkeypatch.setattr(host, "measure", doubled)
    host._resource_revision += 1
    assert pen.elide(text, width, size=16, font_family="mono") == "ab…"
    assert host._text_layout_cache.get(("e", text, width, 16, True)) == "ab…"


@pytest.mark.parametrize("override", ["subclass", "instance", "class"])
def test_real_svg_font_cache_preserves_painter_measure_overrides(override, monkeypatch):
    original = Painter.measure

    def doubled(self, text, **kwargs):
        width, height = original(self, text, **kwargs)
        return width * 2, height

    class WiderPainter(Painter):
        measure = doubled

    host = SVGRenderer()
    if override == "subclass":
        pen = WiderPainter(host, Rect(0, 0, 300, 50), dark())
    else:
        pen = painter(host)
        if override == "instance":
            monkeypatch.setattr(pen, "measure", MethodType(doubled, pen))
        else:
            monkeypatch.setattr(Painter, "measure", doubled)
    width = _font.measure("ab…", 16, True)[0] * 2
    assert pen.measure("abcd", size=16, font_family="mono")[0] > width
    assert pen.elide("abcd", width, size=16, font_family="mono") == "ab…"
    assert not any(key[0] == "e" for key in host._text_layout_cache._entries)
