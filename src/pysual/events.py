"""Typed event channels and asyncio delivery. No control or backend imports."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Coroutine
from dataclasses import dataclass, field
from functools import partial
from time import monotonic
from types import UnionType
from typing import (
    Any,
    Awaitable,
    Callable,
    Generic,
    Literal,
    TypeVar,
    Protocol,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    overload,
)

from .errors import BindingError, EventOverloadError, LifecycleError
from .schema import check_ui_thread
from ._engine import call, capture_origin, critical_phase, is_ui_thread, ui_method, _origin, _phase


@dataclass(frozen=True, kw_only=True)
class UiEvent:
    source: object
    timestamp: float = field(default_factory=monotonic)
    origin: Literal["user", "program", "system"] = "program"


@dataclass(frozen=True, kw_only=True)
class ClickEvent(UiEvent):
    x: float | None = None
    y: float | None = None


T = TypeVar("T")


@dataclass(frozen=True, kw_only=True)
class ChangeEvent(UiEvent, Generic[T]):
    old_value: T
    new_value: T
    property_name: str = ""


@dataclass(frozen=True, kw_only=True)
class PointerEvent(UiEvent):
    x: float
    y: float
    pointer_id: int = 0
    pointer_kind: str = "mouse"


PayloadT = TypeVar("PayloadT", covariant=True)


@dataclass(frozen=True)
class DragPayload(Generic[PayloadT]):
    """Typed application data shared by one pointer drag; the envelope is immutable."""

    kind: str
    data: PayloadT

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind or len(self.kind) > 128:
            raise ValueError(
                "Drag payload kind must be a nonempty string of at most 128 characters"
            )


@dataclass(frozen=True, kw_only=True)
class DragEvent(PointerEvent):
    """Shared drag notification; acceptance uses Control.accepts_drop synchronously."""

    payload: DragPayload[object]
    drag_source: object
    target: object | None = None
    accepted: bool = False
    cancelled: bool = False


@dataclass(frozen=True, kw_only=True)
class KeyEvent(UiEvent):
    key: str
    shift: bool = False
    ctrl: bool = False


@dataclass
class _Decision:
    cancelled: bool = False
    sealed: bool = False


@dataclass(frozen=True, kw_only=True)
class ClosingEvent(UiEvent):
    _decision: _Decision = field(default_factory=_Decision, repr=False, compare=False)

    @property
    def cancelled(self) -> bool:
        return self._decision.cancelled

    def cancel(self) -> None:
        if not is_ui_thread():
            return call(self.cancel)
        check_ui_thread()
        if self._decision.sealed:
            raise LifecycleError("The cancellation decision is already sealed")
        self._decision.cancelled = True


@dataclass
class _ValidationDecision(_Decision):
    message: str = ""


@dataclass(frozen=True, kw_only=True)
class ValidationEvent(ClosingEvent):
    """An awaited validation decision; reject with optional user-facing feedback."""

    _decision: _ValidationDecision = field(
        default_factory=_ValidationDecision, repr=False, compare=False
    )

    @property
    def message(self) -> str:
        return self._decision.message

    def reject(self, message: str = "") -> None:
        if not is_ui_thread():
            return call(self.reject, message)
        if type(message) is not str:
            raise TypeError("Validation message must be a string")
        self.cancel()
        if message and not self._decision.message:
            self._decision.message = message


E = TypeVar("E", bound=UiEvent)
DecisionT = TypeVar("DecisionT", bound=ClosingEvent)
Callback = Callable[[E], None | Awaitable[None]]
ValidationCallback = Callable[[ValidationEvent], bool | None | Awaitable[bool | None]]


def source_label(owner: object) -> str:
    """Use binding identity, then the scope's visual ancestry; never user repr()."""
    parts = [getattr(owner, "_name", None) or type(owner).__name__]
    scope = getattr(owner, "_binding_owner", None) or getattr(owner, "_parent", None)
    while scope is not None:
        parts.append(getattr(scope, "_name", None) or type(scope).__name__)
        scope = getattr(scope, "_parent", None)
    return ".".join(reversed(parts))


def callback_label(callback: object) -> str:
    return getattr(callback, "__qualname__", type(callback).__qualname__)


def annotate_error(error: BaseException, context: str) -> None:
    """Retain the original exception type, traceback and any application notes."""
    if context not in getattr(error, "__notes__", ()):
        error.add_note(context)


def validate_callback(
    callback: Callable[..., object], event_type: type[UiEvent]
) -> None:
    parameters = list(inspect.signature(callback).parameters.values())
    if len(parameters) != 1 or parameters[0].kind not in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    ):
        raise BindingError("Handlers must accept exactly one event argument")
    target = callback
    while isinstance(target, partial):
        target = target.func
    if not inspect.isroutine(target):
        target = getattr(target, "__call__")
    annotation = get_type_hints(target).get(parameters[0].name)

    def accepts(annotation: Any) -> bool:
        if annotation is None or annotation is Any:
            return True
        origin = get_origin(annotation)
        if origin in (Union, UnionType):
            return any(accepts(branch) for branch in get_args(annotation))
        annotation = origin or annotation
        return isinstance(annotation, type) and issubclass(event_type, annotation)

    if not accepts(annotation):
        raise BindingError(f"Handler cannot accept {event_type.__name__}")


class EventOwner(Protocol):
    _channels: dict[str, Channel[Any]]
    _dispatcher: Dispatcher | None
    _disposed: bool

    def _check_live(self) -> None: ...


class Event(Generic[E]):
    def __init__(self, event_type: type[E], *, doc: str = ""):
        self.event_type = event_type
        self.doc = doc
        self.name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    def _make_channel(self, owner: EventOwner) -> Channel[E]:
        return Channel(owner, self)

    @overload
    def __get__(self, owner: None, cls: type | None = None) -> Event[E]: ...
    @overload
    def __get__(self, owner: EventOwner, cls: type | None = None) -> Channel[E]: ...
    def __get__(
        self, owner: EventOwner | None, cls: type | None = None
    ) -> Event[E] | Channel[E]:
        if owner is None:
            return self
        if not is_ui_thread():
            return call(self.__get__, owner, cls)
        owner._check_live()
        if self.name not in owner._channels:
            owner._channels[self.name] = self._make_channel(owner)
        return owner._channels[self.name]


class Subscription:
    def __init__(self, disconnect: Callable[[], None]):
        self._disconnect = disconnect

    @ui_method
    def disconnect(self) -> None:
        check_ui_thread()
        self._disconnect()


class Channel(Generic[E]):
    def __init__(self, owner: EventOwner, declaration: Event[E]):
        self.owner = owner
        self.declaration = declaration
        self._listeners: list[Callback[E]] = []
        self._convention: Callback[E] | None = None
        self._namespace: Callback[E] | None = None

    @ui_method
    def connect(self, callback: Callback[E]) -> Subscription:
        self.owner._check_live()
        try:
            validate_callback(callback, self.declaration.event_type)
        except Exception as error:
            annotate_error(
                error,
                f"Connecting {callback_label(callback)} to "
                f"{source_label(self.owner)}.{self.declaration.name}",
            )
            raise
        if callback not in self._listeners:
            self._listeners.append(callback)
        return Subscription(
            lambda: (
                self._listeners.remove(callback)
                if callback in self._listeners
                else None
            )
        )

    def _snapshot(self) -> tuple[Callback[E], ...]:
        bound: list[Callback[E]] = []
        for callback in (self._convention, self._namespace):
            if callback is not None and callback not in bound:
                bound.append(callback)
        # connect() already deduplicates explicit listeners. Only the two
        # binding sources can overlap with them at dispatch time.
        if not bound:
            return tuple(self._listeners)
        return (
            *bound,
            *(callback for callback in self._listeners if callback not in bound),
        )

    @ui_method
    def emit(self, event: E) -> None:
        self.owner._check_live()
        if (
            not isinstance(event, self.declaration.event_type)
            or event.source is not self.owner
        ):
            raise TypeError("Event class/source does not match its channel")
        if isinstance(event, ClosingEvent):
            raise TypeError("Cancellable events require awaited decision dispatch")
        dispatcher = self.owner._dispatcher
        if dispatcher is not None:
            dispatcher.emit(self, event)


class _ValidationChannel(Channel[ValidationEvent]):
    @ui_method
    def connect(self, callback: ValidationCallback) -> Subscription:
        # Storage and lifecycle are shared with ordinary channels; only this
        # channel's public callback contract admits a boolean decision.
        return super().connect(cast(Callback[ValidationEvent], callback))


class Validation(Event[ValidationEvent]):
    """A typed validation channel accepting False as well as event.reject()."""

    def __init__(self, *, doc: str = ""):
        super().__init__(ValidationEvent, doc=doc)

    def _make_channel(self, owner: EventOwner) -> _ValidationChannel:
        return _ValidationChannel(owner, self)

    @overload
    def __get__(self, owner: None, cls: type | None = None) -> Validation: ...
    @overload
    def __get__(self, owner: EventOwner, cls: type | None = None) -> _ValidationChannel: ...
    def __get__(
        self, owner: EventOwner | None, cls: type | None = None
    ) -> Validation | _ValidationChannel:
        if owner is None:
            return self
        return cast(_ValidationChannel, super().__get__(owner, cls))


class Dispatcher:
    """Own callback tasks on asyncio; enforce overload and lifecycle boundaries."""

    def __init__(self, limit: int | None = 1000):
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ValueError("Handler limit must be a positive integer or None")
        self.limit = limit
        self.pending = 0
        self._running = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._errors: list[BaseException] = []
        self._stopped = False
        self._owners: dict[asyncio.Task[Any], object] = {}
        self._counted: set[asyncio.Task[Any]] = set()
        self._survivors: set[asyncio.Task[Any]] = set()

    def _record_error(self, error: BaseException) -> None:
        # An overload both raises at the call site and signals fatal failure.
        # Its enclosing task can then finish with the same exception object.
        if not isinstance(error, EventOverloadError) or not any(
            error is existing for existing in self._errors
        ):
            self._errors.append(error)

    def _reserve(self, count: int, channel: Channel[Any], *, terminal: bool = False) -> None:
        check_ui_thread()
        if self._stopped and not terminal:
            raise LifecycleError("Event dispatcher has stopped")
        if self.limit is not None and self.pending + count > self.limit:
            error = EventOverloadError(
                f"{source_label(channel.owner)}.{channel.declaration.name}: "
                f"queued={self.pending - self._running}, running={self._running}, "
                f"pending={self.pending}, incoming={count}, limit={self.limit}"
            )
            self._record_error(error)
            raise error
        self.pending += count

    def emit(self, channel: Channel[E], event: E, *, terminal: bool = False) -> None:
        listeners = channel._snapshot()
        self._reserve(len(listeners), channel, terminal=terminal)
        remaining = len(listeners)
        try:
            for callback in listeners:
                token = _origin.set(capture_origin(channel.owner))
                try:
                    delivery = self._deliver(channel, callback, event, terminal=terminal)
                    try:
                        task = asyncio.create_task(
                            delivery,
                            name=f"{source_label(channel.owner)}.{channel.declaration.name} -> {callback_label(callback)}",
                        )
                    except BaseException:
                        if inspect.getcoroutinestate(delivery) != inspect.CORO_CREATED:
                            remaining -= 1
                        delivery.close()
                        raise
                    remaining -= 1
                    # Eager factories can enter _deliver before _track. Once
                    # entered, its finally block owns the reservation release.
                    self._track(
                        task, channel.owner,
                        counted=inspect.getcoroutinestate(delivery) == inspect.CORO_CREATED,
                    )
                finally:
                    _origin.reset(token)
        finally:
            self.pending -= remaining

    async def _deliver(
        self, channel: Channel[E], callback: Callback[E], event: E, *, terminal: bool = False
    ) -> None:
        # Until entry, _finished owns cancellation-before-start cleanup. After
        # entry this finally block releases the invocation immediately, including
        # each individual listener in an awaited cancellation decision.
        task = asyncio.current_task()
        if task is not None:
            self._counted.discard(task)
        self._running += 1
        phase_token = _phase.set(None)
        try:
            if terminal or not channel.owner._disposed:
                result: object = callback(event)
                if inspect.isawaitable(result):
                    result = await result
                if isinstance(event, ValidationEvent) and result is False:
                    event.reject()
        except Exception as error:
            annotate_error(
                error,
                f"Handling {source_label(channel.owner)}.{channel.declaration.name} "
                f"with {callback_label(callback)}",
            )
            raise
        finally:
            _phase.reset(phase_token)
            self._running -= 1
            self.pending -= 1

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._survivors.discard(task)
        if task in self._counted:
            self.pending -= 1
            self._counted.discard(task)
        self._owners.pop(task, None)
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            annotate_error(error, f"UI task: {task.get_name()}")
            self._record_error(error)
            if self._stopped:
                task.get_loop().call_exception_handler({
                    "message": "Pysual callback failed after its window closed",
                    "exception": error,
                    "task": task,
                })

    def _track(
        self, task: asyncio.Task[Any], owner: object, counted: bool = False
    ) -> asyncio.Task[Any]:
        self._tasks.add(task)
        self._owners[task] = owner
        if counted:
            self._counted.add(task)
        task.add_done_callback(self._finished)
        return task

    def create_task(
        self, coroutine: Coroutine[Any, Any, Any], owner: object
    ) -> asyncio.Task[Any]:
        if self._stopped:
            coroutine.close()
            raise LifecycleError("Runtime is closing")
        token = _origin.set(capture_origin(owner))
        started = False
        async def execute() -> Any:
            nonlocal started
            started = True
            with critical_phase(None):
                return await coroutine
        try:
            execution = execute()
            try:
                task = asyncio.create_task(
                    execution, name=f"{source_label(owner)}: {callback_label(coroutine)}"
                )
            except BaseException:
                execution.close()
                if not started:
                    coroutine.close()
                raise
            self._track(task, owner)
            # Cancelling an asyncio task before its first turn never enters its
            # coroutine. Close the caller's inner coroutine in that case too.
            def release_unstarted(_: object) -> None:
                if not started:
                    coroutine.close()
            task.add_done_callback(release_unstarted)
            return task
        finally:
            _origin.reset(token)

    def preserve_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is not None and task in self._tasks:
            self._survivors.add(task)

    def release_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is not None:
            self._survivors.discard(task)

    def cancel_owner(self, owner: object, *, force: bool = False) -> None:
        for task, source in tuple(self._owners.items()):
            if source is owner and task is not asyncio.current_task() and (force or task not in self._survivors):
                task.cancel()

    def raise_errors(self) -> None:
        if self._errors:
            errors, self._errors = self._errors, []
            if len(errors) == 1:
                raise errors[0]
            raise BaseExceptionGroup("Event handlers failed", errors)

    async def decide(self, channel: Channel[DecisionT], event: DecisionT) -> bool:
        if event.source is not channel.owner or event._decision.sealed:
            raise LifecycleError("Invalid or already-dispatched decision event")
        listeners = channel._snapshot()
        self._reserve(len(listeners), channel)
        remaining = len(listeners)
        origin_token = _origin.set(capture_origin(channel.owner))
        try:
            for callback in listeners:
                remaining -= 1
                await self._deliver(channel, callback, event)
            return not event.cancelled
        finally:
            _origin.reset(origin_token)
            event._decision.sealed = True
            self.pending -= remaining

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
            # gather(done_tasks) may complete without yielding. Let done callbacks
            # release ownership before testing the task set again.
            await asyncio.sleep(0)
        self.raise_errors()

    async def shutdown(self) -> None:
        self._stopped = True
        tasks = self._tasks - self._survivors - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
        self.raise_errors()
