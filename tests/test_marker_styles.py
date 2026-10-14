"""Custom choice markers accept partial surface styles on every drawing path."""

from dataclasses import replace

import pytest

from pysual import Painter, Rect, Style, Theme

from test_library import RecordingHost


BOUNDS = Rect(10, 20, 24, 24)
MARKER = Rect(1, 2, 20, 20)
ABSOLUTE = Rect(11, 22, 20, 20)
PARTIAL = Style(fill="#112233", border="#445566", border_width=1, radius=2)


class PixelHost(RecordingHost):
    def __init__(self):
        super().__init__()
        self.rects = []
        self.strokes = []

    def rect(self, rect, fill, radius=0, border="", border_width=1):
        self.rects.append((rect, fill, radius, border, border_width))

    def line(self, x1, y1, x2, y2, color, width=1):
        self.strokes.append((x1, y1, x2, y2, color, width))


class SemanticHost(PixelHost):
    def __init__(self, *, handled, retained):
        super().__init__()
        self.handled = handled
        self.native_retained = retained
        self.markers = []
        self.surfaces = []

    def marker(self, rect, style, **options):
        self.markers.append((rect, style, options))
        return self.handled

    def styled_rect(self, rect, style, **options):
        self.surfaces.append((rect, style, options))
        return False


def assert_pixels(host, theme, shape, checked):
    radius = 10 if shape == "circle" else 2
    assert host.rects[:2] == [
        (ABSOLUTE, PARTIAL.fill, radius, "", 1),
        (ABSOLUTE, "", radius, PARTIAL.border, 1),
    ]
    if checked and shape == "circle":
        assert host.rects[2:] == [
            (ABSOLUTE.inset(6), theme.tokens.foreground, 4, "", 1),
        ]
    else:
        assert len(host.rects) == 2
    if checked and shape == "square":
        assert host.strokes
        assert all(line[4] == theme.tokens.foreground for line in host.strokes)
    else:
        assert not host.strokes


@pytest.mark.parametrize("shape", ["square", "circle"])
@pytest.mark.parametrize("checked", [False, True])
def test_partial_marker_style_uses_pixel_fallback(shape, checked):
    host = PixelHost()
    theme = Theme()

    Painter(host, BOUNDS, theme).marker(MARKER, PARTIAL, shape=shape, checked=checked)

    assert_pixels(host, theme, shape, checked)


@pytest.mark.parametrize("shape", ["square", "circle"])
@pytest.mark.parametrize("checked", [False, True])
@pytest.mark.parametrize("handled", [False, True])
@pytest.mark.parametrize("retained", [False, True])
def test_partial_marker_style_preserves_semantic_host_contract(
    shape, checked, handled, retained
):
    host = SemanticHost(handled=handled, retained=retained)
    theme = Theme()

    Painter(host, BOUNDS, theme).marker(MARKER, PARTIAL, shape=shape, checked=checked)

    face = replace(PARTIAL, foreground=theme.tokens.foreground) if checked else PARTIAL
    clips = {"effect_clip": BOUNDS} if retained else {}
    assert host.markers == [(ABSOLUTE, face, {"shape": shape, "checked": checked, **clips})]
    # Fields unrelated to markers remain absent, even when a host handles paint.
    assert face.font_size is face.font_family is face.padding is None
    assert PARTIAL.foreground is None
    if handled:
        assert not host.surfaces
        assert not host.rects
        assert not host.strokes
    else:
        surface = replace(face, radius=10) if shape == "circle" else face
        assert host.surfaces == [(ABSOLUTE, surface, clips)]
        assert_pixels(host, theme, shape, checked)


@pytest.mark.parametrize("shape", ["square", "circle"])
def test_partial_marker_style_keeps_explicit_checked_foreground(shape):
    host = SemanticHost(handled=True, retained=False)
    style = replace(PARTIAL, foreground="#abcdef")

    Painter(host, BOUNDS, Theme()).marker(MARKER, style, shape=shape, checked=True)

    assert host.markers[0][1] == style
