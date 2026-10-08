"""Immutable, on-demand diagnostics; never exposes tasks, handlers or cache handles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from weakref import ref

from ._engine import ui_method
from .events import callback_label, source_label
from .geometry import Rect
from .painting import resolve_style
from .schema import Dirty
from .theme import Style

if TYPE_CHECKING:
    from .controls import Control


@dataclass(frozen=True)
class HandlerSnapshot:
    event: str
    identity: str
    convention: bool


@dataclass(frozen=True)
class TaskSnapshot:
    name: str
    done: bool
    cancelling: bool


@dataclass(frozen=True)
class ControlSnapshot:
    identity: str
    control_type: str
    name: str | None
    bounds: Rect
    clip: Rect
    paint_clip: Rect
    styles: tuple[tuple[str, Style], ...]
    handlers: tuple[HandlerSnapshot, ...]
    tasks: tuple[TaskSnapshot, ...]
    dirty: Dirty
    paint_revision: int
    layout: str | None
    visible: bool
    enabled: bool
    focused: bool
    drop_target: bool
    cache_enabled: bool
    cache_resident: bool
    cache_bytes: int

    @property
    def owned_task_count(self) -> int:
        return len(self.tasks)



@ui_method
def inspect_control(control: Control) -> ControlSnapshot:
    """Take one snapshot on the UI thread; no layout, drawing or callback executes."""
    control._check_live()
    runtime = getattr(control._root(), "_runtime", None)
    handlers = tuple(
        HandlerSnapshot(name, callback_label(callback),
                        callback is channel._convention or callback is channel._namespace)
        for name, channel in sorted(control._channels.items())
        for callback in channel._snapshot()
    )
    tasks: tuple[TaskSnapshot, ...] = ()
    if control._dispatcher is not None:
        tasks = tuple(
            sorted(
                (
                    TaskSnapshot(task.get_name(), task.done(), bool(task.cancelling()))
                    for task, owner in control._dispatcher._owners.items()
                    if owner is control and not task.done()
                ),
                key=lambda task: task.name,
            )
        )
    entry = (
        runtime.render_cache.entries.get(ref(control)) if runtime is not None else None
    )
    return ControlSnapshot(
        source_label(control),
        f"{type(control).__module__}.{type(control).__qualname__}",
        control.name,
        control.bounds,
        control._clip,
        control._paint_clip,
        tuple((part, resolve_style(control, part)) for part in control.style_parts),
        handlers,
        tasks,
        control._dirty,
        control._paint_revision,
        getattr(control, "layout", None),
        control.visible,
        control.effective_enabled,
        control.focused,
        runtime is not None and runtime.router.drop_target is control,
        control.cache_paint,
        entry is not None,
        entry[2] if entry is not None else 0,
    )


@ui_method
def inspect_tree(root: Control) -> tuple[ControlSnapshot, ...]:
    """Take preorder snapshots of the current control subtree."""
    snapshots = [inspect_control(root)]
    for child in getattr(root, "_children", ()):
        snapshots.extend(inspect_tree(child))
    return tuple(snapshots)
