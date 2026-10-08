"""Typed immutable tokens and control/part states, with safe JSON persistence."""

import json
import re
from dataclasses import asdict, dataclass, fields, replace
from functools import cached_property
from math import isfinite
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol

StyleState = Literal["normal", "selected", "checked", "hover", "pressed", "disabled"]

# Largest corner radius of a CheckBox box; a RadioButton check stays a circle.
CHECK_BOX_RADIUS = 5


class StyleTarget(Protocol):
    _style_kind: ClassVar[str]
    style_parts: ClassVar[tuple[str, ...]]


def _color(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value
    ):
        raise ValueError(f"Expected #RRGGBB or #RRGGBBAA color, got {value!r}")


def _luminance(value):
    channels = [int(value[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [
        c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
        for c in channels
    ]
    return sum(c * w for c, w in zip(linear, (0.2126, 0.7152, 0.0722)))


def _tint(first, second, amount):
    return "#" + "".join(
        f"{round(int(first[i:i + 2], 16) * (1 - amount) + int(second[i:i + 2], 16) * amount):02x}"
        for i in (1, 3, 5)
    )


def _readable_ink(preferred, background, fallback):
    light, dark = sorted((_luminance(preferred), _luminance(background)), reverse=True)
    return preferred if (light + .05) / (dark + .05) >= 4.5 else fallback


def _hover_fill(color, foreground):
    """Tint an action face away from its text, preserving the original alpha."""

    # A small tint makes a solid accent button respond without reducing text
    # contrast. It is resolved once with the immutable theme, never each frame.
    target = 0 if _luminance(color) < _luminance(foreground) else 255
    return (
        "#"
        + "".join(
            f"{round(int(color[i : i + 2], 16) * 0.88 + target * 0.12):02x}"
            for i in (1, 3, 5)
        )
        + color[7:]
    )


@dataclass(frozen=True)
class Tokens:
    background: str = "#11151f"
    surface: str = "#1b2230"
    raised: str = "#252e40"
    foreground: str = "#ecf0f8"
    muted: str = "#9aa8be"
    accent: str = "#5c6bd4"
    accent_text: str = "#ffffff"
    border: str = "#344057"
    selection: str = "#344c83"
    danger: str = "#ee7383"
    font_family: str = "ui"
    font_size: float = 15
    spacing: float = 12
    radius: float = 10
    control_height: float = 38

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name == "font_family":
                if value not in ("ui", "mono"):
                    raise ValueError("font_family must be ui or mono")
            elif f.type is str:
                _color(value)
            elif (
                type(value) not in (int, float)
                or not isfinite(value)
                or value < 0
                or (f.name in ("font_size", "control_height") and value == 0)
            ):
                raise ValueError(f"{f.name} must be finite and positive")


@dataclass(frozen=True)
class Style:
    fill: str | None = None
    foreground: str | None = None
    border: str | None = None
    radius: float | None = None
    border_width: float | None = None
    font_family: str | None = None
    font_size: float | None = None
    padding: float | None = None
    # Surface effects are data, rendered by the shared Painter on every host.
    fill_end: str | None = None
    border_end: str | None = None
    gradient_axis: Literal["vertical", "horizontal"] | None = None
    bevel: Literal["none", "raised", "sunken"] | None = None
    bevel_width: float | None = None
    bevel_light: str | None = None
    bevel_dark: str | None = None
    highlight: str | None = None
    inner_border: str | None = None
    glow: str | None = None
    glow_width: float | None = None
    pattern: Literal["none", "scanlines", "grid"] | None = None
    pattern_color: str | None = None
    pattern_spacing: float | None = None
    shadow: str | None = None
    shadow_blur: float | None = None
    shadow_x: float | None = None
    shadow_y: float | None = None

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            if value is None:
                continue
            if f.name == "font_family":
                if value not in ("ui", "mono"):
                    raise ValueError("font_family must be ui or mono")
            elif f.name in ("gradient_axis", "bevel", "pattern"):
                choices = {
                    "gradient_axis": ("vertical", "horizontal"),
                    "bevel": ("none", "raised", "sunken"),
                    "pattern": ("none", "scanlines", "grid"),
                }[f.name]
                if value not in choices:
                    raise ValueError(f"{f.name} must be one of {choices}")
            elif f.name in (
                "fill",
                "foreground",
                "border",
                "fill_end",
                "border_end",
                "bevel_light",
                "bevel_dark",
                "highlight",
                "inner_border",
                "glow",
                "pattern_color",
                "shadow",
            ):
                _color(value)
            elif (
                type(value) not in (int, float)
                or not isfinite(value)
                or (value < 0 and f.name not in ("shadow_x", "shadow_y"))
                or (f.name == "font_size" and value == 0)
            ):
                raise ValueError(f"Invalid style value for {f.name}")
            if f.name in ("bevel_width", "glow_width") and float(value) > 16:
                raise ValueError(f"{f.name} must be at most 16 logical pixels")
            if f.name == "pattern_spacing" and float(value) < 2:
                raise ValueError("pattern_spacing must be at least 2 logical pixels")
            if (
                f.name in ("shadow_blur", "shadow_x", "shadow_y")
                and abs(float(value)) > 24
            ):
                raise ValueError(f"{f.name} must be within 24 logical pixels")


@dataclass(frozen=True)
class Rule:
    control: str
    part: str = "body"
    state: StyleState = "normal"
    style: Style = Style()

    def __post_init__(self):
        if self.state not in (
            "normal",
            "selected",
            "checked",
            "hover",
            "pressed",
            "disabled",
        ):
            raise ValueError(f"Unknown style state {self.state!r}")
        if (
            not isinstance(self.style, Style)
            or not isinstance(self.control, str)
            or not self.control.isidentifier()
            or not isinstance(self.part, str)
            or not self.part.isidentifier()
        ):
            raise ValueError("A rule needs control, part and typed Style")
        if self.state != "normal" and (
            self.style.font_size is not None
            or self.style.padding is not None
            or self.style.font_family is not None
        ):
            raise ValueError(
                "Put font_family, font_size and padding in the normal style; interaction states must not change layout"
            )


@dataclass(frozen=True)
class Theme:
    tokens: Tokens = Tokens()
    rules: tuple[Rule, ...] = ()

    def __post_init__(self):
        if (
            not isinstance(self.tokens, Tokens)
            or not isinstance(self.rules, tuple)
            or any(not isinstance(r, Rule) for r in self.rules)
        ):
            raise TypeError("Theme requires Tokens and a tuple of Rule objects")
        keys = [(r.control, r.part, r.state) for r in self.rules]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate theme rule")

    def derive(self, **tokens):
        return replace(self, tokens=replace(self.tokens, **tokens))

    def styled(
        self,
        control: type[StyleTarget],
        style: Style,
        *,
        part: str = "body",
        state: StyleState = "normal",
    ) -> "Theme":
        if part not in control.style_parts:
            raise ValueError(
                f"{control.__name__} has parts {control.style_parts}, not {part!r}"
            )
        rule = Rule(control._style_kind, part, state, style)
        return replace(
            self,
            rules=tuple(
                r
                for r in self.rules
                if (r.control, r.part, r.state) != (rule.control, part, state)
            )
            + (rule,),
        )

    @cached_property
    def _rule_index(self):
        # Compile only the immutable data lookup, never drawing code. This avoids
        # scanning and converting the entire catalog on every control part.
        return {
            (r.control, r.part, r.state): {
                k: v for k, v in asdict(r.style).items() if v is not None
            }
            for r in self.rules
        }

    @cached_property
    def _resolved_styles(self):
        # Per immutable theme, rather than an lru_cache keyed by a deep hash of
        # every rule. Derived/loaded themes get their own bounded lookup.
        return {}

    @cached_property
    def _has_shadows(self):
        return any(
            r.style.shadow
            and not (len(r.style.shadow) == 9 and r.style.shadow.endswith("00"))
            for r in self.rules
        )

    @cached_property
    def _has_glows(self):
        return any(
            r.style.glow
            and not (len(r.style.glow) == 9 and r.style.glow.endswith("00"))
            for r in self.rules
        )

    def resolve(
        self, control, part="body", state="normal", checked=False, selected=False
    ):
        kinds = (control,) if isinstance(control, str) else tuple(control)
        key = (kinds, part, state, checked, selected)
        cache = self._resolved_styles
        if key in cache:
            return cache[key]
        style = self._resolve(kinds, part, state, checked, selected)
        if len(cache) >= 1024:
            cache.pop(next(iter(cache)))
        cache[key] = style
        return style

    def _resolve(self, kinds, part, state, checked, selected):
        t = self.tokens
        primary = "Button" in kinds and "CheckBox" not in kinds
        result: dict[str, Any] = {
            "fill": t.accent if primary else t.surface,
            "foreground": t.accent_text if primary else t.foreground,
            "border": t.border,
            "radius": t.radius,
            "border_width": 1,
            "font_family": t.font_family,
            "font_size": t.font_size,
            "padding": 8 if "TextBox" in kinds else 10,
        }
        if "Container" in kinds and not any(
            k in kinds for k in ("SubWindow", "Popup", "TabControl")
        ):
            result.update(radius=0, border_width=0)
        if part in ("row", "cell", "item", "tab", "header", "titlebar"):
            result.update(radius=0, border_width=0)
        if part in ("track", "fill", "thumb"):
            result.update(border_width=0)
        if "Slider" in kinds:
            result["radius"] = 9 if part == "thumb" else 3
        if "Toggle" in kinds:
            result["radius"] = 9 if part == "thumb" else 12
        if "RadioButton" in kinds and part == "check":
            result["radius"] = 10
        if part == "track":
            result["fill"] = t.border
        if part in ("fill", "thumb"):
            result["fill"] = t.accent
        if "ProgressBar" in kinds and part == "fill":
            result["foreground"] = t.accent_text
        if "Toggle" in kinds and part == "thumb":
            result["fill"] = t.foreground
        if part == "check":
            result.update(
                fill=t.accent if checked else t.surface, foreground=t.accent_text
            )
            if "RadioButton" not in kinds:
                # A 20 px box with the token radius (10 in the default themes)
                # becomes a circle: unchecked, it is then indistinguishable
                # from a radio button. Boxes stay rounded squares.
                result["radius"] = min(result["radius"], CHECK_BOX_RADIUS)
        if part == "titlebar":
            result["fill"] = t.raised
        if part == "indicator":
            result.update(fill=t.accent, border_width=3, radius=0)
        # Explicit, finite precedence; theme data cannot execute code.
        if selected or (checked and part not in ("check", "thumb")):
            result["fill"] = t.selection
        if state == "hover":
            # Value-bearing parts keep their colors: hovering a slider must not
            # turn both its filled and empty track into the same surface color.
            if part in ("thumb", "check"):
                result.update(border=t.accent, border_width=2)
                if part == "check" and not checked:
                    result["fill"] = t.raised
            elif part not in (
                "track",
                "fill",
                "row",
                "cell",
                "item",
                "tab",
                "header",
            ) and not (checked or selected):
                result["fill"] = (
                    _hover_fill(t.accent, t.accent_text) if primary else t.raised
                )
                if primary:
                    result["border"] = t.accent
        if state == "pressed":
            if part in ("thumb", "check"):
                result.update(border=t.accent, border_width=2)
            elif part not in ("track", "fill", "row", "cell", "item", "tab", "header"):
                result.update(fill=t.selection, foreground=t.foreground)
        if part == "body" and state in ("hover", "pressed") and any(
            kind in kinds for kind in ("TextBox", "NumericInput", "ColorPicker")
        ):
            result.update(fill=t.surface, foreground=t.foreground, border=t.accent)
        if state == "disabled":
            result.update(fill=t.surface, foreground=t.muted)
            if part in ("fill", "thumb") and any(
                k in kinds for k in ("Slider", "Toggle")
            ):
                result["fill"] = t.muted
        if "Hyperlink" in kinds and part == "body":
            result.update(
                fill="#00000000", border_width=0,
                foreground=t.muted if state == "disabled" else t.accent,
            )
            if state in ("hover", "pressed"):
                result.update(border=t.accent, border_width=1)
        if any(kind in kinds for kind in ("SegmentedControl", "Breadcrumb", "Pagination")):
            # These compose actions, but their full body is not a primary button.
            result.update(fill="#00000000", foreground=t.foreground, border_width=0)
            if "SegmentedControl" in kinds and part == "body":
                result.update(fill=t.raised, border=t.border, border_width=1)
            if part in ("segment", "page", "arrow", "item"):
                if selected:
                    result.update(fill=t.accent, foreground=t.accent_text)
                elif state in ("hover", "pressed"):
                    result.update(fill=t.selection)
                if "Breadcrumb" in kinds:
                    result.update(fill="#00000000",
                                  foreground=t.foreground if selected else t.accent)
            if part in ("separator", "ellipsis") or state == "disabled":
                result.update(foreground=t.muted)
                if state == "disabled":
                    result.update(fill=t.surface if selected else "#00000000")
        if "ChoiceCard" in kinds:
            active = checked or selected
            result.update(fill=t.selection if active else t.surface,
                          foreground=t.foreground, border=t.accent if active else t.border,
                          border_width=2 if active else 1)
            if part == "description":
                result.update(foreground=t.foreground if active else _readable_ink(
                    t.muted, t.surface, t.foreground))
            if part == "icon":
                icon_fill = _tint(t.surface, t.accent, .22 if active else .10)
                result.update(fill=icon_fill, border_width=0, radius=min(t.radius, 10),
                              foreground=_readable_ink(t.accent, icon_fill, t.foreground))
            if part == "check":
                result.update(fill=t.accent if active else t.surface,
                              foreground=_readable_ink(t.accent_text, t.accent, t.background),
                              radius=CHECK_BOX_RADIUS)
            if state in ("hover", "pressed"):
                result.update(border=t.accent)
            if state == "disabled":
                result.update(fill=t.surface, foreground=t.muted, border=t.border)
        if "Disclosure" in kinds:
            result.update(fill=t.surface, foreground=t.foreground)
            if part == "header":
                result.update(fill=t.selection if selected else t.raised)
            if part == "summary":
                result.update(foreground=_readable_ink(
                    t.muted, t.selection if selected else t.raised, t.foreground))
            if part == "chevron":
                result.update(foreground=t.foreground)
            if state == "disabled":
                result.update(fill=t.surface, foreground=t.muted)
        if any(kind in kinds for kind in ("Badge", "AlertBanner", "Meter")):
            night = _luminance(t.background) < .2
            tones = {
                "neutral": t.muted,
                "info": t.accent,
                "success": "#83d6a5" if night else "#196846",
                "warning": "#efc577" if night else "#825500",
                "danger": t.danger,
            }
            if part in tones:
                color = tones[part]
                fill = t.raised if part == "neutral" else _tint(t.surface, color, .12)
                result.update(fill=fill,
                              foreground=_readable_ink(color, fill, t.foreground),
                              border=_tint(t.border, color, .45), border_width=1)
                if "Meter" in kinds:
                    result.update(fill=color, foreground=_readable_ink(
                        t.accent_text, color, t.background), border_width=0)
            if "Badge" in kinds:
                result.update(radius=12, padding=7)
            if part in ("action", "close"):
                result.update(fill=t.selection if selected or state in ("hover", "pressed")
                              else "#00000000", foreground=t.foreground,
                              border=t.accent if selected else t.border,
                              border_width=1 if selected else 0)
            if state == "disabled":
                result.update(fill=t.surface, foreground=t.muted, border=t.border)
        if "SubWindow" in kinds and part == "close":
            title = self.resolve(kinds, "titlebar")
            result.update(fill="#00000000", border_width=0, padding=6,
                          foreground=title.foreground, font_size=title.font_size,
                          font_family=title.font_family)
            if state in ("hover", "pressed"):
                result.update(fill="#a6261b" if state == "pressed" else "#c42b1c",
                              foreground="#ffffff")
            elif state == "disabled":
                result.update(foreground=t.muted)
        selection_states = (["checked"] if checked else []) + (["selected"] if selected else [])
        states = ["normal"] + selection_states + ([state] if state != "normal" else [])
        if "ChoiceCard" in kinds and state in ("hover", "pressed"):
            # The selected card retains its selection material while another
            # card is hovered. Disabled always remains the final override.
            states = ["normal", state] + selection_states
        for selected_state in states:
            for kind in kinds:
                result.update(self._rule_index.get((kind, part, selected_state), {}))
        return Style(**result)

    def to_json(self):
        return json.dumps({"schema": "pysual-theme/1", **asdict(self)}, indent=2)

    @classmethod
    def from_json(cls, text):
        doc = json.loads(text)
        if (
            not isinstance(doc, dict)
            or set(doc) != {"schema", "tokens", "rules"}
            or doc["schema"] != "pysual-theme/1"
        ):
            raise ValueError("Unsupported theme document")
        if (
            not isinstance(doc["tokens"], dict)
            or set(doc["tokens"]) != {f.name for f in fields(Tokens)}
            or not isinstance(doc["rules"], list)
        ):
            raise ValueError("Theme requires all declared tokens and a rules list")
        if any(
            not isinstance(r, dict) or set(r) != {"control", "part", "state", "style"}
            for r in doc["rules"]
        ):
            raise ValueError("Theme rules require control, part, state, style")
        return cls(
            Tokens(**doc["tokens"]),
            tuple(
                Rule(r["control"], r["part"], r["state"], Style(**r["style"]))
                for r in doc["rules"]
            ),
        )


def _polished_theme(tokens, *, night):
    """Neutral elevation stays coherent when callers derive their own palette."""
    rules = []
    for kind in ("Button", "Dropdown"):
        rules.extend(
            (
                Rule(
                    kind,
                    style=Style(
                        shadow="#00000050" if night else "#24365a24",
                        shadow_blur=4,
                        shadow_y=2,
                        highlight="#ffffff38" if night else "#ffffff70",
                        inner_border="#ffffff10",
                    ),
                ),
                Rule(
                    kind,
                    state="hover",
                    style=Style(
                        shadow="#00000070" if night else "#24365a38",
                        shadow_blur=8,
                        shadow_y=3,
                        highlight="#ffffff68" if night else "#ffffffa0",
                    ),
                ),
                Rule(
                    kind,
                    state="pressed",
                    style=Style(
                        shadow="#00000000",
                        highlight="#00000000",
                        inner_border="#00000018",
                    ),
                ),
                Rule(
                    kind,
                    state="disabled",
                    style=Style(
                        shadow="#00000000",
                        highlight="#00000000",
                        inner_border="#00000000",
                    ),
                ),
            )
        )
    for kind in ("Popup", "SubWindow"):
        rules.append(
            Rule(
                kind,
                style=Style(
                    shadow="#00000080" if night else "#24365a30",
                    shadow_blur=18,
                    shadow_y=6,
                    border_width=1,
                    highlight="#ffffff18" if night else "#ffffffc0",
                ),
            )
        )
    # Navigation has Button fallbacks for custom themes, but its group/backdrop
    # must not inherit button elevation or an accent face in a catalog theme.
    for kind in ("SegmentedControl", "Pagination"):
        for state in ("normal", "hover", "pressed", "disabled"):
            rules.append(Rule(kind, state=state, style=Style(
                shadow="#00000000", highlight="#00000000",
                inner_border="#00000000",
            )))
    # The neutral themes have a deliberate elevation hierarchy too: quiet
    # cards, inset fields, and floating menus. Colors remain token-driven.
    panel = Style(radius=12, border_width=1,
                  highlight="#ffffff0b" if night else "#ffffffa0")
    field = Style(radius=7, border_width=1,
                  highlight="#00000020" if night else "#00000008")
    for kind in ("Container", "TabControl"):
        rules.append(Rule(kind, style=panel))
    for kind in ("TextBox", "NumericInput", "ColorPicker", "ListView", "TreeView", "DataGrid"):
        rules.append(Rule(kind, style=field))
    for kind, part in (("ListView", "row"), ("TreeView", "row"),
                       ("DataGrid", "cell"), ("MenuBar", "item"), ("TabControl", "tab")):
        rules.append(Rule(kind, part, style=Style(radius=5)))
    rules.extend(_content_rules(tokens, panel, field))
    rules.extend(_scrollbar_rules(tokens, field))
    return Theme(tokens, tuple(rules))


def dark():
    theme = _polished_theme(Tokens(), night=True)
    return replace(theme, rules=theme.rules + (
        Rule("Hyperlink", style=Style(foreground="#8b9cff")),
        Rule("Hyperlink", state="disabled", style=Style(foreground=theme.tokens.muted)),
        Rule("Breadcrumb", "item", style=Style(foreground="#8b9cff")),
        Rule("Breadcrumb", "item", "selected", Style(foreground=theme.tokens.foreground)),
        Rule("Breadcrumb", "item", "disabled", Style(foreground=theme.tokens.muted)),
    ))


def light():
    return _polished_theme(
        Tokens(
            background="#f1f4fa",
            surface="#ffffff",
            raised="#e7ecf6",
            foreground="#1f293b",
            muted="#63718a",
            accent="#465ed5",
            border="#cbd3e3",
            selection="#dce4ff",
            danger="#b5384b",
        ),
        night=False,
    )


def load_theme(path):
    return Theme.from_json(Path(path).read_text(encoding="utf-8"))


def save_theme(theme, path):
    Path(path).write_text(theme.to_json() + "\n", encoding="utf-8")


# Immutable catalog values use the same checked theme data as user themes.
# No theme object owns drawing code or imports control classes.
def theme_names() -> tuple[str, ...]:
    """Nine visual families, each followed by its dark companion.

    Unsuffixed identifiers select the light appearance.
    """
    return (
        "modern",
        "modern_dark",
        "windows",
        "windows_dark",
        "macos",
        "macos_dark",
        "terminal",
        "terminal_dark",
        "win31",
        "win31_dark",
        "winxp",
        "winxp_dark",
        "macintosh",
        "macintosh_dark",
        "analog_arcade",
        "analog_arcade_dark",
        "neon",
        "neon_dark",
    )


def get_theme(name: str) -> Theme:
    """Return a curated immutable theme; unknown names fail explicitly."""
    name = name.casefold()
    if name == "modern_dark":
        return dark()
    if name == "modern":
        return light()
    if name in (
        "terminal", "terminal_dark", "win31", "win31_dark",
        "winxp", "winxp_dark", "macintosh", "macintosh_dark",
        "analog_arcade", "analog_arcade_dark", "neon", "neon_dark",
    ):
        return _reference_theme(name)
    if name in ("windows", "windows_dark", "macos", "macos_dark"):
        return _platform_theme(name)
    raise ValueError(f"Unknown theme {name!r}; choose from {', '.join(theme_names())}")


def _check_box(style):
    """Keep a CheckBox box a rounded square whatever radius its source style has."""
    radius = style.radius if style.radius is not None else CHECK_BOX_RADIUS
    return replace(style, radius=min(radius, CHECK_BOX_RADIUS))


def _surface_rules(tokens, face, field, panel, accent, selection, titlebar, thumb=None,
                   secondary=None):
    """Apply a visual language through ordinary control/part/state declarations.

    ``thumb`` styles slider and toggle thumbs; it defaults to the button face.
    """
    rules = []
    thumb = face if thumb is None else thumb
    secondary = secondary or replace(
        panel, fill=tokens.raised, fill_end=tokens.raised,
        foreground=tokens.foreground, radius=field.radius,
        glow_width=min(2, panel.glow_width or 0), shadow="#00000000",
    )

    def rule(
        kinds: str, style: Style, part: str = "body", state: StyleState = "normal"
    ):
        rules.extend(Rule(kind, part, state, style) for kind in kinds.split())

    rule("Button", face)
    rule("Dropdown", secondary)
    rule("TextBox NumericInput ColorPicker ListView TreeView DataGrid", field)
    rule("Container MenuBar TabControl", panel)
    rule("Popup SubWindow", replace(
        panel, shadow=panel.shadow or ("#00000000" if panel.bevel else "#00000050"),
        shadow_blur=panel.shadow_blur if panel.shadow else 16,
        shadow_y=panel.shadow_y if panel.shadow else 5,
    ))
    rule("CheckBox", Style(foreground=tokens.foreground))
    rule("CheckBox", _check_box(field), "check")
    rule("CheckBox", _check_box(accent), "check", "checked")
    rule("RadioButton", Style(radius=10), "check")
    rule("RadioButton", Style(radius=10), "check", "checked")
    rule("Toggle Slider ProgressBar", field, "track")
    rule("Toggle", accent, "track", "checked")
    rule("Slider ProgressBar", accent, "fill")
    rule("Slider Toggle", thumb, "thumb")
    rule("SubWindow", titlebar, "titlebar")
    rule("DataGrid", titlebar, "header")
    for kind, part in (
        ("ListView", "row"),
        ("TreeView", "row"),
        ("DataGrid", "cell"),
        ("MenuBar", "item"),
        ("TabControl", "tab"),
    ):
        rule(kind, Style(fill=tokens.surface, radius=0, border_width=0), part)
        rule(kind, selection, part, "selected")
    for kind, normal in (("Button", face), ("Dropdown", secondary)):
        rule(
            kind,
            Style(
                fill=_hover_fill(normal.fill or tokens.surface,
                                 normal.foreground or tokens.foreground),
                fill_end=_hover_fill(normal.fill_end or normal.fill or tokens.surface,
                                     normal.foreground or tokens.foreground),
                border=tokens.accent,
                glow_width=min(16, normal.glow_width + 2) if normal.glow_width else None,
                shadow=normal.shadow,
                shadow_blur=min(24, (normal.shadow_blur or 0) + 2) if normal.shadow else None,
                shadow_y=normal.shadow_y,
            ),
            state="hover",
        )
    rule(
        "TextBox NumericInput ColorPicker",
        Style(border=tokens.accent),
        state="hover",
    )
    for state in ("hover", "pressed"):
        rule(
            "CheckBox",
            Style(border=tokens.accent, border_width=2),
            "check",
            state,
        )
        rule(
            "Slider Toggle",
            Style(border=tokens.accent, border_width=2),
            "thumb",
            state,
        )
    rule(
        "Button Dropdown",
        Style(
            fill=tokens.selection,
            fill_end=tokens.selection,
            foreground=tokens.foreground,
            bevel="sunken" if face.bevel == "raised" else "none",
            glow_width=0,
            shadow="#00000000",
            highlight="#00000000",
        ),
        state="pressed",
    )
    disabled = Style(
        fill=tokens.surface,
        fill_end=tokens.surface,
        foreground=tokens.muted,
        border=tokens.border,
        border_end=tokens.border,
        glow_width=0,
        highlight="#00000000",
        inner_border="#00000000",
        pattern="none",
        shadow="#00000000",
    )
    rule("Button Dropdown TextBox NumericInput ColorPicker", disabled, state="disabled")
    rule("CheckBox", Style(foreground=tokens.muted), state="disabled")
    rule("CheckBox", disabled, "check", "disabled")
    rule("Slider Toggle", disabled, "track", "disabled")
    for part in ("fill", "thumb"):
        rule(
            "Slider" if part == "fill" else "Slider Toggle",
            replace(disabled, fill=tokens.muted, fill_end=tokens.muted),
            part,
            "disabled",
        )
    rules.extend(_navigation_rules(tokens, face, field, accent))
    rules.extend(_content_rules(tokens, panel, field))
    rules.extend(_scrollbar_rules(tokens, field))
    return tuple(rules)


def _scrollbar_rules(t, field):
    """Scroll affordances are neutral grips, leaving accent for actual values."""
    rules = []
    for kind in ("ScrollArea", "ListView", "TreeView", "DataGrid", "TextBox"):
        channel = field.fill or t.surface
        rules.append(Rule(kind, "track", style=Style(
            fill=channel, radius=0, border_width=0,
        )))
        rules.append(Rule(kind, "thumb", style=Style(
            fill=t.muted, border=channel, border_width=3,
            radius=0 if t.radius == 0 else 6,
            shadow="#00000000", glow_width=0,
        )))
        for state in ("hover", "pressed"):
            rules.append(Rule(kind, "thumb", state, Style(fill=t.foreground)))
        rules.append(Rule(kind, "thumb", "disabled", Style(fill=t.border)))
    return rules


def _content_rules(t, panel, field):
    """Cards and feedback reuse each material while keeping their semantic roles."""
    rules = []

    def rule(kind, part, style, state="normal"):
        rules.append(Rule(kind, part, state, style))

    # Explicitly clear Button fallback effects on every interactive state.
    quiet = Style(shadow="#00000000", glow_width=0, highlight="#00000000",
                  inner_border="#00000000", bevel="none", pattern="none")
    card = replace(
        panel, foreground=t.foreground, border_width=panel.border_width or 1,
        fill_end=panel.fill_end or panel.fill,
        border_end=panel.border_end or panel.border,
        bevel=panel.bevel or "none", pattern=panel.pattern or "none",
        highlight=panel.highlight or "#00000000",
        inner_border=panel.inner_border or "#00000000",
        shadow="#00000000", glow_width=min(2, panel.glow_width or 0),
    )
    rule("ChoiceCard", "body", card)
    for state in ("checked", "selected"):
        rule("ChoiceCard", "body", replace(
            quiet, fill=t.selection, fill_end=t.selection,
            foreground=t.foreground, border=t.accent, border_end=t.accent,
            border_width=2,
        ), state)
    for state in ("hover", "pressed"):
        # Preserve the checked fill: an unselected card under the pointer
        # must not look like a second selected option beside the active one.
        rule("ChoiceCard", "body", replace(quiet, fill=card.fill or t.surface,
                                             fill_end=card.fill_end or card.fill or t.surface,
                                             foreground=t.foreground,
                                             border=t.accent, border_end=t.accent,
                                             border_width=2), state)
    rule("ChoiceCard", "body", replace(quiet, fill=t.surface, fill_end=t.surface,
                                         foreground=t.muted, border=t.border,
                                         border_end=t.border, border_width=1), "disabled")
    rule("ChoiceCard", "icon", quiet)
    rule("ChoiceCard", "description", replace(quiet, foreground=_readable_ink(
        t.muted, card.fill or t.surface, t.foreground)))
    for state in ("selected", "checked"):
        rule("ChoiceCard", "description", Style(foreground=t.foreground), state)
    rule("ChoiceCard", "description", Style(foreground=t.muted), "disabled")
    rule("ChoiceCard", "check", replace(quiet, radius=min(t.radius, CHECK_BOX_RADIUS)))
    rule("Disclosure", "body", card)
    rule("Disclosure", "header", replace(card, fill=t.raised, fill_end=t.raised,
                                             border_width=0))
    rule("Disclosure", "header", Style(fill=t.selection, fill_end=t.selection), "selected")
    for state in ("hover", "pressed"):
        rule("Disclosure", "header", Style(border=t.accent, border_width=1), state)
    rule("Disclosure", "summary", Style(foreground=_readable_ink(
        t.muted, t.raised, t.foreground)))
    rule("Disclosure", "summary", Style(foreground=_readable_ink(
        t.muted, t.selection, t.foreground)), "selected")
    rule("Disclosure", "summary", Style(foreground=t.muted), "disabled")
    for part in ("body", "header"):
        rule("Disclosure", part, replace(quiet, fill=t.surface, fill_end=t.surface,
                                           foreground=t.muted, border=t.border,
                                           border_end=t.border), "disabled")
    # Semantic surfaces keep tone colors resolved from the current tokens;
    # copying the panel gradient here would erase success/warning/danger colors.
    material = replace(quiet, radius=panel.radius, border_width=1,
                       bevel=panel.bevel or "none", bevel_width=panel.bevel_width,
                       bevel_light=panel.bevel_light, bevel_dark=panel.bevel_dark,
                       pattern=panel.pattern or "none", pattern_color=panel.pattern_color,
                       pattern_spacing=panel.pattern_spacing,
                       highlight=panel.highlight or "#00000000")
    for kind in ("Badge", "AlertBanner"):
        for part in ("body", "info", "success", "warning", "danger") + (("neutral",) if kind == "Badge" else ()):
            rule(kind, part, replace(material, radius=0 if t.radius == 0 else
                                     (12 if kind == "Badge" else panel.radius),
                                     # A rounded badge is a signal lamp, not a
                                     # cabinet panel. Insetting a square bevel by
                                     # its pill radius puts the edge through text.
                                     bevel="none" if kind == "Badge" and t.radius else material.bevel))
        for part in ("info", "success", "warning", "danger") + (("neutral",) if kind == "Badge" else ()):
            rule(kind, part, replace(quiet, fill=t.surface, fill_end=t.surface,
                                     foreground=t.muted, border=t.border), "disabled")
    for part in ("action", "close"):
        rule("AlertBanner", part, replace(quiet, radius=min(t.radius, 6)))
    for part in ("warning", "danger"):
        rule("Meter", part, replace(quiet, radius=field.radius, border_width=0,
                                     pattern=panel.pattern or "none",
                                     pattern_color=panel.pattern_color,
                                     pattern_spacing=panel.pattern_spacing))
    return rules


def _navigation_rules(t, face, field, accent):
    """Navigation shares the theme's surfaces without inheriting button bodies."""
    rules = []

    def rule(kind, part, style, state="normal"):
        rules.append(Rule(kind, part, state, style))

    empty = Style(fill="#00000000", fill_end="#00000000", border_width=0,
                  foreground=t.foreground, bevel="none", pattern="none",
                  shadow="#00000000", glow_width=0, highlight="#00000000",
                  inner_border="#00000000")
    well = replace(field, radius=face.radius,
                   fill_end=field.fill_end or field.fill,
                   shadow="#00000000", glow_width=0,
                   highlight=field.highlight or "#00000000",
                   inner_border=field.inner_border or "#00000000",
                   bevel=field.bevel or "none", pattern=field.pattern or "none")
    rule("SegmentedControl", "body", well)
    for state in ("hover", "pressed"):
        rule("SegmentedControl", "body", well, state)
    rule("SegmentedControl", "body", replace(well, fill=t.surface,
                                               fill_end=t.surface, foreground=t.muted), "disabled")
    for kind in ("Pagination", "Breadcrumb"):
        for state in ("normal", "hover", "pressed", "disabled"):
            rule(kind, "body", empty, state)
    for kind, part in (("SegmentedControl", "segment"), ("Pagination", "page"),
                       ("Pagination", "arrow")):
        rule(kind, part, replace(empty, radius=face.radius))
        rule(kind, part, replace(accent, fill_end=accent.fill_end or accent.fill,
                                shadow="#00000000", glow_width=0,
                                bevel="none"), "selected")
        for state in ("hover", "pressed"):
            # An outline keeps selected fills intact under pointer states.
            rule(kind, part, Style(border=t.accent, border_width=1), state)
        rule(kind, part, replace(empty, foreground=t.muted), "disabled")
    for part in ("item", "separator"):
        rule("Breadcrumb", part, replace(empty, foreground=t.accent if part == "item" else t.muted))
    rule("Breadcrumb", "item", replace(empty, foreground=t.foreground), "selected")
    rule("Breadcrumb", "item", Style(foreground=t.muted), "disabled")
    rule("SegmentedControl", "separator", replace(empty, foreground=t.border))
    rule("Pagination", "ellipsis", replace(empty, foreground=t.muted))
    return rules


def _reference_theme(name):
    """Era-specific materials, shared by every control and every renderer.

    Light and dark are designed companions, not inverted screenshots. Geometry,
    edge treatment, typography and feedback preserve each family's character.
    All decoration remains serializable Style data rather than drawing hooks.
    """
    night = name.endswith("_dark")
    family = name.removesuffix("_dark")
    if family == "terminal":
        t = Tokens(
            background="#00004e" if night else "#e0e4dc",
            surface="#000080" if night else "#f3f3e7",
            raised="#171794" if night else "#e0e5df",
            foreground="#ffffff" if night else "#142451",
            muted="#b2b2d7" if night else "#58617a",
            accent="#00e5e5" if night else "#174f79",
            accent_text="#001343" if night else "#ffffff",
            border="#3ee5e5" if night else "#536784",
            selection="#17389a" if night else "#cedae2",
            danger="#ffff55" if night else "#973723",
            font_family="mono", font_size=14, spacing=8,
            radius=0, control_height=34,
        )
        panel = Style(fill=t.surface, foreground=t.foreground,
                      border=t.border, border_width=1, radius=0,
                      inner_border="#008eaf" if night else "#b6c0c5")
        field = replace(panel, fill="#00005c" if night else "#ffffff",
                        inner_border="#00005c" if night else "#ffffff")
        face = replace(panel, fill=t.accent, foreground=t.accent_text,
                       border=t.accent, inner_border=t.accent_text,
                       shadow="#000027" if night else "#8c959d",
                       shadow_blur=0, shadow_x=3, shadow_y=3)
        accent = replace(face, shadow="#00000000", inner_border="#00000000")
        selection = replace(accent, border_width=0)
        titlebar = replace(accent, border_width=1, inner_border=t.surface)
        secondary = replace(panel, foreground=t.foreground)
        thumb = replace(accent, border=t.foreground, border_width=1)
    elif family == "win31":
        t = Tokens(
            background="#20262b" if night else "#b7c3c3",
            surface="#34383e" if night else "#c0c0c0",
            raised="#484e56" if night else "#dedede",
            foreground="#f4f4f4" if night else "#111111",
            muted="#b3b7bf" if night else "#545454",
            accent="#a6beff" if night else "#000080",
            accent_text="#111b36" if night else "#ffffff",
            border="#727c88" if night else "#555555",
            selection="#364b76" if night else "#a3b3d1",
            danger="#ffb59c" if night else "#8b1818",
            radius=0, spacing=8, font_size=14, control_height=32,
        )
        panel = Style(fill=t.surface, foreground=t.foreground, radius=0,
                      border=t.border, border_width=0, bevel="raised", bevel_width=2,
                      bevel_light="#87909c" if night else "#ffffff",
                      bevel_dark="#101317" if night else "#484848")
        face = replace(panel, shadow="#00000000")
        field = replace(panel, fill="#191d24" if night else "#ffffff", bevel="sunken")
        accent = Style(fill=t.accent, foreground=t.accent_text,
                       border_width=0, radius=0)
        selection = Style(fill="#000080", foreground="#ffffff", border_width=0, radius=0)
        titlebar = replace(selection, border="#9ba9de" if night else "#ffffff",
                           border_width=1)
        secondary = face
        thumb = replace(face, fill=t.raised, border=t.border, border_width=1)
    elif family == "winxp":
        t = Tokens(
            background="#172940" if night else "#dbe9f7",
            surface="#283748" if night else "#ece9d8",
            raised="#3b4e63" if night else "#fbfaf4",
            foreground="#f0f5ff" if night else "#172640",
            muted="#b0c1d5" if night else "#5c687b",
            accent="#9bc8ff" if night else "#0052b5",
            accent_text="#13233b" if night else "#ffffff",
            border="#697e99" if night else "#8fa4bc",
            selection="#254b75" if night else "#c6dcf5",
            danger="#ffafa2" if night else "#a02d21",
            radius=4, spacing=10, font_size=14, control_height=34,
        )
        panel = Style(fill=t.surface, foreground=t.foreground, border=t.border,
                      radius=6, border_width=1,
                      inner_border="#a6c2e52b" if night else "#ffffff",
                      highlight="#a3bee244" if night else "#ffffff")
        secondary = Style(fill="#43566e" if night else "#ffffff",
                          fill_end="#2c3c50" if night else "#e1dfd1",
                          foreground=t.foreground, border=t.border,
                          radius=3, border_width=1, highlight="#ffffff70",
                          inner_border="#b2c9ed45" if night else "#ffffff")
        face = Style(fill="#2463b7" if night else "#1262ca",
                     fill_end="#104080" if night else "#00429c",
                     foreground="#ffffff", border="#82b9ff",
                     border_width=1, radius=4, highlight="#9ce0ff",
                     inner_border="#ffffff42", shadow="#002a7048",
                     shadow_blur=2, shadow_y=1)
        field = Style(fill="#182537" if night else "#ffffff",
                      foreground=t.foreground, border=t.border, border_width=1,
                      radius=2, highlight="#00000020")
        accent = Style(fill=t.accent, foreground=t.accent_text,
                       border=t.border, border_width=1, radius=3)
        selection = replace(face, radius=0, shadow="#00000000",
                            highlight="#00000000", inner_border="#00000000")
        titlebar = Style(fill="#1e66d0", fill_end="#003c9e",
                         foreground="#ffffff", border="#a3cfff",
                         border_width=1, radius=5, highlight="#a9dcff",
                         inner_border="#438eff")
        thumb = replace(secondary, fill="#d7e7d4" if night else "#ffffff",
                        fill_end="#a3c59c" if night else "#dfecd6",
                        border="#38783d", radius=3, border_width=1)
    elif family == "macintosh":
        t = Tokens(
            background="#1e1e1e" if night else "#bcbcbc",
            surface="#303030" if night else "#ededed",
            raised="#484848" if night else "#ffffff",
            foreground="#f4f4f4" if night else "#101010",
            muted="#b3b3b3" if night else "#555555",
            accent="#f4f4f4" if night else "#111111",
            accent_text="#111111" if night else "#ffffff",
            border="#cccccc" if night else "#161616",
            selection="#525252" if night else "#c6c6c6",
            danger="#ffffff" if night else "#111111",
            font_family="mono", font_size=14, radius=0,
            spacing=8, control_height=32,
        )
        panel = Style(fill=t.surface, foreground=t.foreground, border=t.border,
                      border_width=1, radius=0,
                      shadow="#000000", shadow_blur=0, shadow_x=2, shadow_y=2)
        face = replace(panel, fill=t.raised, radius=4,
                       inner_border="#747474" if night else "#ffffff")
        field = replace(panel, fill="#181818" if night else "#ffffff",
                        shadow="#00000000", inner_border=t.surface)
        accent = Style(fill=t.accent, foreground=t.accent_text,
                       border=t.border, border_width=1, radius=0)
        selection = replace(accent, border_width=0)
        titlebar = replace(panel, fill=t.raised, shadow="#00000000",
                           pattern="scanlines", pattern_spacing=3,
                           pattern_color="#ffffff28" if night else "#00000024")
        secondary = face
        thumb = replace(face, radius=0, shadow="#00000000")
    elif family == "analog_arcade":
        t = Tokens(
            background="#17110c" if night else "#e6dbc3",
            surface="#251c13" if night else "#f7eed9",
            raised="#443321" if night else "#fff8e6",
            foreground="#fff0c8" if night else "#382715",
            muted="#c8b08c" if night else "#706047",
            accent="#ffbf54" if night else "#844109",
            accent_text="#291805" if night else "#ffffff",
            border="#a98249" if night else "#aa8b51",
            selection="#594021" if night else "#e5cd9f",
            danger="#ffaf86" if night else "#9d301b",
            font_family="mono", font_size=14, radius=4,
            spacing=12, control_height=38,
        )
        panel = Style(fill=t.surface, foreground=t.foreground, border=t.border,
                      border_width=2, radius=6, bevel="sunken", bevel_width=2,
                      bevel_light="#b7a171" if night else "#fff7dc",
                      bevel_dark="#070604" if night else "#aa8a4c",
                      pattern="scanlines", pattern_spacing=4,
                      pattern_color="#fff1c008" if night else "#76502008")
        face = Style(fill="#b74224" if night else "#a63721",
                     fill_end="#7b2415", foreground="#fff5df",
                     border="#e5a354", border_width=1, radius=5,
                     bevel="raised", bevel_width=2,
                     bevel_light="#e9a26c", bevel_dark="#4b1a0c",
                     highlight="#ffd1a640", shadow="#070403b0",
                     shadow_blur=0, shadow_y=4)
        field = replace(panel, fill="#100f08" if night else "#fff9df",
                        border_width=1, radius=3, pattern="none",
                        inner_border="#b2842640")
        accent = Style(fill="#ffd36c" if night else "#e9ba61",
                       fill_end="#edaa37" if night else "#d6a044",
                       foreground="#2f210a", border="#bd842e",
                       border_width=1, radius=2,
                       pattern="scanlines", pattern_color="#43260120", pattern_spacing=3)
        selection = Style(fill=t.selection, foreground=t.foreground,
                          border=t.accent, border_width=1, radius=2)
        titlebar = replace(panel, fill=t.raised, pattern="none",
                           foreground=t.foreground, bevel="raised")
        secondary = replace(face, fill="#57402a" if night else "#f6e6bd",
                            fill_end="#352417" if night else "#d3bb87",
                            foreground=t.foreground, bevel_light="#aa8653" if night else "#fff8dc",
                            bevel_dark="#171008" if night else "#9d804a", shadow_y=2)
        thumb = replace(secondary, fill="#f9d88a", fill_end="#d4a74e",
                        foreground="#352614", border="#382b12", radius=3)
    else:  # neon: luminous glass and electric outlines, without CRT texture.
        t = Tokens(
            background="#080d20" if night else "#eaf1ff",
            surface="#12182e" if night else "#fafbff",
            raised="#252c49" if night else "#ffffff",
            foreground="#edf7ff" if night else "#242347",
            muted="#abb7d4" if night else "#646786",
            accent="#65eaff" if night else "#5c2ca5",
            accent_text="#061c2a" if night else "#ffffff",
            border="#49829e" if night else "#8cadd0",
            selection="#30335b" if night else "#e4dcf8",
            danger="#ff9acb" if night else "#a52e67",
            radius=8, spacing=12, control_height=38,
        )
        panel = Style(fill=t.surface, foreground=t.foreground,
                      border="#55bed6" if night else "#76aecd",
                      border_end="#b57bd1" if night else "#b695d3",
                      gradient_axis="horizontal", border_width=1, radius=10,
                      inner_border="#65ddff15" if night else "#ffffff",
                      glow="#1ebada" if night else "#398fd6", glow_width=3)
        face = Style(fill="#483299" if night else "#5631a3",
                     fill_end="#8c285f" if night else "#84256a",
                     foreground="#ffffff", border="#6cecff" if night else "#47a9cc",
                     border_end="#f099e0" if night else "#c570b9",
                     gradient_axis="horizontal", border_width=1, radius=6,
                     highlight="#ffffff58", glow="#b073ff", glow_width=5,
                     shadow="#a053d750", shadow_blur=8, shadow_y=2)
        field = replace(panel, fill="#0a1125" if night else "#ffffff",
                        radius=5, glow_width=1)
        accent = Style(fill="#5ee5f5" if night else "#5731a7",
                       fill_end="#a5b3fa" if night else "#852d80",
                       foreground=t.accent_text, border=t.accent,
                       border_width=1, radius=4, gradient_axis="horizontal")
        selection = Style(fill=t.selection, foreground=t.foreground,
                          border=t.accent, border_width=1, radius=4)
        titlebar = replace(panel, fill=t.raised, foreground=t.foreground,
                           radius=6, glow_width=0)
        secondary = replace(panel, fill=t.raised, foreground=t.foreground,
                            radius=6, glow_width=1)
        thumb = replace(accent, fill="#e4fdff" if night else "#ffffff",
                        fill_end="#83dcee" if night else "#e1dcff",
                        foreground=t.foreground, radius=3,
                        glow=t.accent, glow_width=3, border=t.accent)

    rules = {
        (r.control, r.part, r.state): r
        for r in _surface_rules(t, face, field, panel, accent, selection, titlebar,
                                thumb=thumb, secondary=secondary)
    }

    def rule(kinds, style, part="body", state="normal"):
        for kind in kinds.split():
            rules[kind, part, state] = Rule(kind, part, state, style)

    # Window titles are distinctive; table headings remain quiet and legible.
    rule("DataGrid", replace(secondary, radius=0, shadow="#00000000",
                              glow_width=0), "header")
    rule("MenuBar", replace(panel, shadow="#00000000", radius=0,
                             glow_width=0, pattern="none"))
    # Typography is part of the illusion even in composed controls. Outer
    # surfaces never give text-only labels a primary-button foreground.
    rule("Label CheckBox RadioButton", Style(foreground=t.foreground))

    if family == "winxp":
        close = Style(fill="#b83b2b", fill_end="#8d201b", foreground="#ffffff",
                      border="#ffd9ba", border_width=1, radius=3, padding=6,
                      highlight="#ffbf92", inner_border="#f2725050")
    elif family == "macintosh":
        close = replace(face, radius=0, padding=9, shadow="#00000000")
    elif family == "terminal":
        close = replace(panel, padding=7, inner_border="#00000000")
    elif family == "neon":
        close = replace(secondary, foreground=t.danger, border=t.danger,
                        border_end=t.danger, padding=7)
    else:
        close = replace(secondary, padding=6, radius=0 if family == "win31" else 3,
                        shadow="#00000000")
    rule("SubWindow", close, "close")
    for state in ("hover", "pressed"):
        rule("SubWindow", Style(
            fill=_hover_fill(close.fill, close.foreground),
            fill_end=_hover_fill(close.fill_end or close.fill, close.foreground),
            foreground=close.foreground, border=close.border,
            bevel="sunken" if state == "pressed" and close.bevel == "raised" else close.bevel,
            shadow="#00000000", highlight="#00000000" if state == "pressed" else close.highlight,
        ), "close", state)
    rule("SubWindow", Style(fill=t.surface, fill_end=t.surface, foreground=t.muted,
                             border=t.border, border_end=t.border, shadow="#00000000",
                             glow_width=0, bevel="none", highlight="#00000000",
                             inner_border="#00000000"), "close", "disabled")

    if family in ("terminal", "win31", "macintosh", "analog_arcade"):
        # Physical caps depress in place; their hard shadows must stay crisp.
        for kind, normal in (("Button", face), ("Dropdown", secondary)):
            rule(kind, replace(normal, fill=_hover_fill(normal.fill, normal.foreground),
                               fill_end=_hover_fill(normal.fill_end or normal.fill, normal.foreground),
                               shadow_blur=0, border=t.accent), state="hover")
            rule(kind, replace(normal,
                               bevel="sunken" if normal.bevel == "raised" else "none",
                               shadow="#00000000", highlight="#00000000"), state="pressed")
        # Classic flat rails and square thumbs are part of the interaction
        # language, including on the composed RangeSlider and Meter controls.
        rule("Slider ProgressBar", replace(field, radius=0, shadow="#00000000"), "track")
        rule("Toggle", replace(field, radius=0, shadow="#00000000"), "track")
        rule("Toggle", replace(accent, radius=0), "track", "checked")

    if family == "winxp":
        # Luna's green progress channel is deliberately independent of the
        # blue selection accent. Text retains contrast at both gradient ends.
        rule("ProgressBar", Style(fill="#91d355", fill_end="#68b63b",
                                  foreground="#152908", border="#3d822b",
                                  border_width=1, radius=2,
                                  highlight="#d5f7bb"), "fill")
        rule("ProgressBar Slider", replace(field, fill="#172536" if night else "#e1e3df",
                                            fill_end="#273b51" if night else "#f9faf5",
                                            radius=3), "track")
        rule("Toggle", replace(field, radius=3), "track")
        rule("Toggle", replace(accent, radius=3), "track", "checked")
        rule("TabControl", replace(secondary, shadow="#00000000", radius=3), "tab")
        rule("TabControl", replace(panel, border="#dc9a24", border_width=2,
                                    highlight="#ffc75e", radius=3), "tab", "selected")
    elif family == "macintosh":
        rule("TabControl", replace(face, shadow="#00000000", radius=4), "tab")
        rule("TabControl", replace(face, fill=t.surface, radius=4,
                                    shadow="#00000000", border_width=2), "tab", "selected")
    elif family == "analog_arcade":
        # A low phosphor glow belongs to the value, never the entire cabinet.
        rule("ProgressBar", replace(accent, glow="#f4aa39", glow_width=2), "fill")

    if family in ("win31", "macintosh", "winxp", "analog_arcade", "terminal"):
        # Period scrollbars use actual raised grips rather than a modern
        # floating pill, while retaining the controls' generous hit targets.
        for kind in ("ScrollArea", "ListView", "TreeView", "DataGrid", "TextBox"):
            channel = replace(field, radius=0, shadow="#00000000", glow_width=0)
            grip = replace(secondary, radius=0, shadow="#00000000", glow_width=0,
                           border=t.border, border_width=1)
            if family == "macintosh":
                channel = replace(channel, pattern="grid", pattern_spacing=2,
                                  pattern_color="#ffffff28" if night else "#00000024")
            if family == "terminal":
                grip = replace(accent, radius=0, shadow="#00000000")
            rule(kind, channel, "track")
            rule(kind, grip, "thumb")
            for state in ("hover", "pressed"):
                rule(kind, replace(grip, fill=t.raised, fill_end=t.raised,
                                    border=t.accent,
                                    bevel="sunken" if state == "pressed" and grip.bevel else grip.bevel),
                     "thumb", state)
            rule(kind, replace(grip, fill=t.muted, fill_end=t.muted,
                                border=t.border), "thumb", "disabled")
    return Theme(t, tuple(rules.values()))


def _platform_theme(name):
    """Desktop-inspired roles; ordinary data, never a native widget dependency."""
    mac = name.startswith("macos")
    night = name.endswith("_dark")
    if night:
        t = Tokens(
            background="#202020" if not mac else "#242426",
            surface="#2b2b2b" if not mac else "#303033",
            raised="#383838" if not mac else "#414144",
            foreground="#f4f4f4",
            muted="#b5b5ba",
            accent="#60cdff" if not mac else "#409cff",
            accent_text="#002b42" if not mac else "#ffffff",
            border="#484848" if not mac else "#555558",
            selection="#254a5c" if not mac else "#255887",
            danger="#ff6961",
            radius=6 if mac else 4,
            spacing=10,
            control_height=32,
            font_size=14,
        )
        face_fill, input_fill = (
            ("#414144", "#252527") if mac else ("#373737", "#252525")
        )
        top_edge, shadow = "#ffffff18", "#00000060"
    else:
        t = Tokens(
            background="#f3f3f3" if not mac else "#ececef",
            surface="#fafafa" if not mac else "#f7f7f9",
            raised="#ffffff",
            foreground="#1b1b1f",
            muted="#63636b",
            accent="#0067c0" if not mac else "#0064d1",
            accent_text="#ffffff",
            border="#d1d1d1" if not mac else "#c9c9ce",
            selection="#deecf9" if not mac else "#dceaff",
            danger="#c42b1c" if not mac else "#d52e26",
            radius=6 if mac else 4,
            spacing=10,
            control_height=32,
            font_size=14,
        )
        face_fill, input_fill = "#ffffff", "#ffffff"
        top_edge, shadow = "#ffffffd0", "#00000028"
    face = Style(
        fill=face_fill,
        fill_end=("#363639" if night else "#ececf0") if mac else None,
        foreground=t.foreground,
        border=t.border,
        border_width=1,
        radius=6 if mac else 4,
        highlight=top_edge if mac else None,
        shadow=shadow,
        shadow_blur=3 if mac else 1,
        shadow_y=1,
    )
    field = Style(
        fill=input_fill,
        foreground=t.foreground,
        border=t.border,
        border_end=None if mac else ("#98989b" if night else "#85858a"),
        border_width=1,
        radius=5 if mac else 4,
        highlight=("#00000028" if night else "#00000012") if mac else None,
    )
    panel = Style(
        fill=t.surface,
        foreground=t.foreground,
        border=t.border,
        border_width=1,
        radius=12 if mac else 8,
        # Layout containers stay quiet; only windows and popups are elevated.
        shadow="#00000000",
    )
    accent = Style(
        fill=t.accent,
        foreground=t.background if mac and night else t.accent_text,
        border=t.accent,
        border_width=0,
        radius=5 if mac else 3,
    )
    selection = Style(
        fill=t.selection,
        foreground=t.foreground,
        border_width=0,
        radius=6 if mac else 3,
    )
    titlebar = Style(
        fill=t.raised if night else t.background,
        foreground=t.foreground,
        border_width=0,
        radius=0,
    )
    rules = {
        (r.control, r.part, r.state): r
        for r in _surface_rules(t, face, field, panel, accent, selection, titlebar,
                                secondary=face)
    }

    def rule(
        kinds: str, style: Style, part: str = "body", state: StyleState = "normal"
    ):
        for kind in kinds.split():
            rules[kind, part, state] = Rule(kind, part, state, style)

    if mac:
        rule("TabControl", Style(border_width=0), "indicator")
        rule("SubWindow", Style(fill="#ff625a", foreground="#520b08", border="#d94b43",
                                 border_width=1, radius=12, padding=10,
                                 highlight="#ffffff55"), "close")
        for state in ("hover", "pressed"):
            rule("SubWindow", Style(fill="#ee5048" if state == "pressed" else "#ff8078",
                                     foreground="#520b08", border="#d94b43"), "close", state)
        rule("SubWindow", Style(fill=t.raised, foreground=t.muted, border=t.border,
                                 highlight="#00000000"), "close", "disabled")
    else:
        # Fluent inputs carry a stronger lower edge; the accent edge appears
        # on interaction, while macOS keeps a softly inset, even outline.
        for state in ("hover", "pressed"):
            rule("TextBox NumericInput ColorPicker", Style(
                border=t.border, border_end=t.accent,
            ), state=state)

    rule(
        "Button Dropdown",
        Style(
            fill=t.raised,
            fill_end=t.raised,
            border=t.border,
            shadow="#00000070" if night else "#00000030",
            shadow_blur=3 if mac else 1,
            shadow_y=1,
        ),
        state="hover",
    )
    rule(
        "Button Dropdown",
        Style(fill=t.selection, fill_end=t.selection, foreground=t.foreground,
              shadow="#00000000", highlight="#00000000"),
        state="pressed",
    )
    rule(
        "Button Dropdown TextBox NumericInput ColorPicker",
        Style(
            fill=t.surface,
            fill_end=t.surface,
            foreground=t.muted,
            border=t.border,
            shadow="#00000000",
            highlight="#00000000",
        ),
        state="disabled",
    )
    rule(
        "Slider Toggle",
        Style(
            fill="#ffffff",
            border="#00000020",
            border_width=1,
            radius=10,
            shadow=shadow,
            shadow_blur=3,
            shadow_y=1,
        ),
        "thumb",
    )
    rule(
        "Slider Toggle",
        Style(border=t.accent, border_width=2, shadow="#00000000"),
        "thumb",
        "pressed",
    )
    rule("Slider", Style(radius=3), "track")
    rule("Slider ProgressBar", Style(radius=3), "fill")
    if mac and night:
        rule("ProgressBar", Style(foreground=t.background), "fill")
        for kind in ("CheckBox", "RadioButton"):
            rule(kind, replace(rules[kind, "check", "checked"].style,
                               foreground=t.background), "check", "checked")
    rule("Toggle", Style(fill=("#626266" if night else "#d1d1d6") if mac
                         else ("#292929" if night else "#f0f0f0"),
                         border="#929296" if night else "#88888d",
                         border_width=0 if mac else 1, radius=12), "track")
    rule("Toggle", replace(accent, radius=12), "track", "checked")
    rule(
        "Popup SubWindow",
        replace(
            panel,
            fill=t.surface,
            shadow="#00000080" if night else "#00000036",
            shadow_blur=18 if mac else 12,
            shadow_y=6,
        ),
    )
    # Menus inherit Popup elevation; their rows use opaque, readable selection.
    rule(
        "ListView",
        Style(fill=t.surface, foreground=t.foreground, radius=4, border_width=0),
        "row",
    )
    rule(
        "ListView",
        replace(
            selection, fill=t.accent if mac else t.selection,
            foreground=accent.foreground if mac else t.foreground,
            border=t.accent, border_width=0 if mac else 1,
        ),
        "row",
        "selected",
    )
    rule(
        "TabControl",
        Style(fill=t.background, radius=8, border_width=0, shadow="#00000000"),
    )
    rule(
        "TabControl",
        Style(
            fill=t.background,
            foreground=t.muted,
            radius=7 if mac else 4,
            border_width=0,
        ),
        "tab",
    )
    rule(
        "TabControl",
        Style(
            fill=face_fill,
            foreground=t.foreground,
            radius=7 if mac else 4,
            border=t.border,
            border_width=1,
        ),
        "tab",
        "selected",
    )
    rule(
        "MenuBar",
        Style(
            fill=t.background,
            foreground=t.foreground,
            radius=0,
            border_width=0,
            shadow="#00000000",
        ),
    )
    rule(
        "CheckBox",
        Style(fill=t.surface, foreground=t.muted, shadow="#00000000"),
        "check",
        "disabled",
    )
    # Primary actions follow the blue faces in the catalog's platform references;
    # selection controls keep the neutral secondary face defined above.
    primary = replace(accent, radius=6 if mac else 4, border_width=1,
                      border="#1c73d8" if mac else t.accent,
                      fill_end=("#3292f5" if night else "#005bc5") if mac else t.accent,
                      highlight="#ffffff48" if mac else "#ffffff18",
                      shadow=shadow, shadow_blur=2 if mac else 1, shadow_y=1)
    rule("Button", primary)
    rule("Button", Style(fill=_hover_fill(primary.fill, primary.foreground),
                         fill_end=_hover_fill(primary.fill_end, primary.foreground),
                         border=primary.border, shadow=shadow, shadow_blur=2,
                         shadow_y=1), state="hover")
    rule("Button", Style(fill=_hover_fill(primary.fill, primary.foreground),
                         fill_end=_hover_fill(primary.fill, primary.foreground),
                         foreground=primary.foreground, shadow="#00000000",
                         highlight="#00000000"), state="pressed")
    # An empty progress channel must remain visible on pale platform cards.
    rule("Slider ProgressBar", Style(fill="#515154" if night else "#dadade",
                                     foreground=t.foreground, radius=3,
                                     border_width=0, highlight="#00000000"), "track")
    if not mac:
        rule("Slider", Style(fill=t.accent, border="#ffffff" if not night else "#454545",
                             border_width=3, radius=9, shadow=shadow,
                             shadow_blur=2, shadow_y=1), "thumb")
    # The catalog's macOS reference uses a glossy blue selected segment in a
    # recessed well; Windows has a flat face and smaller corner radii.
    well = replace(field, fill=t.background, fill_end=t.background,
                   radius=7 if mac else 4, shadow="#00000000", glow_width=0,
                   highlight="#00000000", inner_border="#00000000", bevel="none")
    for state in ("normal", "hover", "pressed"):
        rule("SegmentedControl", well, state=state)
    if mac:
        rule("SegmentedControl", replace(primary, radius=5, border_width=1,
                                          shadow_blur=2), "segment", "selected")
    else:
        rule("SegmentedControl", replace(accent, radius=3, fill_end=accent.fill), "segment", "selected")
    # Selection uses the same readable accent pair in lists, trees and tables.
    for kind, part in (("TreeView", "row"), ("DataGrid", "cell")):
        rule(kind, replace(selection, fill=t.accent if mac else t.selection,
                           foreground=accent.foreground if mac else t.foreground,
                           border=t.accent, border_width=0 if mac else 1), part, "selected")
    # Keep the generous scrollbar hit target, with a narrow neutral grip inside
    # it. Track-colored borders inset the visible thumb without changing layout.
    for kind in ("ScrollArea", "ListView", "TreeView", "DataGrid", "TextBox"):
        channel = t.surface if kind == "ScrollArea" else input_fill
        rule(kind, Style(fill=channel, radius=0, border_width=0), "track")
        rule(kind, Style(fill="#949499" if night else "#88888d",
                         border=channel, border_width=3, radius=6,
                         shadow="#00000000"), "thumb")
        for state in ("hover", "pressed"):
            rule(kind, Style(fill="#b0b0b5" if night else "#68686d"), "thumb", state)
        rule(kind, Style(fill="#626267" if night else "#bebec3"), "thumb", "disabled")
    return Theme(t, tuple(rules.values()))
