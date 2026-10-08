"""Theme selectors compose independently of concrete-control inheritance."""

from typing import ClassVar

from _ui_testcase import UIOwnerTestCase

from pysual import (
    Button, CheckBox, Control, Hyperlink, Label, PressBehavior, RadioButton,
    Style, TextBox, Theme, Toggle,
)
from pysual.errors import SchemaError
from pysual.host import Input
from pysual.painting import _effect_parts, _style_kinds, resolve_style
from pysual.theme import Rule


class ComposedAction(Control):
    style_fallbacks: ClassVar[tuple[str, ...]] = ("Button",)

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior()

    def handle_input(self, event, /):
        self._press.handle_input(self, event)

    def destroy(self):
        self._press.reset(self)
        super().destroy()


class ComposedField(Control):
    style_fallbacks: ClassVar[tuple[str, ...]] = ("TextBox",)


class StyleCompositionTests(UIOwnerTestCase):
    def control(self, kind, **properties):
        owner = kind(**properties)
        self.addCleanup(owner.destroy)
        return owner

    def test_independent_pressed_control_reuses_button_theme_and_state_rules(self):
        theme = (Theme()
                 .styled(Button, Style(fill="#123456", padding=17))
                 .styled(Button, Style(fill="#654321"), state="pressed"))
        owner = self.control(ComposedAction, theme_override=theme)
        self.assertNotIsInstance(owner, Button)
        self.assertEqual(resolve_style(owner).fill, "#123456")
        self.assertEqual(resolve_style(owner).padding, 17)
        owner.handle_input(Input("key_down", key="Space"))
        self.assertEqual(resolve_style(owner).fill, "#654321")
        owner.handle_input(Input("key_up", key="Space"))
        self.assertEqual(resolve_style(owner).fill, "#123456")

    def test_field_fallback_preserves_field_defaults_and_state_treatment(self):
        theme = Theme().styled(TextBox, Style(border="#123456"))
        owner = self.control(ComposedField, theme_override=theme)
        field = self.control(TextBox, theme_override=theme)
        self.assertNotIsInstance(owner, TextBox)
        for state in ("normal", "hover", "pressed", "disabled"):
            with self.subTest(state=state):
                self.assertEqual(resolve_style(owner, state=state),
                                 resolve_style(field, state=state))

    def test_specific_rules_override_fallbacks_without_changing_state_precedence(self):
        # Rule tuple order does not decide specificity: class selectors do.
        theme = Theme(rules=(
            Rule("ComposedAction", style=Style(fill="#112233", padding=23)),
            Rule("ComposedAction", state="pressed", style=Style(fill="#445566")),
            Rule("Button", style=Style(fill="#778899", foreground="#abcdef")),
            Rule("Button", state="hover", style=Style(fill="#aabbcc")),
        ))
        owner = self.control(ComposedAction, theme_override=theme)
        normal = resolve_style(owner)
        self.assertEqual((normal.fill, normal.foreground, normal.padding),
                         ("#112233", "#abcdef", 23))
        self.assertEqual(resolve_style(owner, state="hover").fill, "#aabbcc")
        self.assertEqual(resolve_style(owner, state="pressed").fill, "#445566")
        self.assertIs(resolve_style(owner), normal)
        derived = theme.derive(font_size=19)
        owner.theme_override = derived
        self.assertEqual(resolve_style(owner).font_size, 19)
        self.assertIsNot(derived._resolved_styles, theme._resolved_styles)
        owner.theme_override = theme
        self.assertIs(resolve_style(owner), normal)

    def test_subclasses_inherit_fallbacks_and_add_selectors_in_mro_order(self):
        class SpecializedAction(ComposedAction):
            style_fallbacks = ("SharedAccent", "Button")

        class FurtherAction(SpecializedAction):
            pass

        self.assertEqual(_style_kinds(FurtherAction), (
            "Control", "Button", "ComposedAction", "SharedAccent",
            "SpecializedAction", "FurtherAction",
        ))
        theme = Theme(rules=(
            Rule("Button", style=Style(padding=18, foreground="#112233")),
            Rule("SharedAccent", style=Style(foreground="#445566")),
            Rule("SpecializedAction", style=Style(fill="#778899")),
            Rule("FurtherAction", style=Style(fill="#aabbcc")),
        ))
        owner = self.control(FurtherAction, theme_override=theme)
        style = resolve_style(owner)
        self.assertEqual((style.padding, style.foreground, style.fill),
                         (18, "#445566", "#aabbcc"))
        self.assertNotIn("style_fallbacks", owner.properties())
        self.assertNotIn("style_excludes", owner.properties())

    def test_controls_without_composition_metadata_keep_the_default_mro_cascade(self):
        class CustomButton(Button):
            pass

        self.assertEqual(_style_kinds(CustomButton),
                         ("Control", "Label", "Button", "CustomButton"))
        theme = (Theme()
                 .styled(Label, Style(font_size=21))
                 .styled(Button, Style(fill="#112233"))
                 .styled(CustomButton, Style(fill="#445566")))
        owner = self.control(CustomButton, theme_override=theme)
        self.assertEqual((resolve_style(owner).font_size, resolve_style(owner).fill),
                         (21, "#445566"))

    def test_checkbox_and_hyperlink_descendants_keep_label_styles_without_button_chrome(self):
        class CustomLink(Hyperlink):
            pass

        theme = (Theme()
                 .styled(Label, Style(font_size=21))
                 .styled(Button, Style(fill="#123456", padding=47,
                                       shadow="#112233", shadow_blur=4)))
        for kind in (CheckBox, RadioButton, Toggle, Hyperlink, CustomLink):
            with self.subTest(control=kind.__name__):
                owner = self.control(kind, theme_override=theme)
                style = resolve_style(owner)
                self.assertNotIn("Button", _style_kinds(kind))
                self.assertEqual(style.font_size, 21)
                self.assertNotEqual(style.fill, "#123456")
                self.assertNotEqual(style.padding, 47)
                self.assertIsNone(style.shadow)
                self.assertEqual(_effect_parts(theme, kind), ())

    def test_exclusions_are_inherited_and_can_be_replaced_by_a_subclass(self):
        class PlainAction(ComposedAction):
            style_excludes = ("Button",)

        class PlainChild(PlainAction):
            pass

        class RestoredAction(PlainAction):
            style_excludes = ()

        theme = Theme().styled(Button, Style(fill="#123456"))
        for kind in (PlainAction, PlainChild):
            owner = self.control(kind, theme_override=theme)
            self.assertNotIn("Button", _style_kinds(kind))
            self.assertEqual(resolve_style(owner).fill, theme.tokens.surface)
        restored = self.control(RestoredAction, theme_override=theme)
        self.assertIn("Button", _style_kinds(RestoredAction))
        self.assertEqual(resolve_style(restored).fill, "#123456")

    def test_fallback_effect_parts_use_existing_declared_part_filter_and_cache(self):
        class ComposedParts(Control):
            style_fallbacks = ("SharedField",)
            style_parts = ("body", "indicator")

        theme = Theme(rules=(
            Rule("SharedField", "indicator", style=Style(shadow="#123456", shadow_blur=3)),
            Rule("SharedField", "unknown", style=Style(shadow="#654321", shadow_blur=3)),
        ))
        parts = _effect_parts(theme, ComposedParts)
        self.assertEqual(parts, ("indicator",))
        self.assertIs(_effect_parts(theme, ComposedParts), parts)

    def test_invalid_selector_metadata_is_rejected_at_class_definition(self):
        for name in ("style_fallbacks", "style_excludes"):
            for invalid in (None, "Button", ["Button"], ("",), ("bad name",),
                            ("bad-name",), (1,), ("Button", None)):
                with self.subTest(metadata=name, value=invalid):
                    with self.assertRaisesRegex(SchemaError, name):
                        type("InvalidStyle", (Control,), {name: invalid})
