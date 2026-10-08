"""Snapshot caller conventions without retaining frames or traversing attributes."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any

from .controls import Container, Control
from .errors import BindingError
from .events import annotate_error, validate_callback


@dataclass(frozen=True)
class NamespaceHandlers:
    aliases: tuple[str, ...]
    callbacks: dict[str, Any]


def capture_handlers(window: Control) -> NamespaceHandlers | None:
    """Called directly by a caller-side gateway; inspect only its caller."""
    frame = inspect.currentframe()
    caller = None
    try:
        if frame is None or frame.f_back is None:
            return None
        caller = frame.f_back.f_back
        if caller is None:
            return None
        namespace = dict(caller.f_globals)
        namespace.update(caller.f_locals)
    finally:
        del caller
        del frame
    aliases = tuple(
        name for name, value in namespace.items()
        if isinstance(name, str) and name.isidentifier() and value is window
    )
    if not aliases:
        return None
    prefixes = tuple(f"{name}_" for name in aliases)
    return NamespaceHandlers(aliases, {
        name: callback for name, callback in namespace.items()
        if isinstance(name, str) and name.startswith(prefixes) and "_on_" in name
    })


def bind_handlers(window: Container, namespace: NamespaceHandlers | None) -> None:
    """Validate the complete snapshot before replacing its automatic bindings."""
    if namespace is None:
        return
    # Canonical bindings, not attributes or visual-parent paths, define names.
    targets: dict[str, list[tuple[Control, str]]] = {}
    pending: list[tuple[str, Control]] = [("", window)]
    visited: set[Control] = set()
    while pending:
        path, control = pending.pop()
        if control in visited or control._disposed:
            continue
        visited.add(control)
        for event_name in control._event_definitions():
            for alias in namespace.aliases:
                name = f"{alias}{path}_on_{event_name}"
                if name in namespace.callbacks and namespace.callbacks[name] is not None:
                    targets.setdefault(name, []).append((control, event_name))
        if isinstance(control, Container):
            pending.extend(
                (f"{path}_{name}", child)
                for name, child in control._bindings.items()
            )

    selected: dict[tuple[Control, str], tuple[str, Any]] = {}
    for name, matches in targets.items():
        if len(matches) != 1:
            raise BindingError(f"Namespace handler {name!r} matches multiple controls/events")
        target = matches[0]
        callback = namespace.callbacks[name]
        previous = selected.get(target)
        if previous is not None and previous[1] is not callback:
            raise BindingError(
                f"Namespace handlers {previous[0]!r} and {name!r} match the same event"
            )
        control, event_name = target
        try:
            validate_callback(callback, control._event_definitions()[event_name].event_type)
        except Exception as error:
            annotate_error(error, f"Binding caller namespace handler {name}")
            raise
        selected[target] = (name, callback)

    # A refresh removes only namespace bindings, including ones whose function
    # was deleted. Method conventions and explicit subscriptions stay intact.
    controls: list[Control] = [window]
    while controls:
        control = controls.pop()
        for channel in control._channels.values():
            channel._namespace = None
        if isinstance(control, Container):
            controls.extend(control._children)
    for (control, event_name), (_, callback) in selected.items():
        getattr(control, event_name)._namespace = callback
