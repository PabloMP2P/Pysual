"""Completed paint inputs and finite metadata from the built-in catalog.

Built-ins declare their extra inputs beside their drawing implementation.
Catalog installation captures original hooks once, after normal UI wrapping.
Custom types and replacement hooks retain revision-based invalidation.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple, cast

from .host import text_row_height

if TYPE_CHECKING:
    from .controls import Control
    from .geometry import Rect
    from .host import Host
    from .theme import Theme


TextMeasurement = tuple[str, float, bool]
PaintState = int | tuple[object, ...]


class PaintInputs(NamedTuple):
    state: PaintState
    bounds: Rect
    clip: Rect
    paint_clip: Rect
    theme: Theme
    enabled: bool
    hover: bool
    pressed: bool
    focused: bool
    layout: object
    scale: float
    resources: int


# Installed only from the finite framework catalog; never append custom types.
_PAINT_HOOKS = ("paint", "paint_bounds", "paint_focus")
# Derived declaration metadata does not change runtime drawing behavior.
# Python materializes annotation dictionaries and copyreg's slot-name cache
# lazily during ordinary introspection, copying and pickling.
_DECLARATION_METADATA = (
    "__annotations__", "__annotations_cache__", "__annotate__", "__slotnames__",
)
_original_hooks: dict[int, object] = {}
_bodies: dict[type[Control], _BuiltinPaint] = {}
_installed = False
_implementation_revision = 0


@dataclass
class _ImplementationChecks:
    kinds: dict[type[Control], tuple[int, bool]]
    namespaces: dict[type, tuple[int, bool]]
    prepared: dict[Control, _PreparedPaint]
    active: bool = True


_checks: ContextVar[_ImplementationChecks | None] = ContextVar(
    "pysual_paint_implementations", default=None,
)


def _current_checks() -> _ImplementationChecks | None:
    checks = _checks.get()
    return checks if checks is not None and checks.active else None


def implementation_changed() -> None:
    """Invalidate implementation checks after normal control-class mutation."""
    global _implementation_revision
    _implementation_revision += 1


@contextmanager
def painting_scope() -> Generator[None, None, None]:
    """Check each implementation once per pass, including inherited helpers.

    A fresh pass also observes edits to ordinary Python mixin classes. Control
    metaclass edits invalidate checks immediately, including during callbacks.
    Reentrant painting owns an independent scope.
    """
    checks = _ImplementationChecks({}, {}, {})
    token = _checks.set(checks)
    try:
        yield
    finally:
        # A task created by a host callback can inherit this ContextVar. Closing
        # the scope releases its controls and prevents reuse in that later task.
        checks.active = False
        checks.prepared.clear()
        _checks.reset(token)


def _namespace(kind: type) -> dict[str, object]:
    members = dict(vars(kind))
    for name in _DECLARATION_METADATA:
        members.pop(name, None)
    return members


@dataclass(frozen=True)
class _Namespace:
    kind: type
    members: dict[str, object]

    def matches(self, checks: _ImplementationChecks | None) -> bool:
        previous = None if checks is None else checks.namespaces.get(self.kind)
        if previous is not None and previous[0] == _implementation_revision:
            return previous[1]
        current = _namespace(self.kind)
        matches = (current.keys() == self.members.keys()
                   and all(current[name] is value for name, value in self.members.items()))
        if checks is not None:
            checks.namespaces[self.kind] = _implementation_revision, matches
        return matches


def custom_paint(control: Control) -> bool:
    body = _bodies.get(type(control))
    if body is not None:
        return not body.matches(control)
    for name in _PAINT_HOOKS:
        hook = getattr(control, name)
        function = getattr(hook, "__func__", hook)
        if _original_hooks.get(id(function)) is not function:
            return True
    return False


@dataclass(frozen=True)
class _BuiltinPaint:
    resolution_order: tuple[type, ...]
    namespaces: tuple[_Namespace, ...]
    names: frozenset[str]
    inputs: Callable[[Control], tuple[object, ...]] | None
    prepare: Callable[[Control, Host], None] | None
    measurements: Callable[[Control], tuple[TextMeasurement, ...]] | None

    def matches(self, control: Control) -> bool:
        kind = type(control)
        # Any shadowed implementation member invalidates trust, including
        # private helpers and object.__setattr__ replacements. No hook list
        # needs updating when a painter gains another helper.
        if not self.names.isdisjoint(control.__dict__):
            return False
        checks = _current_checks()
        previous = None if checks is None else checks.kinds.get(kind)
        if previous is not None and previous[0] == _implementation_revision:
            return previous[1]
        # Equal resolved attributes do not imply equal super() dispatch.
        matches = (kind.__mro__ == self.resolution_order
                   and all(namespace.matches(checks) for namespace in self.namespaces))
        if checks is not None:
            checks.kinds[kind] = _implementation_revision, matches
        return matches


def install_builtin_painting(
    base: type[Control], kinds: tuple[type[Control], ...],
) -> None:
    """Capture the catalog's original methods once; registration is separate.

    Reinstalling public factory aliases must not bless replacement methods or
    expand paint trust to a user-registered control.
    """
    global _installed
    if _installed:
        return
    # Share original namespaces and their per-pass audits across the catalog.
    # Preserve each base independently: super() can reach shadowed members.
    # This table exists only during installation; runtime checks add no types.
    namespaces: dict[type, _Namespace] = {}
    for kind in (base, *kinds):
        for name in _PAINT_HOOKS:
            hook = getattr(kind, name)
            _original_hooks[id(hook)] = hook
        if kind is base or not kind._paint_reuse:
            continue
        for ancestor in kind.__mro__:
            if ancestor not in namespaces:
                namespaces[ancestor] = _Namespace(ancestor, _namespace(ancestor))
        inherited = tuple(namespaces[ancestor] for ancestor in kind.__mro__)
        _bodies[kind] = _BuiltinPaint(
            kind.__mro__,
            inherited,
            frozenset(name for namespace in inherited for name in namespace.members),
            cast("Callable[[Control], tuple[object, ...]] | None", getattr(kind, "_paint_inputs", None)),
            cast("Callable[[Control, Host], None] | None", getattr(kind, "_prepare_paint", None)),
            cast("Callable[[Control], tuple[TextMeasurement, ...]] | None",
                 getattr(kind, "_paint_measurements", None)),
        )
    _installed = True


def _body(control: Control) -> _BuiltinPaint | None:
    if control._motion is not None:
        return None
    body = _bodies.get(type(control))
    if body is not None and body.matches(control):
        return body
    return None


def _state(control: Control, body: _BuiltinPaint) -> tuple[object, ...]:
    return control._visual_epoch, (() if body.inputs is None else body.inputs(control))


def measurement_uses_resources(control: Control) -> bool:
    """Only unchanged catalog implementations may assume metric-only sizing.

    The implementation guard covers helpers, MRO changes and instance shadows,
    as well as measure itself. Custom subclasses keep the conservative default.
    """
    declared = control._values["measure_resources"]
    if declared is not None:
        return cast(bool, declared)
    body = _bodies.get(type(control))
    return body is None or not body.matches(control)


def paint_measurements(control: Control) -> tuple[TextMeasurement, ...]:
    """Demand known by this exact body's implementation, before drawing it."""
    body = _body(control)
    if body is None or body.measurements is None:
        return ()
    return body.measurements(control)


def paint_inputs(
    control: Control, host: Host, focus: Control | None, theme: Theme | None,
    *, local: bool = False, reuse: bool = True, previous: PaintInputs | None = None,
) -> PaintInputs:
    """The same completed dependency snapshot for segments and surfaces."""
    body = _body(control)
    if body is not None and body.prepare is not None and reuse and previous is not None:
        observed = _state(control, body)
        # A completed result already normalized these exact declared inputs.
        # Changed model/private inputs go straight to preparation, without an
        # extra geometry/environment snapshot on actively changing controls.
        if observed == previous.state:
            root = control._root()
            guard = _preparation_guard(control, root)
            current = _snapshot(control, host, focus, theme, observed)
            if (_body(control) is body and current == previous
                    and observed == _state(control, body)
                    and guard == _preparation_guard(control, root)
                    and control._root() is root):
                return previous
            # Snapshot callbacks can replace a helper. Never prepare through
            # the original contract after that contract has stopped matching.
            body = _body(control)
    if body is not None and body.prepare is not None:
        body.prepare(control, host)
        # Host measurement callbacks can replace hooks during preparation.
        # Stateless bodies need just the initial live validation.
        body = _body(control)
    state = (_state(control, body) if body is not None and reuse else
             control._paint_local_revision if local else control._paint_revision)
    return _snapshot(control, host, focus, theme, state)


def capture_preparation(control: Control, host: Host, prepare: Callable[[], object]) -> None:
    """Keep a reusable preparation result only where drawing consumes it.

    Normalization-only hooks pay no snapshot/storage cost. Actual focus used by
    a prepared body belongs to its declared inputs; tree focus is a separate
    drawing concern, so this private operation uses a neutral focus argument.
    """
    checks = _current_checks()
    if checks is None:
        prepare()
        return
    # A repeated attempt or failed preparation cannot expose an older result.
    checks.prepared.pop(control, None)
    body = _body(control)
    if body is None:
        prepare()
        return
    root = control._root()
    guard = _preparation_guard(control, root)
    before = _snapshot(control, host, None, None, _state(control, body))
    payload = prepare()
    # A callback can replace helpers used by the original input projection.
    # Stop trusting it before reading those helpers again.
    if (payload is None or _body(control) is not body
            or guard != _preparation_guard(control, root)):
        return
    observed = _state(control, body)
    completed = _snapshot(control, host, None, None, observed)
    if (_body(control) is body and control._root() is root
            and before == completed and observed == _state(control, body)
            and guard == _preparation_guard(control, root)):
        checks.prepared[control] = _PreparedPaint(host, root, body, guard, completed, payload)


def _preparation_guard(control: Control, root: Control) -> tuple[object, ...]:
    # Revisions also catch callbacks that change a value and then restore it.
    # Full input snapshots separately cover unannounced private-state changes.
    return (control._visual_epoch, control._paint_local_revision,
            root._paint_revision, _implementation_revision)


@dataclass(frozen=True)
class _PreparedPaint:
    host: Host
    root: Control
    body: _BuiltinPaint
    guard: tuple[object, ...]
    inputs: PaintInputs
    payload: object


def take_prepared_paint(control: Control, host: Host) -> object | None:
    """Consume preparation at its point of use, after intervening host calls."""
    return _prepared_payload(control, host, consume=True)


def peek_prepared_paint(control: Control, host: Host) -> object | None:
    """Inspect live preparation without consuming its later drawing proof."""
    return _prepared_payload(control, host, consume=False)


def _prepared_payload(control: Control, host: Host, *, consume: bool) -> object | None:
    checks = _current_checks()
    if checks is None:
        return None
    prepared = (checks.prepared.pop(control, None) if consume
                else checks.prepared.get(control))
    if prepared is None or prepared.host is not host:
        return None
    body = _body(control)
    if (body is None or body is not prepared.body
            or prepared.guard != _preparation_guard(control, prepared.root)
            or control._root() is not prepared.root):
        return None
    state = _state(control, body)
    current = _snapshot(control, host, None, None, state)
    if (current == prepared.inputs
            and _body(control) is body and state == _state(control, body)
            and prepared.guard == _preparation_guard(control, prepared.root)):
        return prepared.payload
    return None


def _snapshot(
    control: Control, host: Host, focus: Control | None, theme: Theme | None, state: PaintState,
) -> PaintInputs:
    layout_key = getattr(control, "_paint_layout_key", None)
    return PaintInputs(
        state,
        control._rect, control._clip, control._paint_clip,
        control._fast_theme() if theme is None else theme,
        control._committed_enabled(), control._hover, control._pressed,
        control is focus,
        (layout_key() if layout_key is not None else None, text_row_height(host, 0.0)),
        getattr(host, "render_scale", 1.0), getattr(host, "resource_revision", 0),
    )
