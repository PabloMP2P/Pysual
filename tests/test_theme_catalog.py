"""The public catalog presents complete, readable light/dark families."""

import pytest

from pysual import Rect, Theme, dark, get_theme, light, theme_names
from pysual.painting import Painter
from test_library import RecordingHost


FAMILIES = (
    "modern", "windows", "macos", "terminal", "win31", "winxp",
    "macintosh", "analog_arcade", "neon",
)
CATALOG = tuple(name for family in FAMILIES for name in (family, family + "_dark"))


def luminance(color):
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return sum((c / 12.92 if c <= .04045 else ((c + .055) / 1.055) ** 2.4) * weight
               for c, weight in zip(channels, (.2126, .7152, .0722)))


def contrast(first, second):
    bright, dim = sorted((luminance(first), luminance(second)), reverse=True)
    return (bright + .05) / (dim + .05)


def test_catalog_has_exactly_nine_ordered_light_dark_pairs():
    assert theme_names() == CATALOG
    assert len({get_theme(name).to_json() for name in CATALOG}) == 18
    assert get_theme("modern") == light()
    assert get_theme("modern_dark") == dark()
    for name in CATALOG:
        assert get_theme(name.upper()) == get_theme(name)
    with pytest.raises(ValueError):
        get_theme("not_a_theme")


@pytest.mark.parametrize("family", FAMILIES)
def test_each_family_has_real_light_and_dark_surfaces(family):
    day, night = get_theme(family), get_theme(family + "_dark")
    assert luminance(day.tokens.background) > .45
    assert luminance(night.tokens.background) < .15
    assert luminance(day.tokens.surface) > .45
    assert luminance(night.tokens.surface) < .15
    for theme in (day, night):
        for backdrop in (theme.tokens.background, theme.tokens.surface):
            assert contrast(theme.tokens.foreground, backdrop) >= 4.5


@pytest.mark.parametrize("name", [
    "midnight", "daylight", "forest", "paper", "slate", "arcade",
    "glass", "classic", "blueprint", "dark", "light",
])
def test_removed_theme_names_are_rejected(name):
    assert name not in theme_names()
    with pytest.raises(ValueError, match="Unknown theme"):
        get_theme(name)
    with pytest.raises(ValueError, match="Unknown theme"):
        get_theme(name.upper())


@pytest.mark.parametrize("name", CATALOG)
def test_control_materials_render_after_serialization(name):
    class Host(RecordingHost):
        def __init__(self):
            super().__init__()
            self.rects = []

        def rect(self, *args, **kwargs):
            self.rects.append((args, kwargs))

    theme = get_theme(name)
    restored = Theme.from_json(theme.to_json())
    # A full control vocabulary, including focusable parts, must survive
    # transport to another backend without losing its material or state.
    parts = (("Button", "body"), ("TextBox", "body"), ("Toggle", "track"),
             ("Slider", "thumb"), ("ProgressBar", "fill"),
             ("SubWindow", "titlebar"), ("ListView", "row"),
             ("TabControl", "tab"), ("CheckBox", "check"))
    for kind, part in parts:
        for state in ("normal", "hover", "pressed", "disabled"):
            for selected in (False, True):
                before, after = Host(), Host()
                for material, host in ((theme, before), (restored, after)):
                    style = material.resolve(kind, part, state, checked=selected, selected=selected)
                    Painter(host, Rect(0, 0, 160, 44), material).surface(Rect(4, 4, 144, 32), style)
                assert before.rects, (name, kind, part, state)
                assert before.rects == after.rects, (name, kind, part, state, selected)


def test_family_identity_changes_geometry_and_materials_beyond_palette():
    parts = (("Button", "body"), ("TextBox", "body"), ("SubWindow", "titlebar"),
             ("Slider", "thumb"), ("Container", "body"))
    features = ("radius", "border_width", "bevel", "bevel_width", "pattern",
                "pattern_spacing", "shadow_blur", "shadow_x", "shadow_y",
                "glow_width", "font_family")
    signatures = []
    for family in FAMILIES:
        theme = get_theme(family)
        signatures.append(tuple(tuple(getattr(theme.resolve(kind, part), field)
                                      for field in features) for kind, part in parts))
    assert len(set(signatures)) == len(FAMILIES)
