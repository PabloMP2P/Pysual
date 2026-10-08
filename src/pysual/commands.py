"""Immutable menu declarations and a small, explicit keyboard-chord grammar."""

from __future__ import annotations

from dataclasses import dataclass
from .events import UiEvent
from .icons import icon_names

_NAMED_KEYS = {key.casefold(): key for key in (
    "Enter", "Space", "Home", "End", "PageUp", "PageDown",
    "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
)}


def normalize_shortcut(value: str) -> str:
    if not value:
        return ""
    parts = [part.strip() for part in value.split("+")]
    modifiers = [part.title() for part in parts[:-1]]
    key = parts[-1]
    if len(set(modifiers)) != len(modifiers) or set(modifiers) - {"Ctrl", "Shift"}:
        raise ValueError("Shortcuts use Ctrl and optional Shift, or an F1–F12 key")
    key = _NAMED_KEYS.get(key.casefold(), key.upper())
    function = key in {f"F{i}" for i in range(1, 13)}
    named = key.casefold() in _NAMED_KEYS
    if not (
        function
        or (
            "Ctrl" in modifiers
            and (named or (len(key) == 1 and key.isascii() and key.isalnum()))
        )
    ):
        raise ValueError("Use Ctrl+letter/digit/navigation key or F1–F12")
    return "+".join(
        [modifier for modifier in ("Ctrl", "Shift") if modifier in modifiers] + [key]
    )


@dataclass(frozen=True)
class MenuItem:
    key: str
    text: str
    shortcut: str = ""
    enabled: bool = True
    checked: bool = False
    separator: bool = False
    children: tuple[MenuItem, ...] = ()
    icon: str = ""

    def __post_init__(self):
        if (
            not isinstance(self.key, str)
            or not self.key
            or not isinstance(self.text, str)
        ):
            raise ValueError("MenuItem needs a nonempty key and text string")
        if not isinstance(self.shortcut, str) or any(
            type(v) is not bool for v in (self.enabled, self.checked, self.separator)
        ):
            raise TypeError("Menu shortcuts are strings; state flags are booleans")
        normalize_shortcut(self.shortcut)
        if not isinstance(self.icon, str) or (
            self.icon and (self.icon not in icon_names() or self.separator)
        ):
            raise ValueError(
                "MenuItem.icon must be empty or a catalog icon on a command"
            )
        if not isinstance(self.children, tuple) or any(
            not isinstance(item, MenuItem) for item in self.children
        ):
            raise TypeError("Menu children must be a tuple of MenuItem records")
        if self.children and (self.separator or self.shortcut or self.checked):
            raise ValueError(
                "Submenu headings cannot be separators, checked or shortcut commands"
            )


@dataclass(frozen=True)
class MenuGroup:
    text: str
    items: tuple[MenuItem, ...]

    def __post_init__(self):
        if (
            not isinstance(self.text, str)
            or not isinstance(self.items, tuple)
            or any(not isinstance(item, MenuItem) for item in self.items)
        ):
            raise TypeError(
                "MenuGroup needs a text label and tuple of MenuItem records"
            )


@dataclass(frozen=True, kw_only=True)
class CommandEvent(UiEvent):
    key: str


def validate_items(items):
    keys, shortcuts = set(), set()

    def visit(entries, depth=0):
        if depth >= 8:
            raise ValueError("Menus support at most eight nested levels")
        for item in entries:
            if not isinstance(item, MenuItem):
                raise TypeError("Menus contain MenuItem records")
            chord = normalize_shortcut(item.shortcut)
            if item.key in keys:
                raise ValueError(f"Duplicate menu key {item.key!r}")
            if chord and chord in shortcuts:
                raise ValueError(f"Duplicate menu shortcut {chord!r}")
            keys.add(item.key)
            if chord:
                shortcuts.add(chord)
            if item.children:
                visit(item.children, depth + 1)

    visit(items)
