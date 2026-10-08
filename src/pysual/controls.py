"""Control identity, ownership, extension hooks, and stable basic-control exports."""

from __future__ import annotations

import inspect
from collections.abc import Mapping
import keyword
from difflib import get_close_matches
from functools import wraps
from types import MappingProxyType
from typing import (
    TYPE_CHECKING,
    Callable,
    ClassVar,
    Literal,
    ParamSpec,
    TypeVar,
    cast,
)

from ._factory_types import FactoryTypes
from ._registry import (
    _Factory,
    register_control as register_control,
    registered_controls as registered_controls,
)
from ._blueprint import Blueprint, declarations as _blueprint_declarations
from .errors import BindingError, LifecycleError, SchemaError
from .events import (
    Channel,
    ChangeEvent,
    ClickEvent,
    Dispatcher,
    DragEvent,
    DragPayload,
    Event,
    KeyEvent,
    PointerEvent,
    UiEvent,
    annotate_error,
    source_label,
    validate_callback,
)
from .geometry import Rect
from .painting import resolve_style
from .theme import Theme, dark
from .schema import (
    Dirty,
    SchemaObject,
    _LAYOUT_MASK,
    _VISUAL_MASK,
    _init_argument,
    _mask,
    check_ui_thread,
    prop,
    unknown_name,
)
from ._engine import (
    TaskHandle,
    call,
    critical_phase,
    get_engine,
    is_ui_thread,
    ui_method,
    ui_operation,
)

if TYPE_CHECKING:
    from .inspection import ControlSnapshot


# Detached controls share immutable defaults and their bounded style cache.
_DETACHED_THEME = dark()


def _control_mutator(function):
    """Run custom public mutation preflight and commit as one owner operation."""
    owner_operation = ui_method(function)

    @wraps(function)
    def mutation(self, name, *args, **kwargs):
        # Private implementation state keeps its existing direct behavior,
        # including assignments before a custom constructor calls super().
        if name.startswith("_"):
            return function(self, name, *args, **kwargs)
        return owner_operation(self, name, *args, **kwargs)

    return ui_operation(mutation)


class _ControlMeta(type):
    """Explicit parenting happens only after the complete Python constructor."""

    def __new__(metaclass, name, bases, namespace, **kwargs):
        # Wrapping happens at class creation, preserving concrete identity,
        # annotations and override dispatch without user-facing decorators.
        cls = super().__new__(metaclass, name, bases, dict(namespace), **kwargs)
        for member_name in ("__setattr__", "__delattr__"):
            if member_name in namespace:
                # Framework gateways perform their own dispatch. An override
                # may read or normalize before super(), so route its complete
                # public operation instead of only the eventual base setter.
                framework_gateway = (
                    namespace.get("__module__") == __name__
                    and name in ("Control", "Container")
                )
                wrapper = ui_operation if framework_gateway else _control_mutator
                setattr(cls, member_name, wrapper(namespace[member_name]))
        resolved = {}
        for base in cls.__mro__:
            for member_name, member in vars(base).items():
                if not member_name.startswith("_"):
                    resolved.setdefault(member_name, member)
        for member_name, member in resolved.items():
            if inspect.isfunction(member):
                caller_gateway = any(
                    getattr(vars(base).get(member_name), "__pysual_caller_side__", False)
                    for base in cls.__mro__
                )
                if caller_gateway:
                    if member_name in vars(cls):
                        setattr(member, "__pysual_caller_side__", True)
                    continue
                if not getattr(member, "__pysual_ui_method__", False):
                    setattr(cls, member_name, ui_method(member))
            elif isinstance(member, property):
                accessors = (member.fget, member.fset, member.fdel)
                if all(function is None or getattr(function, "__pysual_ui_method__", False)
                       for function in accessors):
                    continue
                setattr(cls, member_name, property(
                    ui_method(member.fget) if member.fget else None,
                    ui_method(member.fset) if member.fset else None,
                    ui_method(member.fdel) if member.fdel else None,
                    member.__doc__,
                ))
        return cls

    def __call__(cls, *args: object, **kwargs: object):
        if not is_ui_thread():
            return call(lambda: cls(*args, **kwargs))
        get_engine()  # Freeze config for a direct-owner Pyodide constructor too.
        check_ui_thread()
        parent = kwargs.pop("parent", None)
        if parent is not None:
            if not isinstance(parent, Container):
                raise TypeError("parent must be a Container or None")
            parent._check_live()
            if getattr(cls, "_is_app", False):
                raise LifecycleError("Window cannot have a parent; use a Container")
        with critical_phase("construction"):
            control = super().__call__(*args, **kwargs)
            if not hasattr(control, "_values"):
                raise LifecycleError("Custom __init__ must call super().__init__()")
            control._check_live()
            try:
                if isinstance(control, Container):
                    control._materialize_blueprints()
                elif control._blueprints:
                    raise BindingError("Declare blueprints on an App or Container")
                if parent is not None:
                    parent.add(control)
            except BaseException:
                control.destroy()
                raise
        return control


class Control(SchemaObject, metaclass=_ControlMeta):
    if TYPE_CHECKING:
        # The alias adds a typed constructor input without making parent writable
        # or including it in schema, instance values, or serialization.
        _parent_input: Container | None = _init_argument(default=None, alias="parent")
    context_menu: ClassVar[Event[PointerEvent]] = Event(PointerEvent)
    pointer_down: ClassVar[Event[PointerEvent]] = Event(PointerEvent)
    pointer_move: ClassVar[Event[PointerEvent]] = Event(PointerEvent)
    pointer_up: ClassVar[Event[PointerEvent]] = Event(PointerEvent)
    key_down: ClassVar[Event[KeyEvent]] = Event(KeyEvent)
    key_up: ClassVar[Event[KeyEvent]] = Event(KeyEvent)
    focused_changed: ClassVar[Event[UiEvent]] = Event(UiEvent)
    drag_enter: ClassVar[Event[DragEvent]] = Event(DragEvent)
    drag_over: ClassVar[Event[DragEvent]] = Event(DragEvent)
    drag_leave: ClassVar[Event[DragEvent]] = Event(DragEvent)
    drop: ClassVar[Event[DragEvent]] = Event(DragEvent)
    drag_end: ClassVar[Event[DragEvent]] = Event(DragEvent)
    left: float = prop(default=0.0, affects=Dirty.ARRANGE | Dirty.HIT_TEST)
    top: float = prop(default=0.0, affects=Dirty.ARRANGE | Dirty.HIT_TEST)
    width: float | None = prop(
        default=None, minimum=0, affects=Dirty.MEASURE | Dirty.HIT_TEST
    )
    height: float | None = prop(
        default=None, minimum=0, affects=Dirty.MEASURE | Dirty.HIT_TEST
    )
    enabled: bool = prop(default=True, affects=Dirty.PAINT | Dirty.HIT_TEST)
    visible: bool = prop(default=True, affects=Dirty.MEASURE | Dirty.HIT_TEST)
    min_width: float = prop(default=0.0, minimum=0, affects=Dirty.MEASURE)
    min_height: float = prop(default=0.0, minimum=0, affects=Dirty.MEASURE)
    max_width: float | None = prop(default=None, minimum=0, affects=Dirty.MEASURE)
    max_height: float | None = prop(default=None, minimum=0, affects=Dirty.MEASURE)
    margin: float = prop(default=0.0, minimum=0, affects=Dirty.MEASURE)
    flex: float = prop(default=0.0, minimum=0, affects=Dirty.MEASURE)
    anchor: str = prop(default="left,top", affects=Dirty.ARRANGE)
    grid_row: int = prop(default=-1, minimum=-1, affects=Dirty.MEASURE)
    grid_col: int = prop(default=-1, minimum=-1, affects=Dirty.MEASURE)
    grid_row_span: int = prop(default=1, minimum=1, affects=Dirty.MEASURE)
    grid_col_span: int = prop(default=1, minimum=1, affects=Dirty.MEASURE)
    dock: Literal["left", "top", "right", "bottom", "fill"] = prop(
        default="fill",
        affects=Dirty.MEASURE,
        doc="Edge consumed in a dock layout; one optional final fill child",
    )
    background: str | None = prop(default=None)
    foreground: str | None = prop(default=None)
    tooltip: str = prop(
        default="",
        doc="Single-line hover hint; shown after 500 ms without taking focus",
    )
    font_family: Literal[None, "ui", "mono"] = prop(
        default=None, affects=Dirty.MEASURE | Dirty.PAINT
    )
    font_size: float | None = prop(
        default=None, minimum=1, affects=Dirty.MEASURE | Dirty.PAINT
    )
    theme_override: Theme | None = prop(
        default=None, affects=Dirty.MEASURE | Dirty.PAINT
    )
    cache_paint: bool = prop(
        default=False,
        doc="Reuse stable body painting within the App render-cache budget",
    )
    cache_measure: bool = prop(
        default=True, affects=Dirty.MEASURE,
        doc="Reuse preferred sizes until geometry inputs change; disable for external sizing state",
    )
    focusable: bool = prop(default=False, affects=Dirty.HIT_TEST)
    tab_index: int = prop(default=0, affects=Dirty.HIT_TEST)
    _allow_state: ClassVar[bool] = False
    _style_kind: ClassVar[str] = "Control"
    style_parts: ClassVar[tuple[str, ...]] = ("body",)
    # Theme selectors composed before each class's own selector in MRO order.
    style_fallbacks: ClassVar[tuple[str, ...]] = ()
    # Inherited exclusions may be replaced by subclasses, including with ().
    style_excludes: ClassVar[tuple[str, ...]] = ()
    _overlay: ClassVar[bool] = False
    _blueprints: ClassVar[tuple[tuple[str, Blueprint], ...]] = ()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if any(isinstance(value, Control) for value in vars(cls).values()):
            raise SchemaError(
                "Create controls in build() or __init__; class-level live controls cannot be shared"
            )
        cls._blueprints = tuple(_blueprint_declarations(cls).items())
        if "_style_kind" not in vars(cls):
            cls._style_kind = cls.__name__
        if (
            not isinstance(cls.style_parts, tuple)
            or not cls.style_parts
            or any(not isinstance(p, str) or not p for p in cls.style_parts)
        ):
            raise SchemaError("style_parts must be a non-empty tuple of part names")
        for name in ("style_fallbacks", "style_excludes"):
            kinds = getattr(cls, name)
            if not isinstance(kinds, tuple) or any(
                not isinstance(kind, str) or not kind.isidentifier() for kind in kinds
            ):
                raise SchemaError(f"{name} must be a tuple of style selector names")

    def _initialize(self) -> None:
        self._parent: Container | None = None
        self._name: str | None = None
        self._binding_owner: Container | None = None
        self._channels: dict[str, Channel] = {}
        self._dispatcher: Dispatcher | None = None
        self._rect = Rect()
        self._clip = Rect()
        self._paint_clip = Rect()
        self._fixed_row_height = 0.0
        self._measurement_row_height = None
        self._layout_row_height = None
        self._pressed = self._hover = False
        self._motion = None
        self._anchor_base = None
        self._paint_revision = 0
        self._paint_local_revision = 0
        self._measure_revision = 0
        self._measure_local_revision = 0
        self._measure_cache = []
        self._measure_cache_host = None
        self._measure_cache_enabled = self._values["cache_measure"]
        self._measure_uncached = int(not self._measure_cache_enabled)
        self._measure_coalescing = False
        self._batch_dirty: Dirty | None = None
        self._batch_requested = False
        self._release_paint = None
        self._origin: Literal["user", "program", "system"] = "program"
        self._wake: Callable[[], None] | None = None

    def _validate_live_update(self, name, value):
        super()._validate_live_update(name, value)
        if name == "visible" and not value:
            runtime = getattr(self._root(), "_runtime", None)
            modality = getattr(runtime, "_modality", None)
            if modality is not None:
                modality.validate_visibility(self, value)

    def _validate_update(self, name, value):
        if name in (
            "width", "height", "min_width", "min_height", "max_width", "max_height",
        ):
            values = self._values

            def current(key):
                return value if key == name else values[key]

            for axis in ("width", "height"):
                maximum = current("max_" + axis)
                if maximum is not None and maximum < current("min_" + axis):
                    raise ValueError(f"max_{axis} must be >= min_{axis}")
        elif name == "anchor" and not {part.strip() for part in value.split(",")} <= {
            "left",
            "top",
            "right",
            "bottom",
        }:
            raise ValueError(
                "anchor accepts left, top, right, bottom separated by commas"
            )
        elif name in ("foreground", "background") and value is not None:
            from .theme import _color

            _color(value)

    def _control_height(self, host, text="M"):
        style = resolve_style(self)
        line = host.measure(text, style.font_size, style.font_family == "mono")[1]
        return max(self.effective_theme.tokens.control_height, line + style.padding * 2)

    def _should_reveal_focus(self):
        # Pointer focus must leave the viewport in place until hit-row selection
        # runs. Keyboard/programmatic focus carries the router's reveal request.
        runtime = getattr(self._root(), "_runtime", None)
        return runtime is None or runtime.router._focus_to_reveal is self

    def update(self, **properties: object) -> None:
        """Commit several properties atomically with one ancestor invalidation."""
        properties = dict(self._prepare_update(properties))
        self._validate_candidate(properties)
        self._commit_update(properties)

    def _commit_update(self, properties: Mapping[str, object]) -> None:
        """Commit an already preflighted candidate with coalesced invalidation."""
        previous = self._batch_dirty
        previous_requested = self._batch_requested
        self._batch_dirty = Dirty.NONE
        self._batch_requested = False
        try:
            self._apply_validated(properties)
        finally:
            affects = self._batch_dirty
            requested = self._batch_requested
            assert affects is not None
            self._batch_dirty = previous
            self._batch_requested = previous_requested
            if requested:
                self._flush_invalidation(affects)

    def _flush_invalidation(self, affects):
        # Public overrides may make new local changes while handling the flush.
        if type(self).invalidate is not Control.invalidate:
            self.invalidate(affects)
            return
        # A batch already recorded each originating change. Its final wake is
        # propagated dirt, not a new local change to the batching container.
        previous = self._measure_coalescing
        self._measure_coalescing = True
        try:
            self.invalidate(affects)
        finally:
            self._measure_coalescing = previous

    def _adjust_uncached_measurements(self, delta):
        node = self
        while node is not None:
            node._measure_uncached += delta
            node = node._parent

    def _invalidate_measure(self, affects, *, local=True):
        if not _mask(affects) & _LAYOUT_MASK:
            return
        enabled = self._values["cache_measure"]
        if enabled != self._measure_cache_enabled:
            self._adjust_uncached_measurements(1 if not enabled else -1)
            self._measure_cache_enabled = enabled
        if local:
            self._measure_local_revision += 1
        node = self
        while node is not None:
            node._measure_revision += 1
            node._measure_cache.clear()
            node = node._parent

    def _record_dirty(self, affects):
        super()._record_dirty(affects)
        # Preserve committed sizing changes even when a custom _changed hook
        # consumes the notification instead of calling super().
        self._invalidate_measure(affects)

    def invalidate(self, affects: Dirty = Dirty.PAINT) -> None:
        """Request a frame after changing private drawing state in a custom control."""
        self._check_live()
        self._invalidate_model(affects)

    def _invalidate_model(self, affects):
        """Record dirt on this control and its ancestors, then wake the runtime.

        Runs on the UI owner: every public write already arrives there.
        """
        mask = _mask(affects)
        visual = mask & _VISUAL_MASK
        if not self._measure_coalescing:
            # Batch flushes only propagate dirt; their originating changes
            # already invalidated measurements, input metadata and paint bodies.
            self._invalidate_measure(mask)
            if visual:
                self._paint_local_revision += 1
            root = self
            while root._parent is not None:
                root = root._parent
            runtime = getattr(root, "_runtime", None)
            router = getattr(runtime, "router", None)
            if router is not None:
                router.invalidate(affects, self)
            retained = getattr(runtime, "_retained_paint", None)
            if retained is not None:
                retained.invalidate(self, affects)
        node: Control | None = self
        while node is not None:
            if node._batch_dirty is not None:
                node._batch_dirty = int(node._batch_dirty) | mask
                node._batch_requested = True
                break
            node._dirty = int(node._dirty) | mask
            if visual:
                node._paint_revision += 1
            if node._wake:
                node._wake()
            node = node._parent

    def _changed(self, field, old, value):
        affects = self._change_affects(field, old, value)
        self.invalidate(affects)
        if field.definition.changed and self._dispatcher is not None:
            getattr(self, field.definition.changed).emit(
                ChangeEvent(
                    source=self,
                    old_value=old,
                    new_value=value,
                    property_name=field.name,
                    origin=self._origin,
                )
            )

    def _change_affects(self, field, old, value):
        if field.name in ("left", "top", "anchor"):
            self._anchor_base = None
        affects = field.definition.affects
        if (
            field.name == "background" and isinstance(self, Container)
            and (old is None) != (value is None)
        ):
            # A transparent layout wrapper and a painted panel have different
            # descendant effect clips. Recompute only when that boundary changes.
            affects |= Dirty.ARRANGE
        return affects

    @property
    def bounds(self):
        self._check_live()
        return self._rect

    def _committed_enabled(self):
        """Enabled state for layout and paint, including a custom policy."""
        if type(self).effective_enabled is not Control.effective_enabled:
            return self.effective_enabled
        return self._fast_enabled()

    def _fast_enabled(self):
        """Default enabled chain. Custom policies enter through `_committed_enabled`."""
        node = self
        while node._values["enabled"]:
            parent = node._parent
            if parent is None:
                return True
            # Preserve custom ancestor policies; the ordinary chain only needs
            # the one public lifecycle check on the public property.
            if type(parent).effective_enabled is not Control.effective_enabled:
                return parent.effective_enabled
            node = parent
        return False

    def _fast_theme(self) -> Theme:
        """Committed theme chain for layout and paint, without a thread check."""
        node: Control | None = self
        while node is not None:
            values = node._values
            override = values["theme_override"]
            if override is not None:
                return cast(Theme, override)
            if "theme" in values:
                return cast(Theme, values["theme"])
            node = node._parent
        return _DETACHED_THEME

    @property
    def effective_enabled(self):
        self._check_live()
        return self._fast_enabled()

    @property
    def effective_theme(self) -> Theme:
        self._check_live()
        # Attached ancestors are live. Read the validated storage after checking
        # this public boundary, rather than checking every ancestor descriptor.
        return self._fast_theme()

    def measure(self, host):
        """Return the preferred (width, height) in logical pixels.

        Layout applies constraints separately. Override this synchronous hook
        when a custom control needs content-dependent sizing. Preferred sizes
        are cached: sizing properties must declare MEASURE, and private or
        external sizing changes must invalidate(MEASURE). Set cache_measure=False
        for queries that cannot follow that contract. See
        [custom controls](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#themes-and-custom-painting).
        """
        values = self._values
        return (
            values["width"] or 100,
            values["height"] or self._fast_theme().tokens.control_height,
        )

    def effective_row_height(self, fallback: float) -> float:
        """Return the tree's fixed text advance, or the requested pixel spacing.

        Arrangement establishes the value used for painting, picking, and
        scrolling. Measurement temporarily supplies its requested host's value,
        then restores arranged geometry. A detached tree uses the fallback
        before its first arrangement; attachment adopts the new tree's metric.
        """
        self._check_live()
        root = self._root()
        height = root._measurement_row_height
        return (root._fixed_row_height if height is None else height) or fallback

    def _measure_uses_available(self):
        return (
            type(self).measure_available is not Control.measure_available
            or "measure_available" in self.__dict__
        )

    def measure_available(
        self, host, *, width: float | None = None, height: float | None = None
    ):
        """Measure with parent space hints; ordinary custom measure hooks still work."""
        from .layout import _measurement_rows

        with _measurement_rows(self, host):
            return self.measure(host)

    def _layout_size(self, width, height):
        """Constrain arranged size without changing the requested dimensions."""
        return width, height

    def _measure_layout(self, host):
        from .layout import _size

        return _size(self, host)

    def paint(self, painter, /):
        """Draw this control with the supplied Painter in local logical pixels.

        The runtime calls this synchronous hook during painting. Override it
        for custom drawing; see
        [custom controls](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#themes-and-custom-painting).
        """
        if self.background:
            painter.rect(Rect(0, 0, painter.width, painter.height), self.background)

    def paint_focus(self, painter, /):
        """Draw keyboard focus after the cached body.

        Composite entries may override this when their owner draws a shared
        focus outline. Keep an equally visible indication on that owner.
        """
        radius = (painter.style().radius or 0) if "body" in self.style_parts else 0
        painter.focus_ring(painter.theme.tokens.accent, radius)

    def paint_bounds(self) -> Rect:
        """Declared effect bounds in app coordinates; never changes hit testing.

        Body theme shadows expand these automatically. Custom controls painting
        other external shadows can return e.g. self.bounds.inset(-12).
        """
        return self._rect

    def handle_input(self, event, /):
        """Handle routed pointer, keyboard or focus input synchronously.

        Override this hook for custom interaction. Returning True for a wheel
        event consumes it instead of trying a parent; see `scroll_input` and
        [custom controls](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#themes-and-custom-painting).
        """
        pass

    def scroll_input(self, event, /) -> bool:
        """Handle a wheel event; return True to consume it, False to try a parent.

        A custom handle_input continues receiving wheel events and can return
        True to consume them. Scrollers should accept fractional input when
        overflow exists in that direction, even before an offset changes.
        """
        return self.handle_input(event) is True

    def accepts_drop(self, payload: DragPayload[object], /) -> bool:
        """Synchronously accept an application payload; subclasses opt in."""
        return False

    def begin_drag(
        self, payload: DragPayload[object], *, pointer_id: int | None = None
    ) -> None:
        """Begin a typed drag from this control's active captured pointer."""
        self._check_live()
        runtime = getattr(self._root(), "_runtime", None)
        if runtime is None:
            raise LifecycleError("Drag gestures require a running App")
        runtime.router.begin_drag(self, payload, pointer_id=pointer_id)

    def cancel_drag(self) -> None:
        """Cancel a drag owned by this source, if one is active."""
        self._check_live()
        runtime = getattr(self._root(), "_runtime", None)
        if runtime is not None and runtime.router.drag_source is self:
            runtime.router.cancel_drag()

    @property
    def drop_target(self) -> bool:
        """Whether the active drag currently has this accepted target."""
        self._check_live()
        runtime = getattr(self._root(), "_runtime", None)
        return runtime is not None and runtime.router.drop_target is self

    def inspect(self) -> ControlSnapshot:
        """Take an immutable snapshot of geometry, styles, handlers and tasks."""
        from .inspection import inspect_control

        return inspect_control(self)

    def on_attached(self) -> None:
        """Initialize relationships after joining a container."""

    def invoke_shortcut(self, shortcut: str) -> bool:
        """Return True after dispatching a matching command through its usual event."""
        return False

    def create_task(self, coroutine):
        """Start owned work and return an awaitable task handle with `cancel()`.
        Requires a running App. Destroying this control cancels its work;
        native-window shutdown also cancels and joins owned work. See
        [tasks and lifetime](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#owned-tasks-and-callbacks).
        """
        try:
            self._check_live()
        except BaseException:
            coroutine.close()
            raise
        if self._dispatcher is None:
            coroutine.close()
            raise LifecycleError("Tasks require a running App")
        return TaskHandle(self._dispatcher.create_task(coroutine, self))

    def focus(self) -> None:
        """Move keyboard focus to this visible, enabled control."""
        self._check_live()
        runtime = getattr(self._root(), "_runtime", None)
        if runtime is None:
            raise LifecycleError("Focus requires a running App")
        if getattr(runtime, "_modality", None) is not None:
            if not runtime._modality.allowed(self):
                raise LifecycleError("Focus must remain inside the modal dialog")
        elif runtime.modal is not None and not (
            runtime.popup is not None and runtime.popup.contains(self)
        ):
            node: Control | None = self
            while node is not None and node is not runtime.modal:
                node = node._parent
            if node is None:
                raise LifecycleError("Focus must remain inside the modal dialog")
        runtime.router.set_focus(self)

    @property
    def focused(self) -> bool:
        self._check_live()
        runtime = getattr(self._root(), "_runtime", None)
        return runtime is not None and runtime.router.focus is self

    @classmethod
    def events(cls) -> dict[str, Event]:
        """Declared events, including inherited declarations."""
        return {
            name: value
            for base in reversed(cls.__mro__)
            for name, value in vars(base).items()
            if isinstance(value, Event)
        }

    def __setattr__(self, name: str, value: object) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if not is_ui_thread():
            return call(setattr, self, name, value)
        self._check_live()
        if name not in self._schema:
            member = inspect.getattr_static(type(self), name, None)
            if isinstance(member, property) and member.fset is not None:
                # A declared setter owns this operation. Its wrapper runs
                # the complete setter on UI, just like an ordinary method.
                return super().__setattr__(name, value)
            if callable(member) or isinstance(member, (property, Event, _Factory)):
                raise BindingError(
                    f"{name!r} is a framework member; use a distinct state name"
                )
            if not self._allow_state:
                raise AttributeError(unknown_name(name, self._schema))
        super().__setattr__(name, value)

    @property
    def name(self) -> str | None:
        self._check_live()
        return self._name

    @property
    def parent(self) -> Container | None:
        self._check_live()
        return self._parent

    def _root(self) -> Control:
        node = self
        while node._parent is not None:
            node = node._parent
        return node

    def _event_definitions(self) -> dict[str, Event]:
        return type(self).events()

    def _handler_candidates(self, owner: object, prefix: str):
        candidates = []
        for event_name, definition in self._event_definitions().items():
            method_name = f"{prefix}_on_{event_name}"
            callback = getattr(owner, method_name, None)
            if callback is not None:
                try:
                    validate_callback(callback, definition.event_type)
                except Exception as error:
                    annotate_error(
                        error,
                        f"Binding {source_label(owner)}.{method_name} to "
                        f"{type(self).__name__}.{event_name}",
                    )
                    raise
                candidates.append((event_name, callback))
        return candidates

    def _bind_handlers(self, owner: object, prefix: str) -> None:
        for event_name, callback in self._handler_candidates(owner, prefix):
            getattr(self, event_name)._convention = callback

    def _attach_dispatcher(self, dispatcher: Dispatcher | None) -> None:
        if self._dispatcher is not None and self._dispatcher is not dispatcher:
            self._dispatcher.cancel_owner(self)
        self._dispatcher = dispatcher

    def destroy(self) -> None:
        """Permanently detach this control, release resources and cancel owned work.
        Repeated destruction is harmless; the control cannot be reused. To keep
        a window's tree for reopening, use its `close()` method instead. See
        [window lifetime](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#lifetime-and-threads).
        """
        check_ui_thread()
        if self._disposed:
            return
        runtime = getattr(self._root(), "_runtime", None)
        if runtime is not None:
            modality = getattr(runtime, "_modality", None)
            if modality is not None:
                modality.destroying(self)
            runtime.motion.remove(self)
        if self._release_paint is not None:
            self._release_paint()
            self._release_paint = None
        if self._parent is not None:
            self._parent._adjust_uncached_measurements(-self._measure_uncached)
            self._parent._children.remove(self)
            self._parent.invalidate(Dirty.MEASURE | Dirty.HIT_TEST)
        if self._binding_owner is not None and self._name is not None:
            self._binding_owner._bindings.pop(self._name, None)
        if self._dispatcher is not None:
            self._dispatcher.cancel_owner(self, force=True)
        self._parent = None
        self._binding_owner = None
        self._dispatcher = None
        self._measure_cache.clear()
        self._measure_cache_host = None
        self._layout_measure_host = None
        self._layout_measure_environment = None
        self._channels.clear()
        self._disposed = True
        if runtime is not None:
            runtime.router.reconcile()


C = TypeVar("C", bound=Control)
P = ParamSpec("P")


def blueprint(
    control_type: Callable[P, C], /, *args: P.args, **properties: P.kwargs
) -> Blueprint[C]:
    """Declare fresh typed controls on a Container class without allocating UI."""
    if (
        not isinstance(control_type, type)
        or not issubclass(control_type, Control)
        or getattr(control_type, "_is_app", False)
    ):
        raise TypeError("Blueprints require a Control subclass, excluding App")
    if args:
        raise TypeError("Blueprint constructors accept keyword properties only")
    if "parent" in properties:
        raise TypeError(
            "Use blueprint(...).under('declaration') instead of a live parent"
        )
    fields = control_type.properties()
    for name, value in properties.items():
        if name not in fields:
            raise TypeError(unknown_name(name, fields))
        fields[name].validate(value)
    return Blueprint(cast(type[C], control_type), tuple(properties.items()))


class Container(Control, FactoryTypes):
    """Also the handler scope for composite controls; private state stays Python."""

    def _child_effect_clip(self, rect, content_clip, inherited_clip):
        # Transparent layout wrappers have no visible edge. Shadows can extend
        # to the nearest painted surface, while scroll/custom content viewports
        # retain their strict boundaries. Layout itself stays control-agnostic.
        if (
            type(self).content_bounds is not Container.content_bounds
            or hasattr(self, "scroll_x")
            or hasattr(self, "scroll_y")
        ):
            return content_clip.intersect(inherited_clip)
        if type(self).paint is Container.paint and self.background is None:
            return inherited_clip
        return self._clip.intersect(inherited_clip)

    def paint(self, painter, /):
        # Layout containers remain transparent until given a background. Once
        # visible, their body uses the same part styling as other surfaces.
        if self.background is not None:
            painter.body()

    layout: Literal["absolute", "stack", "grid", "flow", "dock"] = prop(
        default="absolute",
        affects=Dirty.MEASURE,
    )
    direction: Literal["vertical", "horizontal"] = prop(
        default="vertical", affects=Dirty.MEASURE
    )
    padding: float = prop(default=0.0, minimum=0, affects=Dirty.MEASURE)
    spacing: float = prop(default=12.0, minimum=0, affects=Dirty.MEASURE)
    columns: int = prop(default=2, minimum=1, affects=Dirty.MEASURE)
    column_tracks: tuple[float | str, ...] = prop(
        default=(),
        affects=Dirty.MEASURE,
        doc="Grid columns: fixed pixels, auto, or weighted stars; overrides columns",
    )
    row_tracks: tuple[float | str, ...] = prop(
        default=(),
        affects=Dirty.MEASURE,
        doc="Grid rows: fixed pixels, auto, or weighted stars; missing rows use stars",
    )
    _allow_state: ClassVar[bool] = True

    def _initialize(self) -> None:
        super()._initialize()
        self._children: list[Control] = []
        self._bindings: dict[str, Control] = {}
        self._blueprints_materialized = False

    def update_children(self, updates: Mapping[Control, Mapping[str, object]]) -> None:
        """Preflight descendant property batches and invalidate this container once.

        Every target must belong to this subtree. Validation failure leaves all
        controls unchanged; ordinary property notifications still run per field.
        """
        self._check_live()
        prepared = []
        for control, values in updates.items():
            if not isinstance(control, Control):
                raise TypeError("Update targets must be controls")
            node = control.parent
            while node is not None and node is not self:
                node = node.parent
            if node is not self:
                raise ValueError("Update targets must be descendants of this container")
            values = control._prepare_update(values)
            control._validate_candidate(values)
            prepared.append((control, values))
        previous = self._batch_dirty
        previous_requested = self._batch_requested
        self._batch_dirty = Dirty.NONE
        self._batch_requested = False
        try:
            for control, values in prepared:
                control._commit_update(values)
        finally:
            affects = self._batch_dirty
            requested = self._batch_requested
            assert affects is not None
            self._batch_dirty = previous
            self._batch_requested = previous_requested
            if requested:
                self._flush_invalidation(affects)

    def _materialize_blueprints(self) -> None:
        if self._blueprints_materialized:
            return
        definitions = dict(self._blueprints)
        ordered: list[str] = []
        visiting: set[str] = set()

        def visit(name: str) -> None:
            if name in ordered:
                return
            if name in visiting:
                raise BindingError("Blueprint parent references contain a cycle")
            visiting.add(name)
            declaration = definitions[name]
            if not issubclass(declaration.control_type, Control) or getattr(
                declaration.control_type, "_is_app", False
            ):
                raise BindingError(
                    "Blueprints require a Control subclass, excluding App"
                )
            parent_name = declaration.parent_name
            if parent_name is not None:
                if parent_name not in definitions or not issubclass(
                    definitions[parent_name].control_type, Container
                ):
                    raise BindingError(
                        f"Blueprint parent {parent_name!r} must name a Container declaration"
                    )
                visit(parent_name)
            visiting.remove(name)
            ordered.append(name)

        for name in definitions:
            if name in vars(self):
                raise BindingError(f"Do not replace blueprint {name!r} during __init__")
            visit(name)
        created = []
        try:
            for name in ordered:
                declaration = definitions[name]
                control = declaration.control_type(**dict(declaration.properties))
                created.append(control)
                if declaration.parent_name is not None:
                    parent = self._bindings[declaration.parent_name]
                    assert isinstance(parent, Container)
                    parent.add(control)
                setattr(self, name, control)
        except BaseException:
            for control in reversed(created):
                control.destroy()
            raise
        self._blueprints_materialized = True

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name in ("column_tracks", "row_tracks"):
            from .layout import _track

            for track in value:
                _track(track)

    def measure(self, host):
        return self.measure_content(host)

    def measure_content(
        self, host, *, width: float | None = None, height: float | None = None
    ):
        """Measure the declared child layout and padding for custom container chrome.

        Override measure_available to add headings or other non-content insets.
        This method does not invoke the custom measure override again.
        """
        from .layout import _measurement_rows, _natural_size, _size

        with _measurement_rows(self, host):
            if self.width is not None and self.height is not None:
                return self.width, self.height
            return _natural_size(
                self,
                lambda child, **limits: _size(child, host, **limits),
                width=width,
                height=height,
            )

    def measure_available(
        self, host, *, width: float | None = None, height: float | None = None
    ):
        """Use available space for intrinsic flow/stack content without changing properties."""
        from .layout import _measurement_rows

        with _measurement_rows(self, host):
            if (width is None and height is None) or type(
                self
            ).measure is not Container.measure:
                return self.measure(host)
            return self.measure_content(host, width=width, height=height)

    @property
    def children(self) -> tuple[Control, ...]:
        self._check_live()
        return tuple(self._children)

    def validate_layout(self) -> None:
        """Validate grid placement and dock order before mounting a subtree."""
        self._check_live()
        from .layout import validate

        validate(self)

    def check_handlers(self, *, include_unbound: bool = False) -> tuple[str, ...]:
        """Describe unmatched convention names in this subtree without rebinding.

        By default only current control/App prefixes and aliases are checked.
        include_unbound also reports names reserved for controls not created yet.
        """
        self._check_live()
        diagnostics = []
        scopes: list[Container] = [self]
        while scopes:
            scope = scopes.pop()
            scopes.extend(
                child
                for child in reversed(scope._children)
                if isinstance(child, Container)
            )
            namespace: dict[str, object] = {}
            for base in reversed(type(scope).__mro__):
                namespace.update(vars(base))
            namespace.update(vars(scope))
            targets = dict(scope._bindings)
            if getattr(scope, "_is_app", False):
                targets[type(scope).__name__] = scope
            pairs = {
                f"{prefix}_on_{event}"
                for prefix, target in targets.items()
                for event in target._event_definitions()
            }
            aliases = {
                name: value
                for name, value in vars(scope).items()
                if not name.startswith("_")
                and isinstance(value, Control)
                and not value._disposed
                and name not in targets
            }
            alias_pairs = {
                f"{name}_on_{event}": (value, event)
                for name, value in aliases.items()
                for event in value._event_definitions()
            }
            subscribed = [
                callback
                for target in (*targets.values(), *aliases.values())
                for channel in target._channels.values()
                for callback in channel._listeners
            ]
            prefixes = tuple(f"{name}_on_" for name in (*targets, *aliases))
            for name, member in sorted(namespace.items()):
                if isinstance(member, (staticmethod, classmethod)):
                    member = member.__func__
                if (
                    name.startswith("_")
                    or "_on_" not in name
                    or not callable(member)
                    or name in pairs
                ):
                    continue
                if subscribed and getattr(scope, name) in subscribed:
                    continue
                label = f"{source_label(scope)}.{name}"
                if name in alias_pairs:
                    control, event = alias_pairs[name]
                    owner = control._binding_owner
                    diagnostics.append(
                        f"{label} is not bound: aliases do not acquire handlers. "
                        f"Use {source_label(owner)}.{control._name}_on_{event}."
                    )
                elif name.startswith(prefixes) or include_unbound:
                    matches = get_close_matches(name, sorted(pairs), n=1)
                    hint = f" Did you mean {matches[0]!r}?" if matches else ""
                    diagnostics.append(
                        f"{label} has no matching current control/event binding.{hint}"
                    )
        return tuple(diagnostics)

    def content_bounds(self, bounds: Rect) -> Rect:
        """Return child content bounds in app-logical coordinates."""
        return bounds.inset(self.padding)

    def _reveal_bounds(self, bounds: Rect) -> Rect:
        """Return descendant bounds after optional scrolling by this container."""
        return bounds

    def arrange_children(
        self, area: Rect, measure: Callable[[Control], tuple[float, float]]
    ) -> dict[Control, Rect] | None:
        """Override with child/Rect pairs, or return None for the declared layout."""
        return None

    def _arrange_declared(self, area, measure):
        from .layout import _declared_boxes

        return _declared_boxes(self, area, measure)

    def create(
        self, control_type: Callable[P, C], /, *args: P.args, **properties: P.kwargs
    ) -> C:
        """Construct and attach a control without a named factory or registration.

        Constructor arguments and the concrete return type remain typed. This
        container chooses the parent, just like its named factory aliases.
        """
        self._check_live()
        if "parent" in properties:
            raise TypeError(
                "Factories choose their parent; use a direct constructor with parent="
            )
        if (
            not isinstance(control_type, type)
            or not issubclass(control_type, Control)
            or getattr(control_type, "_is_app", False)
        ):
            raise TypeError("Control creation requires a Control subclass, excluding App")
        control = control_type(*args, **properties)
        try:
            return self.add(control)
        except BaseException:
            control.destroy()
            raise

    def add(self, control: C) -> C:
        self._check_live()
        control._check_live()
        if control._parent is not None or control is self or self._root() is control:
            raise BindingError("Control already has a parent or would create a cycle")
        if getattr(control, "_is_app", False):
            raise BindingError("App cannot be nested; use a Container")
        from .layout import _clear_measurements

        _clear_measurements(control)
        control._parent = self
        self._children.append(control)
        self._adjust_uncached_measurements(control._measure_uncached)
        control._attach_dispatcher(self._dispatcher)
        try:
            control.on_attached()
            control._check_live()
        except BaseException:
            if control in self._children:
                self._adjust_uncached_measurements(-control._measure_uncached)
                self._children.remove(control)
            control._parent = None
            control._attach_dispatcher(None)
            _clear_measurements(control)
            raise
        self.invalidate(Dirty.MEASURE | Dirty.HIT_TEST)
        return control

    def __setattr__(self, name: str, value: object) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if not is_ui_thread():
            return call(setattr, self, name, value)
        if not hasattr(self, "_bindings"):
            return super().__setattr__(name, value)
        self._check_live()
        current = self._bindings.get(name)
        if current is not None and current is not value:
            raise BindingError(
                f"Destroy {name!r} before replacing its canonical binding"
            )
        member = inspect.getattr_static(type(self), name, None)
        if isinstance(member, property) and member.fset is not None:
            # Descriptor assignment is explicit behavior, including when its
            # argument is a Control; it is not automatic mounting/naming.
            return super().__setattr__(name, value)
        if isinstance(value, Control):
            value._check_live()
            declaration = inspect.getattr_static(type(self), name, None)
            if isinstance(declaration, Blueprint) and not isinstance(
                value, declaration.control_type
            ):
                raise TypeError(
                    f"Blueprint {name!r} requires {declaration.control_type.__name__}"
                )
            if (
                not name.isidentifier()
                or keyword.iskeyword(name)
                or name == type(self).__name__
                or (
                    not isinstance(declaration, Blueprint)
                    and any(name in vars(base) for base in type(self).__mro__)
                )
            ):
                raise BindingError(
                    f"{name!r} collides with a framework member; use a distinct name such as {name}_control"
                )
            adopting = value._parent is None
            if value is self or (not adopting and value._root() is not self._root()):
                raise BindingError(
                    "Binding owner and control must belong to the same tree"
                )
            candidates = []
            if value._name is None:
                # Resolve pairs, never split user method names on underscores.
                methods = {
                    f"{n}_on_{e}"
                    for n, c in self._bindings.items()
                    for e in c._event_definitions()
                }
                methods.update(
                    f"{type(self).__name__}_on_{e}" for e in self._event_definitions()
                )
                incoming = {f"{name}_on_{e}" for e in value._event_definitions()}
                if methods & incoming:
                    raise BindingError("Ambiguous control/event handler names")
                candidates = value._handler_candidates(self, name)
            # Validate the complete binding before any attachment hook can act
            # on siblings. Existing parents are never changed by assignment.
            if adopting:
                self.add(value)
            if value._name is None:
                for event_name, callback in candidates:
                    getattr(value, event_name)._convention = callback
                value._name, value._binding_owner = name, self
                self._bindings[name] = value
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if not is_ui_thread():
            return call(delattr, self, name)
        self._check_live()
        if name in self._bindings:
            raise BindingError(
                f"Destroy {name!r} before deleting its canonical binding"
            )
        super().__delattr__(name)

    def _attach_dispatcher(self, dispatcher: Dispatcher | None) -> None:
        super()._attach_dispatcher(dispatcher)
        for child in self._children:
            child._attach_dispatcher(dispatcher)

    def destroy(self) -> None:
        check_ui_thread()
        if self._disposed:
            return
        for child in tuple(self._children):
            child.destroy()
        super().destroy()


from ._controls.basic import Label as Label, Button as Button
