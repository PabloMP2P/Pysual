"""One lazy Python UI owner, bounded dispatch and explicit interpreter lifetime.

No native resources are created here. The daemon owner never keeps a script
alive; applications wait explicitly. Native/runtime owners register shutdown
callbacks instead of introducing imports back into the control layer.
"""

from __future__ import annotations

import asyncio
import atexit
from collections import deque
from concurrent.futures import Future, InvalidStateError, TimeoutError as FutureTimeout
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from functools import wraps
import inspect
import sys
import threading
from time import monotonic
from typing import Any, Callable, TypeVar, cast

from .errors import LifecycleError


class DispatchTimeout(TimeoutError):
    """A timed-out UI operation; started operations must never be retried blindly."""

    def __init__(self, started: bool):
        self.started = started
        super().__init__(
            "UI operation timed out after execution began; its outcome is indeterminate"
            if started
            else "UI operation timed out before execution and was cancelled"
        )


class DispatchOverload(LifecycleError):
    """The bounded UI inbox cannot admit more work."""


_origin: ContextVar[tuple[tuple[object, object, int], ...]] = ContextVar(
    "pysual_event_origin", default=()
)
_phase: ContextVar[str | None] = ContextVar("pysual_critical_phase", default=None)
_instance: Engine | None = None
_instance_lock = threading.Lock()
_exiting = False
_F = TypeVar("_F", bound=Callable[..., Any])
_BROWSER_SLICE = 0.008


def caller_side(function: _F) -> _F:
    """Internal marker: this gateway's waiting portion belongs to its caller."""
    setattr(function, "__pysual_caller_side__", True)
    return function


@contextmanager
def critical_phase(name):
    token = _phase.set(name)
    try:
        yield
    finally:
        _phase.reset(token)


def ensure_presentable():
    if phase := _phase.get():
        raise LifecycleError(f"Cannot open or change a presentation during {phase}")


def capture_origin(owner: object) -> tuple[tuple[object, object, int], ...]:
    result = []
    while owner is not None:
        if hasattr(owner, "_presentation"):
            result.append(
                (
                    owner,
                    getattr(owner, "_presentation"),
                    getattr(owner, "_generation", 0),
                )
            )
        owner = getattr(owner, "_parent", None)
    return tuple(result)


def check_origin(obj: object) -> None:
    origins = _origin.get()
    if not origins:
        return
    # No accessed object can belong to a stale origin if all are unchanged.
    # A changed source still needs the full ancestry and first-opening checks.
    for source, presentation, _ in origins:
        if getattr(source, "_presentation", None) is not presentation:
            break
    else:
        return
    node = obj
    while node is not None:
        for source, presentation, generation in origins:
            if (
                node is source
                and getattr(source, "_presentation", None) is not presentation
            ):
                # Initial work may enter the first opening, but never a reopen.
                if (
                    presentation is None
                    and getattr(source, "_generation", 0) <= generation + 1
                ):
                    continue
                raise LifecycleError(
                    "An old UI invocation cannot access a reopened window"
                )
        node = getattr(node, "_parent", None)


@dataclass
class _Request:
    function: Callable
    future: Future
    context: Any
    asynchronous: bool = False


class Engine:
    def __init__(self, *, capacity: int = 10000, timeout: float = 10.0):
        self.capacity = capacity
        self.timeout = timeout
        self._inbox: deque[_Request] = deque()
        self._active_requests = 0
        self._lock = threading.Lock()
        self._scheduled = False
        self._stopping = False
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._ident: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shutdown_hooks: list[tuple[Callable, bool]] = []
        self._tasks: set[asyncio.Task] = set()
        self._startup_error: BaseException | None = None
        self._shutdown_future: Future | None = None
        self._browser = sys.platform == "emscripten"
        self.calls_submitted = 0
        self.calls_completed = 0
        if self._browser:
            self._browser_busy = False
            self._browser_checkpoint_at = monotonic() + _BROWSER_SLICE
            self._ident = threading.get_ident()
            self._ready.set()
        else:
            self._thread = threading.Thread(
                target=self._serve, name="Pysual UI", daemon=True
            )
            self._thread.start()
            self._ready.wait()
            if self._startup_error is not None:
                raise LifecycleError(
                    "The UI owner could not start"
                ) from self._startup_error

    @property
    def loop(self):
        if self._browser:
            return asyncio.get_running_loop()
        if self._loop is None:
            raise LifecycleError("UI owner has no event loop")
        return self._loop

    def is_ui_thread(self) -> bool:
        return self._ident == threading.get_ident()

    def _serve(self):
        try:
            self._ident = threading.get_ident()
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
        except BaseException as error:
            self._startup_error = error
            self._ready.set()
            return
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._reject_pending()
            self._loop.close()
            self._ident = None  # A later OS thread may reuse the numeric ID.

    def _admit(self, function, *, asynchronous=False) -> Future:
        future: Future = Future()
        request = _Request(function, future, copy_context(), asynchronous)
        with self._lock:
            if self._stopping:
                raise LifecycleError("The UI engine has shut down")
            if len(self._inbox) + self._active_requests >= self.capacity:
                raise DispatchOverload(
                    f"UI dispatch capacity is full (limit={self.capacity})"
                )
            self._inbox.append(request)
            self.calls_submitted += 1
            if not self._scheduled:
                self._scheduled = True
                self.loop.call_soon_threadsafe(self._drain)
        return future

    def _drain(self):
        deadline = monotonic() + 0.004
        count = 0
        while count < 64 and monotonic() < deadline:
            with self._lock:
                if not self._inbox:
                    self._scheduled = False
                    return
                request = self._inbox.popleft()
                self._active_requests += 1
            self._run_request(request)
            count += 1
        with self._lock:
            if self._inbox:
                self.loop.call_soon(self._drain)
            else:
                self._scheduled = False

    def drain_pending(self) -> None:
        """Serve the caller requests queued so far, on the owner, right now.

        The runtime calls this before deciding on a frame, so a burst of
        property writes from another thread lands in one scene update instead
        of each write waiting behind a frame. Only requests present at entry are
        served; later ones take the ordinary scheduled path, so a busy producer
        cannot postpone the frame indefinitely.
        """
        if not self.is_ui_thread():
            raise LifecycleError("drain_pending belongs to the UI owner")
        with self._lock:
            count = len(self._inbox)
        for _ in range(count):
            with self._lock:
                if not self._inbox:
                    return
                request = self._inbox.popleft()
                self._active_requests += 1
            self._run_request(request)

    def _run_request(self, request):
        future = request.future
        if request.asynchronous:
            if future.cancelled():
                self._release_request()
            else:
                try:
                    request.context.run(self._start_async, request)
                except BaseException as error:
                    try:
                        if not future.done():
                            future.set_exception(error)
                    finally:
                        self._release_request()
        else:
            if future.set_running_or_notify_cancel():
                try:
                    result = request.context.run(request.function)
                except BaseException as error:
                    self._release_request()
                    future.set_exception(error)
                else:
                    # Release capacity before waking a sequential caller,
                    # which may immediately submit its next operation.
                    self._release_request()
                    future.set_result(result)
            else:
                self._release_request()

    def _release_request(self):
        with self._lock:
            self._active_requests -= 1
            self.calls_completed += 1

    def _start_async(self, request):
        future = request.future

        async def execute():
            if future.cancelled():
                raise asyncio.CancelledError
            result = request.function()
            return await result if inspect.isawaitable(result) else result

        task = self.loop.create_task(execute())
        self._tasks.add(task)

        def complete(done):
            self._tasks.discard(done)
            self._release_request()
            if done.cancelled():
                future.cancel()
                return
            error = done.exception()
            try:
                if not future.done():
                    if error is not None:
                        future.set_exception(error)
                    else:
                        future.set_result(done.result())
            except InvalidStateError:
                if not future.cancelled():
                    raise

        def cancel(done):
            if done.cancelled() and not task.done():
                try:
                    self.loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass

        task.add_done_callback(complete)
        future.add_done_callback(cancel)
        if future.cancelled():
            task.cancel()

    def call(self, function, *args, **kwargs):
        return self._call(function, args, kwargs, timeout=self.timeout)

    def call_opening(self, function, *args, **kwargs):
        """Wait for an opening's actual result, however long its first frame takes."""
        return self._call(function, args, kwargs, timeout=None)

    def _call(self, function, args, kwargs, *, timeout):
        if self.is_ui_thread():
            if self._stopping:
                raise LifecycleError("The UI engine has shut down")
            if self._browser:
                return self._browser_call(function, *args, **kwargs)
            return function(*args, **kwargs)
        future = self._admit(lambda: function(*args, **kwargs))
        try:
            return future.result(timeout)
        except FutureTimeout:
            # TimeoutError may also be the actual result of the operation.
            if future.done():
                return future.result()
            cancelled = future.cancel()
            raise DispatchTimeout(started=not cancelled) from None

    def _browser_call(self, function, *args, **kwargs):
        """Give the shared browser loop time between complete UI operations."""
        # Runtime work and UI callbacks already cooperate at explicit awaits.
        if self._browser_busy or _phase.get() is not None or _origin.get():
            return function(*args, **kwargs)
        self._browser_busy = True
        try:
            if monotonic() >= self._browser_checkpoint_at:
                from pyodide.ffi import can_run_sync, run_sync  # type: ignore[import-not-found]

                if can_run_sync():
                    run_sync(asyncio.sleep(0))
                # Budget from resumption, excluding time spent rendering. Yield
                # before entry so reads and lifecycle checks see current state.
                self._browser_checkpoint_at = monotonic() + _BROWSER_SLICE
            return function(*args, **kwargs)
        finally:
            # Includes nested methods, property notifications and JSPI reentry.
            self._browser_busy = False

    def submit(self, coroutine):
        if self._browser:
            return self.loop.create_task(coroutine)
        try:
            future = self._admit(lambda: coroutine, asynchronous=True)
        except BaseException:
            coroutine.close()
            raise

        # A submitted coroutine rejected or cancelled before entry must be
        # closed on its owner, without racing the moment it starts executing.
        def close_unstarted(done):
            if not done.cancelled() and done.exception() is None:
                return

            def close():
                if inspect.getcoroutinestate(coroutine) == inspect.CORO_CREATED:
                    coroutine.close()

            if self.is_ui_thread():
                close()
            else:
                try:
                    self.loop.call_soon_threadsafe(close)
                except RuntimeError:
                    # A closed loop cannot begin executing the coroutine.
                    close()

        future.add_done_callback(close_unstarted)
        return future

    async def call_async(self, function, *args, **kwargs):
        if self.is_ui_thread():
            result = function(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        future = self._admit(lambda: function(*args, **kwargs), asynchronous=True)
        return await asyncio.wrap_future(future)

    def add_shutdown_hook(self, callback, *, finalization=False):
        def add():
            if (callback, finalization) not in self._shutdown_hooks:
                self._shutdown_hooks.append((callback, finalization))

        self.call(add)

    @staticmethod
    def _settle_rejected(pending):
        for request in pending:
            try:
                if not request.future.done():
                    request.future.set_exception(
                        LifecycleError("UI engine stopped before execution")
                    )
            except InvalidStateError:
                if not request.future.cancelled():
                    raise

    def _reject_pending(self):
        with self._lock:
            self._stopping = True
            pending, self._inbox = self._inbox, deque()
        self._settle_rejected(pending)

    async def _stop(self, *, finalizing=False):
        self._reject_pending()
        errors = []
        for callback, at_exit in reversed(self._shutdown_hooks):
            if finalizing and not at_exit:
                continue
            try:
                result = callback()
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, timeout=1.0)
            except BaseException as error:
                errors.append(error)
        if finalizing:
            return errors
        current = asyncio.current_task()
        tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=0.5)
            if pending:
                errors.append(
                    LifecycleError("UI tasks did not cooperate with shutdown")
                )
        return errors

    def shutdown(self, *, timeout=3.0, finalizing=False):
        if self.is_ui_thread():
            raise LifecycleError(
                "Call shutdown() outside the UI owner; UI handlers can use close_async()"
            )
        # Admission closes on the requesting thread, including when the UI is
        # still executing a slow callback. Concurrent shutdown callers share
        # one supervisor rather than cancelling each other's cleanup tasks.
        with self._lock:
            if self._shutdown_future is None:
                if self.loop.is_closed():
                    return
                self._stopping = True
                pending, self._inbox = self._inbox, deque()
                coroutine = self._stop(finalizing=finalizing)
                try:
                    self._shutdown_future = asyncio.run_coroutine_threadsafe(
                        coroutine, self.loop
                    )
                except BaseException:
                    coroutine.close()
                    raise
            else:
                pending = ()
            future = self._shutdown_future
        self._settle_rejected(pending)
        try:
            errors = future.result(timeout)
        finally:
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except RuntimeError:
                pass  # Another joined shutdown may already have closed it.
            if self._thread is not None:
                self._thread.join(timeout=0.5)
        if errors:
            raise BaseExceptionGroup("UI shutdown failed", errors)


def get_engine() -> Engine:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                if _exiting:
                    raise LifecycleError(
                        "Cannot start UI work during interpreter shutdown"
                    )
                from ._config import freeze

                freeze()
                _instance = Engine()
    return _instance


def is_ui_thread() -> bool:
    return sys.platform == "emscripten" or (
        _instance is not None and _instance.is_ui_thread()
    )


def check_ui_thread() -> None:
    if not is_ui_thread():
        raise LifecycleError("This internal operation belongs to the Pysual UI owner")


def call(function, *args, **kwargs):
    return get_engine().call(function, *args, **kwargs)


def call_opening(function, *args, **kwargs):
    return get_engine().call_opening(function, *args, **kwargs)


async def call_async(function, *args, **kwargs):
    return await get_engine().call_async(function, *args, **kwargs)


def submit(coroutine):
    return get_engine().submit(coroutine)


class TaskHandle:
    """A control-owned task whose await/cancellation follows asyncio.Task semantics."""

    def __init__(self, task):
        self._task = task
        self._future: Future = Future()

        def complete(done):
            if done.cancelled():
                self._future.cancel()
                return
            error = done.exception()
            try:
                if not self._future.done():
                    if error is None:
                        self._future.set_result(done.result())
                    else:
                        self._future.set_exception(error)
            except InvalidStateError:
                if not self._future.cancelled():
                    raise

        task.add_done_callback(complete)

    def cancel(self) -> bool:
        """Request cancellation; the final state follows the task's actual outcome."""
        if self._future.done():
            return False
        if is_ui_thread():
            return self._task.cancel()
        try:
            # Cancellation is lifecycle traffic and bypasses a saturated inbox.
            self._task.get_loop().call_soon_threadsafe(self._task.cancel)
        except RuntimeError:
            return False
        return True

    def done(self) -> bool:
        if is_ui_thread():
            return self._task.done()
        return self._future.done()

    def cancelled(self) -> bool:
        if is_ui_thread():
            return self._task.cancelled()
        return self._future.cancelled()

    def result(self, timeout=None):
        if is_ui_thread():
            if not self._task.done():
                raise LifecycleError("Await an unfinished task on the UI owner")
            return self._task.result()
        return self._future.result(timeout)

    def exception(self, timeout=None):
        if is_ui_thread():
            if not self._task.done():
                raise LifecycleError("Await an unfinished task on the UI owner")
            return self._task.exception()
        return self._future.exception(timeout)

    async def _wait(self):
        bridge = asyncio.wrap_future(self._future)
        bridge.add_done_callback(
            lambda done: None if done.cancelled() else done.exception()
        )
        while True:
            try:
                return await asyncio.shield(bridge)
            except asyncio.CancelledError:
                if bridge.done():
                    raise
                self.cancel()
                # Await the actual outcome, including cancellation cleanup or
                # a task that deliberately suppresses cancellation and returns.

    def __await__(self):
        return self._wait().__await__()


def shutdown() -> None:
    if _instance is not None:
        _instance.shutdown()


def _at_exit():
    global _exiting
    _exiting = True
    if _instance is not None and not _instance._browser:
        try:
            _instance.shutdown(finalizing=True)
        except BaseException:
            # Finalization never starts user close decisions or pins the process.
            pass


atexit.register(_at_exit)


def prioritize_finalization() -> None:
    """Release UI-owned resources before lazily imported bindings' exit hooks."""
    atexit.unregister(_at_exit)
    atexit.register(_at_exit)


def ui_operation(function: _F) -> _F:
    """Add browser cooperation to a synchronous boundary; native calls are free."""
    if sys.platform != "emscripten":
        return function

    @wraps(function)
    def operation(*args, **kwargs):
        # Reading class-level property defaults must not start/freeze the engine.
        if _instance is None:
            return function(*args, **kwargs)
        return _instance._browser_call(function, *args, **kwargs)

    return cast(_F, operation)


def ui_method(function: _F) -> _F:
    """Wrap a public operation once; owner-to-owner calls allocate no Future."""
    if getattr(function, "__pysual_caller_side__", False) or getattr(
        function, "__pysual_ui_method__", False
    ):
        return function
    wrapped: Callable[..., Any]
    if inspect.iscoroutinefunction(function):

        @wraps(function)
        async def asynchronous(*args, **kwargs):
            if is_ui_thread():
                if args:
                    check_origin(args[0])
                return await function(*args, **kwargs)
            return await call_async(asynchronous, *args, **kwargs)

        wrapped = asynchronous
    else:

        @wraps(function)
        def synchronous(*args, **kwargs):
            if is_ui_thread():
                if args:
                    check_origin(args[0])
                return function(*args, **kwargs)
            return call(synchronous, *args, **kwargs)

        wrapped = ui_operation(synchronous)
    setattr(wrapped, "__pysual_ui_method__", True)
    return cast(_F, wrapped)
