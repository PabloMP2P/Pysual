"""Theme data remains strict; all effects use the ordinary shared Painter."""

import json
import unittest
from dataclasses import replace

from test_library import RecordingHost

from pysual import (
    App,
    Button,
    Container,
    Rect,
    Style,
    Theme,
    TextBox,
    dark,
    get_theme,
    registered_controls,
    theme_names,
)
from pysual.painting import Painter


class SurfaceThemeTests(unittest.TestCase):
    def test_secondary_card_text_contrasts_with_its_painted_surface(self):
        from pysual import ChoiceCard, Disclosure
        from pysual.painting import _style_kinds

        for theme in (Theme(), *(get_theme(name) for name in theme_names())):
            for control, text_part, surface_part in (
                (Disclosure, "summary", "header"),
                (ChoiceCard, "description", "body"),
            ):
                kinds = _style_kinds(control)
                for selected in (False, True):
                    for state in ("normal", "hover", "pressed"):
                        with self.subTest(theme=theme.tokens.background, control=control,
                                          selected=selected, state=state):
                            surface = theme.resolve(kinds, surface_part, state, selected=selected)
                            text = theme.resolve(kinds, text_part, state, selected=selected)
                            self.assert_text_contrast(text.foreground, surface)
                    disabled = theme.resolve(kinds, text_part, "disabled", selected=selected)
                    self.assertEqual(disabled.foreground, theme.tokens.muted)

    def test_hovered_choice_keeps_its_unselected_surface(self):
        from pysual import ChoiceCard
        from pysual.painting import _style_kinds

        kinds = _style_kinds(ChoiceCard)
        for name in theme_names():
            theme = get_theme(name)
            with self.subTest(theme=name):
                idle = theme.resolve(kinds)
                hovered = theme.resolve(kinds, state="hover")
                selected = theme.resolve(kinds, state="hover", checked=True, selected=True)
                self.assertEqual(hovered.fill, idle.fill)
                self.assertNotEqual(hovered.fill, selected.fill)

    def test_alert_keyboard_target_remains_visible_without_pointer_hover(self):
        from pysual import AlertBanner
        from pysual.painting import _style_kinds

        kinds = _style_kinds(AlertBanner)
        for theme in (Theme(), *(get_theme(name) for name in theme_names())):
            for part in ("action", "close"):
                with self.subTest(theme=theme.tokens.background, part=part):
                    idle = theme.resolve(kinds, part)
                    target = theme.resolve(kinds, part, selected=True)
                    self.assertEqual(idle.fill, "#00000000")
                    self.assertEqual(target.fill, theme.tokens.selection)
                    self.assertEqual(target.border, theme.tokens.accent)
                    self.assertEqual(target.border_width, 1)
                    self.assert_text_contrast(target.foreground, target)
                    disabled = theme.resolve(kinds, part, "disabled", selected=True)
                    self.assertEqual(disabled.foreground, theme.tokens.muted)

    def test_feedback_tones_are_readable_and_keep_each_themes_geometry(self):
        from pysual import AlertBanner, Badge, Meter
        from pysual.painting import _style_kinds

        for name in theme_names():
            theme = get_theme(name)
            for control in (Badge, AlertBanner):
                kinds = _style_kinds(control)
                for tone in ("info", "success", "warning", "danger"):
                    with self.subTest(theme=name, control=control.__name__, tone=tone):
                        surface = theme.resolve(kinds, tone)
                        self.assert_text_contrast(surface.foreground, surface)
                        self.assertNotEqual(surface.fill, theme.tokens.accent)
                        self.assertEqual(surface.radius == 0, theme.tokens.radius == 0)
                        disabled = theme.resolve(kinds, tone, "disabled")
                        self.assertEqual(disabled.foreground, theme.tokens.muted)
                        self.assertEqual(disabled.fill, theme.tokens.surface)
            meter = _style_kinds(Meter)
            self.assertNotEqual(theme.resolve(meter, "warning").fill,
                                theme.resolve(meter, "danger").fill)
        # Semantic color is resolved from current tokens, even after deriving
        # an existing catalog theme with its material rules intact.
        original = get_theme("modern_dark")
        custom = original.derive(danger="#ffbb88")
        self.assertNotEqual(original.resolve("Badge", "danger").fill,
                            custom.resolve("Badge", "danger").fill)

    def test_choice_cards_do_not_inherit_primary_button_gradients_or_elevation(self):
        from pysual import ChoiceCard
        from pysual.painting import _style_kinds

        kinds = _style_kinds(ChoiceCard)
        for name in theme_names():
            theme = get_theme(name)
            for selected in (False, True):
                for state in ("normal", "hover", "pressed"):
                    with self.subTest(theme=name, selected=selected, state=state):
                        style = theme.resolve(kinds, state=state, checked=selected,
                                              selected=selected)
                        self.assert_text_contrast(style.foreground, style)
                        self.assertEqual(style.shadow, "#00000000")
                        if selected:
                            self.assertEqual(style.border, theme.tokens.accent)
                            self.assertEqual(style.border_width, 2)
                            mark = theme.resolve(kinds, "check", state,
                                                 checked=True, selected=True)
                            self.assert_text_contrast(mark.foreground, mark)
            normal = theme.resolve(kinds)
            self.assertIn(normal.fill_end, (None, normal.fill))

    def test_rounded_feedback_badges_do_not_put_square_bevels_through_the_caption(self):
        from pysual import AlertBanner, Badge
        from pysual.painting import _style_kinds

        for name in theme_names():
            theme = get_theme(name)
            for tone in ("neutral", "info", "success", "warning", "danger"):
                with self.subTest(theme=name, tone=tone):
                    badge = theme.resolve(_style_kinds(Badge), tone)
                    if badge.radius:
                        self.assertIn(badge.bevel, (None, "none"))
        # Suppressing inappropriate pill bevels must not flatten the tactile
        # arcade panels that intentionally use a tactile material.
        arcade = get_theme("analog_arcade")
        self.assertEqual(arcade.resolve(_style_kinds(AlertBanner), "success").bevel, "sunken")

    def test_platform_fields_and_selection_match_distinct_surface_languages(self):
        from pysual import ListView, SegmentedControl
        from pysual.painting import _style_kinds

        for suffix in ("", "_dark"):
            windows, mac = get_theme("windows" + suffix), get_theme("macos" + suffix)
            field = windows.resolve("TextBox")
            self.assertNotEqual(field.border, field.border_end)
            self.assertEqual(windows.resolve("TextBox", state="hover").border_end,
                             windows.tokens.accent)
            self.assertIsNone(mac.resolve("TextBox").border_end)
            row = _style_kinds(ListView)
            self.assertEqual(windows.resolve(row, "row", selected=True).fill,
                             windows.tokens.selection)
            self.assertEqual(mac.resolve(row, "row", selected=True).fill,
                             mac.tokens.accent)
            segments = _style_kinds(SegmentedControl)
            selected = mac.resolve(segments, "segment", selected=True)
            self.assertNotEqual(selected.fill, selected.fill_end)
            self.assert_text_contrast(selected.foreground, selected)
        win31_title = get_theme("win31").resolve("SubWindow", "titlebar")
        self.assert_text_contrast(win31_title.foreground, win31_title)

    def test_platform_primary_secondary_and_elevation_roles_are_distinct(self):
        from pysual import Dropdown, Popup, Slider, Toggle
        from pysual.painting import _style_kinds

        for name in ("windows", "windows_dark", "macos", "macos_dark"):
            with self.subTest(theme=name):
                theme = get_theme(name)
                primary = theme.resolve(_style_kinds(Button))
                secondary = theme.resolve(_style_kinds(Dropdown))
                self.assertEqual(primary.fill, theme.tokens.accent)
                self.assertNotEqual(primary.fill, secondary.fill)
                self.assertLessEqual(primary.radius, 6)
                self.assertEqual(theme.resolve(_style_kinds(Container)).shadow, "#00000000")
                self.assertGreater(theme.resolve(_style_kinds(Popup)).shadow_blur, 10)
                for cls in (Button, Dropdown):
                    for state in ("normal", "hover", "pressed"):
                        style = theme.resolve(_style_kinds(cls), state=state)
                        self.assert_text_contrast(style.foreground, style)
                track = theme.resolve(_style_kinds(Toggle), "track")
                checked = theme.resolve(_style_kinds(Toggle), "track", checked=True)
                self.assertEqual(track.radius, 12)
                self.assertNotEqual(track.fill, checked.fill)
                thumb = theme.resolve(_style_kinds(Slider), "thumb")
                if name.startswith("windows"):
                    self.assertEqual(thumb.fill, theme.tokens.accent)
                    self.assertEqual(thumb.border_width, 3)
                else:
                    self.assertNotEqual(primary.fill, primary.fill_end)

    def test_material_themes_reach_the_shared_surface_renderer(self):
        class Host(RecordingHost):
            def __init__(self):
                super().__init__()
                self.surfaces = []

            def rect(self, *args, **kwargs):
                self.surfaces.append(args)

        painted = {}
        for name in theme_names():
            theme = get_theme(name)
            host = Host()
            style = theme.resolve("Button")
            Painter(host, Rect(0, 0, 160, 40), theme).surface(Rect(4, 4, 140, 32), style)
            painted[name] = host.surfaces
            self.assertTrue(host.surfaces)
            self.assert_text_contrast(style.foreground, style)
        # Raised pixel edges and a hard offset shadow are visible drawing calls,
        # rather than merely different catalog colors.
        win31 = get_theme("win31").resolve("Button")
        self.assertEqual(win31.radius, 0)
        self.assertEqual(win31.bevel, "raised")
        self.assertTrue(any(args[1] == win31.bevel_light for args in painted["win31"]))
        self.assertTrue(any(args[1] == win31.bevel_dark for args in painted["win31"]))
        macintosh = get_theme("macintosh").resolve("Button")
        self.assertEqual((macintosh.shadow_x, macintosh.shadow_y, macintosh.shadow_blur), (2, 2, 0))
        for name in ("macintosh", "analog_arcade"):
            self.assertEqual(get_theme(name).resolve("Button", state="hover").shadow_blur, 0)
        win31_theme = get_theme("win31")
        pressed = win31_theme.resolve("Button", state="pressed")
        self.assertEqual(pressed.fill, win31_theme.resolve("Button").fill)
        self.assertEqual(pressed.bevel, "sunken")
        neon = get_theme("neon").resolve("Button")
        self.assertGreater(neon.glow_width, 0)
        self.assertNotEqual(neon.fill, neon.fill_end)
        self.assertTrue(any(args[3] and args[3].startswith(neon.glow[:7])
                            for args in painted["neon"]))

    def test_platform_scrollbars_and_calendar_navigation_use_secondary_roles(self):
        from pysual import CalendarEditor, DataGrid, ListView, ScrollArea, TreeView
        from pysual.painting import _style_kinds, resolve_style

        for name in ("windows", "windows_dark", "macos", "macos_dark"):
            theme = get_theme(name)
            for cls in (ScrollArea, ListView, TreeView, DataGrid):
                for state in ("normal", "hover", "pressed", "disabled"):
                    with self.subTest(theme=name, control=cls.__name__, state=state):
                        track = theme.resolve(_style_kinds(cls), "track", state)
                        thumb = theme.resolve(_style_kinds(cls), "thumb", state)
                        self.assertNotEqual(thumb.fill, theme.tokens.accent)
                        self.assertNotEqual(thumb.fill, track.fill)
                        self.assertEqual(thumb.border, track.fill)
                        self.assertEqual((thumb.radius, thumb.border_width), (6, 3))
            calendar = CalendarEditor("2026-10-06", theme_override=theme)
            try:
                previous = resolve_style(calendar.previous_button)
                following = resolve_style(calendar.next_button)
                self.assertNotEqual(previous.fill, theme.resolve("Button").fill)
                self.assertEqual(previous.fill, theme.resolve("Dropdown").fill)
                self.assertEqual(previous, following)
                self.assert_text_contrast(previous.foreground, previous)
            finally:
                calendar.destroy()

    def test_editor_scrollbars_share_collection_materials(self):
        from pysual import ComboBox, ListView
        from pysual.painting import _style_kinds

        attributes = ("fill", "fill_end", "border", "border_width", "radius",
                      "bevel", "bevel_width", "pattern", "pattern_color")
        for name in theme_names():
            theme = get_theme(name)
            for part in ("track", "thumb"):
                for state in ("normal", "hover", "pressed", "disabled"):
                    expected = theme.resolve(_style_kinds(ListView), part, state)
                    for cls in (TextBox, ComboBox):
                        with self.subTest(theme=name, control=cls.__name__, part=part, state=state):
                            actual = theme.resolve(_style_kinds(cls), part, state)
                            self.assertEqual(
                                tuple(getattr(actual, field) for field in attributes),
                                tuple(getattr(expected, field) for field in attributes),
                            )

    def test_navigation_surfaces_preserve_selection_and_clear_fallback_effects(self):
        from pysual import Breadcrumb, Pagination, SegmentedControl
        from pysual.painting import _style_kinds

        for name in theme_names():
            theme = get_theme(name)
            for cls, part in ((SegmentedControl, "segment"), (Pagination, "page")):
                kinds = _style_kinds(cls)
                for state in ("normal", "hover", "pressed"):
                    with self.subTest(theme=name, control=cls.__name__, state=state):
                        style = theme.resolve(kinds, part, state, selected=True)
                        self.assert_text_contrast(style.foreground, style)
                body = theme.resolve(kinds)
                self.assertIn(body.shadow, (None, "#00000000"))
            for cls in (Breadcrumb, Pagination):
                for state in ("normal", "hover", "pressed", "disabled"):
                    body = theme.resolve(_style_kinds(cls), state=state)
                    self.assertEqual(body.fill, "#00000000", (name, cls, state))
                    self.assertIn(body.fill_end, (None, "#00000000"))
            current = theme.resolve(_style_kinds(Breadcrumb), "item", selected=True)
            self.assert_text_contrast(current.foreground, Style(fill=theme.tokens.surface))
            ancestor = theme.resolve(_style_kinds(Breadcrumb), "item")
            for backdrop in (theme.tokens.background, theme.tokens.surface):
                self.assert_text_contrast(ancestor.foreground, Style(fill=backdrop))

    def test_detached_fallback_is_shared_without_overriding_inherited_themes(self):
        parent = Container()
        self.addCleanup(parent.destroy)
        first, second = TextBox(parent=parent), TextBox(parent=parent)
        fallback = first.effective_theme
        first.select_all()
        first.replace_selection("Prepared before attachment")
        self.assertIs(first.effective_theme, fallback)
        self.assertIs(second.effective_theme, fallback)
        self.assertEqual(fallback, dark())
        self.assertIsNot(dark(), dark())
        derived = fallback.derive(font_size=fallback.tokens.font_size + 2)
        self.assertIsNot(derived._resolved_styles, fallback._resolved_styles)
        parent.theme_override = derived
        self.assertIs(first.effective_theme, derived)
        inherited = get_theme("modern")
        app = App(theme=inherited)
        self.addCleanup(app.destroy)
        app.add(parent)
        self.assertIs(first.effective_theme, derived)
        parent.theme_override = None
        self.assertIs(first.effective_theme, inherited)
        self.assertIs(second.effective_theme, inherited)

    def assert_text_contrast(self, foreground, style, minimum=4.5):
        def luminance(color):
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            return sum((c / 12.92 if c <= 0.04045 else ((c + .055) / 1.055) ** 2.4) * weight
                       for c, weight in zip(channels, (.2126, .7152, .0722)))

        start, end = style.fill, style.fill_end or style.fill
        for ratio in (0, .25, .5, .75, 1):
            color = "#" + "".join(
                f"{round(int(start[i:i + 2], 16) * (1 - ratio) + int(end[i:i + 2], 16) * ratio):02x}"
                for i in (1, 3, 5)
            )
            light, dark = sorted((luminance(color), luminance(foreground)), reverse=True)
            self.assertGreaterEqual((light + .05) / (dark + .05), minimum, (foreground, color))

    def test_links_captions_and_checked_marks_have_readable_theme_contrast(self):
        from pysual import CheckBox, Hyperlink, Label, RadioButton
        from pysual.painting import _style_kinds

        for name in theme_names():
            theme = get_theme(name)
            with self.subTest(theme=name):
                for state in ("normal", "hover", "pressed"):
                    link = theme.resolve(_style_kinds(Hyperlink), state=state)
                    for background in (theme.tokens.background, theme.tokens.surface):
                        self.assert_text_contrast(link.foreground, Style(fill=background))
                if name in ("modern", "modern_dark"):
                    self.assertEqual(theme.resolve(_style_kinds(Hyperlink), state="disabled").foreground,
                                     theme.tokens.muted)
                for cls in (Label, CheckBox, RadioButton):
                    caption = theme.resolve(_style_kinds(cls))
                    self.assert_text_contrast(caption.foreground, Style(fill=theme.tokens.background))
        mac = get_theme("macos_dark")
        for cls in (CheckBox, RadioButton):
            for state in ("normal", "hover", "pressed"):
                mark = mac.resolve(_style_kinds(cls), "check", state=state, checked=True)
                self.assert_text_contrast(mark.foreground, mark, minimum=3)
                self.assertEqual(mark.fill, mac.tokens.accent)

    def test_selected_list_and_menu_rows_have_readable_text(self):
        from pysual import ListView
        from pysual.menus import _MenuRows
        from pysual.painting import _style_kinds

        for name in theme_names():
            for control in (ListView, _MenuRows):
                for state in ("normal", "hover", "pressed"):
                    with self.subTest(theme=name, control=control.__name__, state=state):
                        style = get_theme(name).resolve(
                            _style_kinds(control), "row", state, selected=True
                        )
                        self.assert_text_contrast(style.foreground, style)

    def test_validation_text_contrasts_with_popup_surfaces(self):
        from pysual.editors import _ValuePopup
        from pysual.painting import _style_kinds

        for name in theme_names():
            with self.subTest(theme=name):
                theme = get_theme(name)
                self.assert_text_contrast(
                    theme.tokens.danger, theme.resolve(_style_kinds(_ValuePopup))
                )

    def test_progress_text_contrasts_with_track_and_fill_gradients(self):
        def luminance(color):
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            return sum((c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4) * weight
                       for c, weight in zip(channels, (0.2126, 0.7152, 0.0722)))

        for name in theme_names():
            for part in ("track", "fill"):
                style = get_theme(name).resolve(("Control", "ProgressBar"), part)
                start, end = style.fill, style.fill_end or style.fill
                for ratio in (0, .25, .5, .75, 1):
                    color = "#" + "".join(
                        f"{round(int(start[i:i + 2], 16) * (1 - ratio) + int(end[i:i + 2], 16) * ratio):02x}"
                        for i in (1, 3, 5)
                    )
                    light, dark = sorted((luminance(color), luminance(style.foreground)), reverse=True)
                    with self.subTest(theme=name, part=part, ratio=ratio):
                        self.assertGreaterEqual((light + .05) / (dark + .05), 4.5)

    def test_check_boxes_stay_rounded_squares_and_radio_checks_stay_circles(self):
        from pysual import CheckBox, RadioButton, dark, light
        from pysual.painting import _style_kinds
        from pysual.theme import CHECK_BOX_RADIUS

        themes = [get_theme(name) for name in theme_names()] + [dark(), light()]
        for theme in themes:
            for state in ("normal", "hover", "pressed", "disabled"):
                for checked in (False, True):
                    with self.subTest(tokens=theme.tokens.accent, state=state, checked=checked):
                        box = theme.resolve(_style_kinds(CheckBox), "check", state, checked)
                        self.assertLessEqual(box.radius, CHECK_BOX_RADIUS)
                        radio = theme.resolve(_style_kinds(RadioButton), "check", state, checked)
                        self.assertEqual(radio.radius, 10)

    def test_slider_and_toggle_thumbs_are_distinct_from_their_surroundings(self):
        from pysual import Slider, Toggle
        from pysual.painting import _style_kinds

        for name in theme_names():
            theme = get_theme(name)
            for control in (Slider, Toggle):
                kinds = _style_kinds(control)
                thumb = theme.resolve(kinds, "thumb")
                track = theme.resolve(kinds, "track")
                body = theme.resolve(kinds, "body")
                with self.subTest(theme=name, control=control.__name__):
                    visible_fill = thumb.fill not in (track.fill, body.fill, theme.tokens.surface)
                    outlined = bool(thumb.border) and (thumb.border_width or 0) > 0
                    self.assertTrue(visible_fill or outlined, thumb)

    def test_pressed_buttons_pair_selection_with_readable_foreground(self):
        def luminance(color):
            channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [
                c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                for c in channels
            ]
            return sum(
                c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722))
            )

        for name in theme_names():
            style = get_theme(name).resolve(
                ("Control", "Label", "Button"), state="pressed"
            )
            light, dark = sorted(
                (luminance(style.fill), luminance(style.foreground)), reverse=True
            )
            self.assertGreaterEqual((light + 0.05) / (dark + 0.05), 4.5, name)

    def test_disabled_slider_and_toggle_parts_remain_visible_and_distinct(self):
        for name in theme_names():
            theme = get_theme(name)
            for kinds, part, checked in (
                (("Control", "Slider"), "fill", False),
                (("Control", "Slider"), "thumb", False),
                (("Control", "Label", "Button", "CheckBox", "Toggle"), "track", True),
                (("Control", "Label", "Button", "CheckBox", "Toggle"), "thumb", True),
            ):
                with self.subTest(theme=name, kinds=kinds, part=part):
                    normal = theme.resolve(kinds, part, checked=checked)
                    disabled = theme.resolve(
                        kinds, part, state="disabled", checked=checked
                    )
                    self.assertNotEqual(disabled.fill, normal.fill)
                    if part == "thumb":
                        track = theme.resolve(
                            kinds, "track", state="disabled", checked=checked
                        )
                        self.assertNotEqual(disabled.fill, track.fill)

    def test_painter_defaults_to_control_typography(self):
        from pysual import Control

        class Badge(Control):
            def paint(self, painter):
                painter.text("Badge", 2, 3)

        class Host(RecordingHost):
            def text(self, *args):
                self.drawn = args

            def measure(self, text, size, mono=False):
                self.measured = (text, size, mono)
                return super().measure(text, size, mono)

        theme = Theme().styled(
            Control, Style(font_size=31, foreground="#123456", font_family="mono")
        )
        badge = Badge(theme_override=theme)
        host = Host()
        try:
            painter = Painter(host, Rect(7, 11, 140, 40), theme, badge)
            badge.paint(painter)
            self.assertEqual(host.drawn, ("Badge", 9, 14, "#123456", 31, True))
            painter.measure("Badge")
            self.assertEqual(host.measured, ("Badge", 31, True))
            self.assertEqual(painter.elide("Badge", 60), "Ba…")

            badge.font_size, badge.foreground, badge.font_family = 23, "#654321", "ui"
            painter = Painter(host, badge.bounds, theme, badge)
            badge.paint(painter)
            self.assertEqual(host.drawn[3:], ("#654321", 23, False))
            painter.measure("Badge")
            self.assertEqual(host.measured, ("Badge", 23, False))
            painter.text("Badge", 0, 0, color="#abcdef", size=12, mono=True)
            self.assertEqual(host.drawn[3:], ("#abcdef", 12, True))
            painter.measure("Badge", size=12, mono=True)
            self.assertEqual(host.measured, ("Badge", 12, True))
        finally:
            badge.destroy()

    def test_standalone_painter_keeps_token_defaults(self):
        class Host(RecordingHost):
            def text(self, *args):
                self.drawn = args

        theme = Theme().derive(font_size=19, foreground="#123456", font_family="mono")
        host = Host()
        painter = Painter(host, Rect(0, 0, 100, 40), theme)
        painter.text("Text", 0, 0)
        self.assertEqual(host.drawn, ("Text", 0, 0, "#123456", 19, True))
        self.assertEqual(painter.measure("Text"), (4 * 19 * 0.6, 19 * 1.2))

    def test_catalog_rules_target_real_parts_and_roundtrip_after_resolution(self):
        classes = {c.__name__: c for c in registered_controls().values()}
        for name in theme_names():
            theme = get_theme(name)
            for rule in theme.rules:
                self.assertIn(
                    rule.part, classes[rule.control].style_parts, (name, rule)
                )
            theme.resolve(("Control", "Label", "Button"), state="hover")
            document = theme.to_json()
            self.assertNotIn("_rule_index", document)
            self.assertEqual(Theme.from_json(document), theme)
        # Existing documents omit new optional effect fields and still load.
        old = json.loads(Theme().styled(Button, Style(fill="#123456")).to_json())
        old["rules"][0]["style"] = {"fill": "#123456"}
        self.assertEqual(
            Theme.from_json(json.dumps(old)).resolve("Button").fill, "#123456"
        )

    def test_effect_validation_and_state_inheritance(self):
        for data in (
            {"fill_end": "red"},
            {"border_end": []},
            {"gradient_axis": "diagonal"},
            {"bevel": "code()"},
            {"bevel_width": 17},
            {"glow_width": float("inf")},
            {"glow_width": True},
            {"pattern": "script"},
            {"pattern_spacing": 0},
            {"shadow": "black"},
            {"shadow_blur": -1},
            {"shadow_blur": 25},
            {"shadow_x": -25},
            {"shadow_y": True},
        ):
            with self.subTest(data=data), self.assertRaises((ValueError, TypeError)):
                Style(**data)
        theme = get_theme("neon")
        theme.resolve("Button")  # Compile the immutable lookup.
        changed = theme.styled(
            Button, Style(bevel="sunken", glow_width=0), state="pressed"
        )
        normal, pressed = (
            changed.resolve("Button"),
            changed.resolve("Button", state="pressed"),
        )
        self.assertEqual(pressed.fill_end, normal.fill_end)
        self.assertEqual(pressed.bevel, "sunken")
        self.assertNotEqual(theme.resolve("Button", state="pressed").bevel, "sunken")
        # Modern checked thumbs remain distinct from their selected tracks.
        for name in ("modern_dark", "modern"):
            theme = get_theme(name)
            kinds = ("Control", "Label", "Button", "CheckBox", "Toggle")
            self.assertNotEqual(
                theme.resolve(kinds, "thumb", checked=True).fill,
                theme.resolve(kinds, "track", checked=True).fill,
            )

    def test_huge_surface_effects_are_bounded_and_restore_clip_on_failure(self):
        class Host(RecordingHost):
            render_scale = 1.5

            def __init__(self):
                super().__init__()
                self.rects = 0
                self.borders = []
                self.clipped = None
                self.fail = False

            def clip(self, rect):
                self.clipped = rect

            def rect(self, *args, **kwargs):
                self.rects += 1
                self.borders.append(args[3])
                if self.fail:
                    raise RuntimeError("draw failure")

        host = Host()
        bounds = Rect(7.25, 10.5, 1_000_000, 1_000_000)
        painter = Painter(host, bounds, Theme())
        style = Style(
            fill="#004466",
            fill_end="#aaddff",
            border="#778899",
            border_end="#ffccaa",
            border_width=2,
            pattern="grid",
            pattern_spacing=2,
            pattern_color="#ffffff30",
            glow="#336699",
            glow_width=16,
        )
        painter.surface(Rect(0, 0, bounds.width, bounds.height), style)
        self.assertLessEqual(host.rects, 96 * 2 + 128 * 2 + 4 + 33)
        # Reducing glow opacity must preserve its color, not tint it toward black.
        self.assertIn("#33669929", host.borders)
        self.assertEqual(host.clipped, bounds)
        host.fail = True
        with self.assertRaisesRegex(RuntimeError, "draw failure"):
            painter.surface(Rect(0, 0, 100, 30), replace(style, pattern="none"))
        self.assertEqual(host.clipped, bounds)

    def test_resolved_styles_are_bounded_and_derived_themes_are_independent(self):
        from pysual import Container
        from pysual.painting import resolve_style

        theme = get_theme("windows")
        self.assertIs(theme.resolve("Button"), theme.resolve("Button"))
        self.assertNotEqual(
            theme.resolve("Button").shadow,
            theme.resolve("Button", state="pressed").shadow,
        )
        changed = theme.styled(Button, Style(fill="#123456"))
        self.assertEqual(changed.resolve("Button").fill, "#123456")
        self.assertNotEqual(theme.resolve("Button").fill, "#123456")
        for i in range(1100):
            theme.resolve(f"Custom{i}")
        self.assertLessEqual(len(theme._resolved_styles), 1024)
        self.assertEqual(Theme.from_json(theme.to_json()), theme)
        parent = Container(theme_override=theme)
        child = Button(parent=parent)
        try:
            self.assertEqual(resolve_style(child).fill, theme.resolve("Button").fill)
            child.background = "#f1a2b3"
            self.assertEqual(resolve_style(child).fill, "#f1a2b3")
            child.background = None
            parent.theme_override = changed
            self.assertEqual(resolve_style(child).fill, "#123456")
            self.assertNotEqual(
                get_theme("macos").resolve("Button").radius,
                get_theme("windows").resolve("Button").radius,
            )
        finally:
            parent.destroy()
