"""Validated startup defaults; external configuration is explicitly opt-in."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import sys
from threading import RLock
from typing import TYPE_CHECKING, cast

from .backends import BACKEND_NAMES, BackendName
from .errors import LifecycleError

if TYPE_CHECKING:
    from .host import Host


@dataclass(frozen=True)
class Settings:
    backend: str | None = None
    terminal_renderer: str | None = None
    theme: str | None = None
    ui_scale: float | None = None


@dataclass(frozen=True)
class Option:
    field: str
    flag: str
    environment: str
    description: str


OPTIONS = (
    Option(
        "backend",
        "--backend",
        "PYSUAL_BACKEND",
        "UI backend: " + ", ".join(BACKEND_NAMES),
    ),
    Option(
        "terminal_renderer",
        "--terminal",
        "PYSUAL_TERMINAL",
        "Terminal renderer: auto, c, or python",
    ),
    Option(
        "theme",
        "--pysual-theme",
        "PYSUAL_THEME",
        "Default window theme (catalog name)",
    ),
    Option(
        "ui_scale",
        "--pysual-scale",
        "PYSUAL_SCALE",
        "Default window scale (finite number, at least 0.25)",
    ),
)
_BY_FLAG = {option.flag: option for option in OPTIONS}
_defaults: dict[str, object] = {}
_environment: dict[str, object] = {}
_command_line: dict[str, object] = {}
_activated = False
_frozen: Settings | None = None
_lock = RLock()


def _validate(name, value):
    if name == "backend":
        if value is not None and (
            not isinstance(value, str) or value not in BACKEND_NAMES
        ):
            raise ValueError("backend must be one of: " + ", ".join(BACKEND_NAMES))
    elif name == "terminal_renderer":
        if value is not None and value not in ("auto", "c", "python"):
            raise ValueError("terminal renderer must be auto, c, or python")
    elif name == "theme":
        from .theme import theme_names

        if value is not None and value not in theme_names():
            raise ValueError("theme must be a built-in theme name")
    elif name == "ui_scale":
        if value is not None and (
            type(value) not in (int, float) or not math.isfinite(value) or value < 0.25
        ):
            raise ValueError("ui_scale must be a finite number of at least 0.25")
    else:
        raise TypeError(f"Unknown startup setting {name!r}")
    return value


def configure(
    *,
    backend: str | None = None,
    terminal_renderer: str | None = None,
    theme: str | None = None,
    ui_scale: float | None = None,
) -> Settings:
    """Set startup defaults before constructing UI objects; never read argv/env.

    Opted-in command-line values override these defaults. Theme and scale defaults
    do not replace an explicit window constructor argument.
    """
    values = {name: value for name, value in locals().items() if value is not None}
    values = {name: _validate(name, value) for name, value in values.items()}
    with _lock:
        if _frozen is not None:
            raise LifecycleError("Configure Pysual before constructing UI objects")
        _defaults.update(values)
        return current()


def current() -> Settings:
    with _lock:
        if _frozen is not None:
            return _frozen
        values = {**_environment, **_defaults, **_command_line}
        return Settings(
            backend=cast(str | None, values.get("backend")),
            terminal_renderer=cast(str | None, values.get("terminal_renderer")),
            theme=cast(str | None, values.get("theme")),
            ui_scale=cast(float | None, values.get("ui_scale")),
        )


def freeze() -> Settings:
    global _frozen
    with _lock:
        if _frozen is None:
            _frozen = current()
        return _frozen


def window_defaults() -> dict[str, object]:
    settings = current()
    defaults: dict[str, object] = {}
    if settings.ui_scale is not None:
        defaults["ui_scale"] = settings.ui_scale
    if settings.theme is not None:
        from .theme import get_theme

        defaults["theme"] = get_theme(settings.theme)
    return defaults


def _convert(option, value):
    if option.field == "ui_scale":
        try:
            value = float(value)
        except (ValueError, TypeError):
            raise ValueError(f"{option.flag} requires a number") from None
    return _validate(option.field, value)


def activate() -> None:
    """Consume only recognized arguments, transactionally and once per process."""
    global _activated
    with _lock:
        if _activated:
            return
        if _frozen is not None:
            raise LifecycleError(
                "Import pysual.autoconfig before constructing UI objects"
            )
        arguments = list(sys.argv)
        remaining = arguments[:1]
        selected: dict[str, object] = {}
        help_requested = False
        index = 1
        while index < len(arguments):
            token = arguments[index]
            if token == "--":
                remaining.extend(arguments[index:])
                break
            flag, separator, value = token.partition("=")
            if flag == "--pysual-help":
                if separator:
                    raise ValueError("--pysual-help does not take a value")
                help_requested = True
                index += 1
                continue
            option = _BY_FLAG.get(flag)
            if option is None:
                if flag.startswith("--pysual-"):
                    raise ValueError(
                        f"Unknown Pysual option {flag!r}; use --pysual-help"
                    )
                remaining.append(token)
                index += 1
                continue
            if option.field in selected:
                raise ValueError(f"Repeated Pysual option {flag}")
            if not separator:
                index += 1
                if index >= len(arguments) or arguments[index].startswith("--"):
                    raise ValueError(f"{flag} requires a value")
                value = arguments[index]
            if not value:
                raise ValueError(f"{flag} requires a value")
            selected[option.field] = _convert(option, value)
            index += 1
        environment = {
            option.field: _convert(option, os.environ[option.environment])
            for option in OPTIONS
            if option.environment in os.environ
            and option.field not in selected
            and option.field not in _defaults
        }
        if help_requested:
            print("Pysual options (opt in with 'from pysual import autoconfig'):")
            for option in OPTIONS:
                print(f"  {option.flag} VALUE  {option.description}")
            print("  --pysual-help  Show this help; application --help is untouched")
            raise SystemExit(0)
        _environment.update(environment)
        _command_line.update(selected)
        sys.argv[:] = remaining
        _activated = True


def resolved_backend(
    preference: Host | BackendName | None = None,
) -> Host | BackendName | None:
    """CLI overrides a named preference; concrete test Hosts remain explicit."""
    if preference is not None and not isinstance(preference, str):
        return preference
    with _lock:
        return cast(
            BackendName | None,
            _command_line.get("backend") or preference or current().backend,
        )
