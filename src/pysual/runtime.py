"""One host UI loop, lifecycle owner, and adapter selection at the composition root."""

from __future__ import annotations

import asyncio
import sys
import warnings
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from math import inf
from time import perf_counter
from typing import TYPE_CHECKING

from ._engine import call, call_async, get_engine, prioritize_finalization
from ._namespace_handlers import NamespaceHandlers, bind_handlers
from .backends import BackendName, resolve_backend
from .errors import BindingWarning, LifecycleError
from .events import ChangeEvent, ClosingEvent, Dispatcher, UiEvent
from .hints import Hints
from .host import CapabilityError, Host, Input, RenderDiagnostics, Viewport
from .input import Router
from .layout import _clear_measurements, _measure_environment, arrange
from .motion import Motion
from .painting import RetainedPaintTree, paint_tree
from .render_cache import RenderCache
from .presentation import Presentation, ensure_wait_allowed
from .schema import Dirty, _HIT_MASK, check_ui_thread

if TYPE_CHECKING:
    from .app import App


@dataclass(frozen=True)
class FrameTiming:
    """One Python scene submission; seconds on the monotonic performance clock.

    Native cached presentations are counted separately by host.native_stats().
    input_seconds accumulates input routing since the preceding submission;
    update_seconds covers the submission itself and excludes idle time.
    """

    number: int
    completed_at: float
    interval_seconds: float
    layout_seconds: float
    paint_seconds: float
    update_seconds: float = 0.0
    input_seconds: float = 0.0


class _RuntimeDispatcher(Dispatcher):
    """Wake event-driven windows when an async callback fails while idle."""

    def __init__(self, limit, wake):
        super().__init__(limit)
        self._wake_runtime = wake

    def _record_error(self, error):
        super()._record_error(error)
        self._wake_runtime()


class Runtime:
    def __init__(
        self, app: App, host: Host, *, modal=False, owner=None, presentation_owner=None
    ):
        from ._modality import ModalManager

        self.app = app
        self.host = host
        self.render_cache = RenderCache(host)
        self._retained_paint = None
        self.dispatcher = _RuntimeDispatcher(
            app.max_pending_handlers, lambda: self._wake_event.set()
        )
        self.motion = Motion(self)
        self.router = Router(self)
        self.hints = Hints(self)
        self._modality = ModalManager(self)
        self.popup = None
        self._owner_runtime: Runtime | None = owner
        self._presentation_owner = presentation_owner or (owner.app if owner else None)
        self._native_parented = False
        self._backend_name: BackendName | None = None
        self._owned: set[Runtime] = set()
        self._native_blockers: set[Runtime] = set()
        self._owner_previous_focus = None
        self._modal = modal
        self.presentation: Presentation
        self._phase: str | None = None
        self._started = False
        self._cleaned = False
        self._task: asyncio.Task | None = None
        self._close_task = None
        self._close_waiter = None
        self._close_result = None
        self._close_reason = "closed"
        self._failure: BaseException | None = None
        self._stop = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._dirty = True
        self._force_layout = True
        self._measure_environment = None
        self._measure_host = None
        self._host_open = False
        self.frames = 0
        # Resource failures are diagnostics, not fatal application exceptions.
        self.resource_errors: deque[str] = deque(maxlen=32)
        self.frame_timings: deque[FrameTiming] = deque(maxlen=4096)
        self._last_presented: float | None = None
        self._last_frame_started = 0.0
        self._input_seconds = 0.0
        self.viewport = Viewport(app.width or 960, app.height or 640, app.ui_scale or 1)
        self.suspended = False
        self._pending_size: dict[str, float | None] = {}

    @property
    def modal(self):
        return self._modality.active

    def diagnostics(self, *, include_native=False) -> RenderDiagnostics:
        check_ui_thread()
        host = self.host
        backend = getattr(host, "backend", None) or self._backend_name
        native = None
        if include_native and self.frames and self._host_open and getattr(host, "native_retained", False):
            native = host.native_stats()
        return RenderDiagnostics(
            scene_submissions=self.frames,
            native_presentations=int(native["frames"]) if native is not None and "frames" in native else None,
            host_name=getattr(host, "diagnostic_name", type(host).__name__),
            backend=backend,
            web_execution=getattr(host, "web_execution", None),
            terminal_renderer=getattr(host, "renderer", None) if backend == "terminal" else None,
            hidden=getattr(host, "hidden", None),
        )

    def _sync_viewport(self, viewport: Viewport | None = None, *, acknowledged=None) -> None:
        previous = self.viewport
        host_viewport = (
            viewport
            if viewport is not None
            else getattr(
                self.host,
                "viewport",
                Viewport(*self.host.size, scale=getattr(self.host, "render_scale", 1)),
            )
        )
        if not isinstance(host_viewport, Viewport):
            raise TypeError("Host viewport must be a Viewport record")
        self.viewport = host_viewport
        for axis, actual in zip(("width", "height"), self.host.size):
            if acknowledged is not None and axis in acknowledged:
                if self._pending_size.get(axis) == acknowledged[axis]:
                    self._pending_size.pop(axis, None)
            if axis not in self._pending_size:
                self.app._values[axis] = actual
        if previous != self.viewport:
            if previous.scale != self.viewport.scale:
                self.render_cache.clear()
                if self._retained_paint is not None:
                    self._retained_paint.invalidate_all()
                # DPI can change rounded font metrics without changing logical
                # rectangles. Invalidate the existing tree's layout caches too.
                pending = [self.app]
                while pending:
                    control = pending.pop()
                    control._dirty |= Dirty.MEASURE | Dirty.ARRANGE | Dirty.HIT_TEST
                    pending.extend(getattr(control, "_children", ()))
            self._force_layout = True
            self.app.invalidate(Dirty.MEASURE | Dirty.ARRANGE | Dirty.HIT_TEST)
            if self.app.is_open:
                self.app.viewport_changed.emit(
                    ChangeEvent(
                        source=self.app,
                        origin="system",
                        old_value=previous,
                        new_value=self.viewport,
                        property_name="viewport",
                    )
                )

    def _suspend(self, suspended: bool) -> None:
        if self.suspended == suspended:
            return
        self.suspended = suspended
        if suspended:
            self.router.process(Input("blur"))
            self.motion.clear()
            self.hints.clear()
            self.render_cache.clear()
            self.app.suspended.emit(UiEvent(source=self.app, origin="system"))
        else:
            self._last_presented = None
            self._last_frame_started = 0
            self._force_layout = True
            self._sync_viewport()
            self.invalidate()
            self.app.resumed.emit(UiEvent(source=self.app, origin="system"))

    def invalidate(self):
        self._dirty = True
        self._wake_event.set()

    def _next_frame_at(self):
        if self.suspended:
            return inf
        interval = self.app.redraw_interval
        if getattr(self.host, "native_retained", False):
            # Re-presenting unchanged scenes and style animation are C work.
            # Python painters run only for real invalidations/explicit requests.
            interval = None
        requested = (
            0.0
            if self._dirty
            else self._last_frame_started + interval
            if interval is not None
            else inf
        )
        requested = min(requested, self.motion.next_frame_at)
        limit = self.app.fps_limit
        allowed = self._last_frame_started + 1 / limit if limit else 0.0
        return max(requested, allowed)

    def _sync_render_policy(self):
        configure = getattr(self.host, "configure_rendering", None)
        if configure is not None:
            configure(
                redraw_interval=self.app.redraw_interval,
                fps_limit=self.app.fps_limit,
                reduce_motion=self.app.reduce_motion,
            )

    def _reveal_focus(self, control):
        """Reveal focus through ancestor layout hooks after current arrangement."""
        bounds = control._rect
        child, parent = control, control._parent
        moved = False
        while parent is not None:
            area = parent.content_bounds(parent._rect)
            if not child._overlay:
                revealed = parent._reveal_bounds(bounds)
                moved = moved or revealed != bounds
                bounds = revealed
            # Only expose the portion visible through the inner container,
            # including when a focus target is larger than its viewport.
            bounds = bounds.intersect(area)
            if bounds.width <= 0 or bounds.height <= 0:
                break
            child, parent = parent, parent._parent
        if moved:
            arrange(self.app, self.host)
        if getattr(control, "_text_input", False):
            self.host.text_input(control._rect)
        return moved

    def _paint_frame(self):
        from ._engine import critical_phase

        previous = self._phase
        self._phase = "paint"
        try:
            with critical_phase("layout and paint"):
                return self._paint_content()
        finally:
            self._phase = previous

    def _paint_content(self):
        app = self.app
        environment = _measure_environment(self.host)
        if self._measure_host is not self.host or environment != self._measure_environment:
            self._measure_host = self.host
            self._measure_environment = environment
            self._force_layout = True
        # Consume this request before painting so a request made during the
        # pass (including by the host) survives for the next eligible frame.
        self._dirty = False
        self._last_frame_started = perf_counter()
        self.motion.tick(self._last_frame_started)
        layout_seconds = 0.0
        layout_changed = False
        # Consume only propagated hit-policy paths before arrangement can clear
        # their ancestors. Focus/capture reconcile below uses the current policy,
        # independently of whether this frame also changes sibling geometry.
        pending = [app]
        while pending:
            control = pending.pop()
            if int(control._dirty) & _HIT_MASK:
                control._dirty = int(control._dirty) & ~_HIT_MASK
                pending.extend(getattr(control, "_children", ()))
        if self._force_layout or app._dirty & (Dirty.MEASURE | Dirty.ARRANGE):
            arrange(app, self.host)
            layout_changed = True
            if self.popup is not None:
                self.popup.reconcile()
            if self.popup is not None:
                self.popup.reposition()
                arrange(app, self.host)
            self._force_layout = False
            if app.profile_frames:
                layout_seconds = perf_counter() - self._last_frame_started
        self.router.reconcile()
        focus_to_reveal = self.router.take_focus_reveal()
        if focus_to_reveal is not None:
            reveal_started = perf_counter() if app.profile_frames else 0.0
            if self._reveal_focus(focus_to_reveal):
                layout_changed = True
                if app.profile_frames:
                    layout_seconds += perf_counter() - reveal_started
        self.host.set_title(app.title)
        paint_started = perf_counter() if app.profile_frames else 0.0
        self.render_cache.configure(app.render_cache_bytes)
        paint_tree(app, self.host, self.router.focus, self.render_cache,
                   retained=self._retained_paint, layout_changed=layout_changed)
        completed = perf_counter()
        self.frames += 1
        if app.profile_frames:
            self.frame_timings.append(
                FrameTiming(
                    self.frames,
                    completed,
                    completed - self._last_presented
                    if self._last_presented is not None
                    else 0.0,
                    layout_seconds,
                    completed - paint_started,
                    completed - self._last_frame_started,
                    self._input_seconds,
                )
            )
        self._input_seconds = 0.0
        self._last_presented = completed

    def request_close(self, result=None):
        """Share a vetoable decision independently of the requesting control."""
        from ._engine import critical_phase

        if self._close_task is None or (
            self._close_task.done() and self.app._state == "RUNNING"
        ):
            self._close_result = result
            self._close_waiter = asyncio.get_running_loop().create_future()
            with critical_phase(None):
                self._close_task = asyncio.create_task(
                    self._decide_close(),
                    name=f"{type(self.app).__name__}: close decision",
                )
            self._close_task.add_done_callback(self._close_finished)
        assert self._close_waiter is not None
        waiter = asyncio.shield(self._close_waiter)
        waiter.add_done_callback(
            lambda future: None if future.cancelled() else future.exception()
        )
        return waiter

    def _close_finished(self, task):
        if task.cancelled():
            if self._close_waiter is not None and not self._close_waiter.done():
                self._close_waiter.set_result(self._stop.is_set())
        elif (error := task.exception()) is not None:
            if self._close_waiter is not None and not self._close_waiter.done():
                self._close_waiter.set_exception(error)
            self._failure = error
            self._stop.set()
            self._wake_event.set()
        elif self._close_waiter is not None and not self._close_waiter.done():
            self._close_waiter.set_result(task.result())

    async def _decide_close(self):
        if self._stop.is_set():
            return True
        accepted = await self.dispatcher.decide(
            self.app.closing, ClosingEvent(source=self.app, origin="system")
        )
        if accepted:
            self.app._state = "CLOSING"
            self._stop.set()
            self._wake_event.set()
        return accepted

    def force_close(self, *, reason="owner_closed"):
        """End this opening without running veto handlers (owner/shutdown/disposal)."""
        self._close_reason = reason
        self.app._state = "CLOSING"
        self._stop.set()
        self._wake_event.set()
        if self._close_waiter is not None and not self._close_waiter.done():
            self._close_waiter.set_result(True)

    async def open_modal(self, control):
        presentation = self._modality.show(control, modal=True)
        return (await presentation.wait_async()).result

    def finish_modal(self, control, result):
        self._modality.finish(control, result)

    def start(self, *, handlers: NamespaceHandlers | None = None):
        """Build/open/paint atomically on UI; present is an acknowledged host call."""
        from ._engine import _origin, capture_origin, critical_phase, ensure_presentable
        from .controls import Container

        check_ui_thread()
        ensure_presentable()
        app = self.app
        if self._started:
            return self.presentation
        if app._disposed or app._destroy_requested or app._state == "FAILED":
            raise LifecycleError("A destroyed or failed window cannot be opened")
        if app._runtime is not None or app._state not in ("CREATED", "CLOSED"):
            raise LifecycleError("This window already has an opening")
        app._generation += 1
        self.presentation = Presentation(
            app._generation,
            modal=self._modal,
            owner=self._presentation_owner,
        )
        app._presentation = self.presentation
        app._runtime = self
        app._state = "STARTING"
        app._wake = self.invalidate
        self._phase = "startup"
        origin_token = _origin.set(
            tuple(entry for entry in _origin.get() if entry[0] is not app)
            + capture_origin(app)
        )
        try:
            _register(self)
            with critical_phase("window startup"):
                if not app._built:
                    try:
                        app.build()
                    except BaseException:
                        app._build_failed = True
                        raise
                    app._built = True
                self.dispatcher.limit = app.max_pending_handlers
                app._bind_handlers(app, type(app).__name__)
                bind_handlers(app, handlers)
                for diagnostic in app.check_handlers():
                    warnings.warn(diagnostic, BindingWarning, stacklevel=2)
                # open() can allocate resources before failing.
                namespace = getattr(self.host, "set_session_namespace", None)
                if callable(namespace):
                    namespace(f"{type(app).__module__}.{type(app).__qualname__}")
                self._host_open = True
                wake = getattr(self.host, "set_wake_callback", None)
                if wake is not None:
                    wake(self._wake_event.set)
                self._sync_render_policy()
                self.host.open(
                    app.title,
                    app.width or 960,
                    app.height or 640,
                    app.resizable,
                    app.ui_scale,
                )
                # A preserved tree may reopen on the same host with fresh
                # platform metrics, or have been measured before opening.
                _clear_measurements(app)
                if "scene_patches" in getattr(self.host, "capabilities", ()):
                    self._retained_paint = RetainedPaintTree(self.host)
                if self._owner_runtime is not None:
                    owner = self._owner_runtime
                    self._set_modal_owner(owner.host)
                    self._native_parented = True
                    owner._owned.add(self)
                    owner._native_blockers.add(self)
                    self._owner_previous_focus = owner.router.focus
                    owner.router.cancel_capture(clear_focus=True)
                    owner.router._set_hover(None)
                    if owner.popup is not None:
                        owner.popup.dismiss(restore_focus=False)
                    owner.invalidate()
                app._values["width"], app._values["height"] = self.host.size
                self._sync_viewport()
                app._attach_dispatcher(self.dispatcher)
                self._paint_frame()
                app._state = "RUNNING"
                self._started = True
            # Handler tasks must not inherit the startup critical-phase context.
            app.loaded.emit(UiEvent(source=app, origin="system"))
        except BaseException as error:
            errors = [error]
            for cleanup in (
                self._unlink_owner,
                self.render_cache.clear,
                self._close_host,
            ):
                try:
                    cleanup()
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            app._runtime = None
            app._wake = None
            app._attach_dispatcher(None)
            app._state = "FAILED" if len(errors) > 1 or app._build_failed else "CLOSED"
            if app._build_failed:
                try:
                    Container.destroy(app)
                except BaseException as cleanup_error:
                    errors.append(cleanup_error)
            failure = (
                errors[0]
                if len(errors) == 1
                else BaseExceptionGroup("Window startup and cleanup failed", errors)
            )
            self._cleaned = True
            self.presentation.fail(failure)
            _unregister(self, failure)
            self._emit_closed()
            raise failure
        finally:
            self._phase = None
            _origin.reset(origin_token)
        return self.presentation

    async def main(self):
        """Run one opening on the shared owner; injected hosts remain testable."""
        return await call_async(self._main)

    async def _main(self):
        from ._engine import _origin

        if not self._started:
            self.start()
        token = _origin.set(
            tuple(entry for entry in _origin.get() if entry[0] is not self.app)
            + ((self.app, self.presentation, self.presentation.generation),)
        )
        try:
            if self._task is None:
                self._task = asyncio.current_task()
            error = None
            try:
                await self._pump()
                if self._failure is not None:
                    raise self._failure
            except BaseException as caught:
                error = caught
            await self._cleanup(error)
            if error is not None:
                raise error
        finally:
            _origin.reset(token)

    async def _pump(self):
        from ._engine import critical_phase

        engine = get_engine()
        while not self._stop.is_set():
            self._wake_event.clear()
            self.dispatcher.raise_errors()
            for event in self.host.poll():
                self.hints.observe(event)
                if event.kind == "error":
                    raise RuntimeError(event.text)
                elif event.kind == "resource_error":
                    self.resource_errors.append(event.text)
                elif event.kind == "repaint":
                    self.invalidate()
                elif event.kind == "close":
                    if not self._native_blockers:
                        self.request_close()
                elif event.kind in ("resize", "viewport"):
                    self._sync_viewport(event.viewport)
                    self.invalidate()
                elif event.kind in ("suspend", "resume"):
                    self._suspend(event.kind == "suspend")
                elif not self.suspended and not self._native_blockers:
                    self._process_input(event)
            # Serve worker-thread requests queued so far before deciding on a
            # frame: a burst of property writes from another thread then lands
            # in one scene update instead of each write waiting behind a frame.
            # Only requests present now are served, so a frame is never
            # postponed indefinitely by a busy producer.
            engine.drain_pending()
            if perf_counter() >= self._next_frame_at():
                # Give ready handler continuations one turn before painting.
                # Startup presents its first frame synchronously in start().
                await asyncio.sleep(0)
            if self._stop.is_set():
                break
            self._submit_size()
            self._sync_render_policy()
            if not self.suspended:
                self.hints.update()
            rendered = perf_counter() >= self._next_frame_at()
            if rendered:
                with critical_phase("layout and paint"):
                    self._paint_frame()
            if getattr(self.host, "event_driven", False):
                deadline = min(
                    self._next_frame_at(),
                    self.hints.next_deadline if not self.suspended else inf,
                )
                delay = max(0.0, deadline - perf_counter())
            else:
                delay = (
                    0.0 if rendered
                    else min(1 / 120, max(0.0, self._next_frame_at() - perf_counter()))
                )
            await self._wait(delay)

    def _process_input(self, event):
        from ._engine import critical_phase

        started = perf_counter() if self.app.profile_frames else None
        self._phase = "input"
        try:
            with critical_phase("native input traversal"):
                self.router.process(event)
        finally:
            self._phase = None
            if started is not None:
                self._input_seconds += perf_counter() - started

    def _submit_size(self):
        # build() can request a size before the host exists. Platforms that own
        # the viewport, such as the browser, never accept set_size; the startup
        # request must be dropped or the next real viewport crashes the window.
        if "host_resize" not in getattr(self.host, "capabilities", ()):
            if self._pending_size:
                self._pending_size.clear()
                self._sync_viewport()
            return
        width, height = self.app.width, self.app.height
        requested = (
            width if width is not None else self.host.size[0],
            height if height is not None else self.host.size[1],
        )
        pending = self._pending_size.copy()
        if requested != self.host.size:
            self.host.set_size(*requested)
            # set_size is synchronous: the returned host size acknowledges
            # exactly this snapshot, including native rounding/clamping.
            self._sync_viewport(acknowledged=pending)
            self._force_layout = True
            self.invalidate()
        elif pending:
            self._sync_viewport(acknowledged=pending)

    async def _wait(self, delay):
        """Yield fairly, waking idle scheduling immediately after a mutation."""
        if delay == 0:
            await asyncio.sleep(0)
        elif delay == inf:
            # Native input, application writes and handler completion wake this
            # wait. There is no recurring Python polling timer.
            await self._wake_event.wait()
        else:
            # Use a relative timer directly. Pyodide's call_at rejects a
            # deadline that expires between its two clock reads, which can
            # happen when a frame leaves only a tiny positive wait budget.
            timer = asyncio.get_running_loop().call_later(delay, self._wake_event.set)
            try:
                await self._wake_event.wait()
            finally:
                timer.cancel()

    def _close_host(self):
        try:
            if self._host_open:
                self._host_open = False
                self.host.close()
        finally:
            _clear_measurements(self.app)
            self._measure_environment = None
            self._measure_host = None
            if self._retained_paint is not None:
                self._retained_paint.clear()
            self.router.clear()

    def _set_modal_owner(self, owner_host):
        set_owner = getattr(self.host, "set_modal_owner", None)
        if set_owner is None:
            raise CapabilityError("This host does not support owner-scoped native modality")
        set_owner(owner_host)

    def _unlink_owner(self):
        owner = self._owner_runtime
        if owner is None:
            return
        self._owner_runtime = None
        owner._owned.discard(self)
        owner._native_blockers.discard(self)
        try:
            if self._host_open and self._native_parented:
                self._native_parented = False
                self._set_modal_owner(None)
        finally:
            previous = self._owner_previous_focus
            self._owner_previous_focus = None
            if owner.app.is_open and not owner._native_blockers:
                owner.router.set_focus(
                    previous
                    if previous is not None
                    and not previous._disposed
                    and owner._modality.allowed(previous)
                    else None
                )
                owner.invalidate()

    async def _cleanup(self, failure=None):
        from .controls import Container

        if self._cleaned:
            return
        self._cleaned = True
        app = self.app
        app._state = "CLOSING"
        errors = [failure] if failure is not None else []

        def attempt(callback):
            try:
                callback()
            except BaseException as error:
                errors.append(error)

        # Descendants release their scopes first. A forced owner close cannot
        # hang forever behind child vetoes or a confirmation dialog.
        for child in tuple(self._owned):
            child.force_close(reason="owner_closed")
            try:
                await child.presentation.wait_async()
            except BaseException as error:
                errors.append(error)
        attempt(lambda: self._modality.shutdown(failure))
        attempt(lambda: self.router.cancel_capture(clear_focus=True))
        attempt(self.motion.clear)
        attempt(lambda: self.router.set_focus(None))
        attempt(self.hints.clear)
        popup = self.popup
        if popup is not None:
            attempt(lambda: popup.dismiss(restore_focus=False))
        if self._close_task is not None and not self._close_task.done():
            self._close_task.cancel()
            try:
                _, pending = await asyncio.wait(
                    (self._close_task,), timeout=app.shutdown_timeout
                )
                if pending:
                    errors.append(
                        LifecycleError("Closing handlers did not cooperate with shutdown")
                    )
            except BaseException as error:
                # Engine shutdown may cancel this wait before the window's
                # deadline. Still close the host and settle its presentation.
                errors.append(error)
        shutdown_task = asyncio.create_task(self.dispatcher.shutdown())
        try:
            _, pending = await asyncio.wait(
                (shutdown_task,), timeout=app.shutdown_timeout
            )
            if pending:
                shutdown_task.cancel()
                shutdown_task.add_done_callback(
                    lambda task: None if task.cancelled() else task.exception()
                )
                errors.append(
                    LifecycleError("UI tasks did not cooperate with shutdown")
                )
            else:
                shutdown_task.result()
        except BaseException as error:
            errors.append(error)
        attempt(self.render_cache.clear)
        attempt(self._unlink_owner)
        attempt(self._close_host)
        # Detach without cancelling preserved close-request continuations.
        attempt(lambda: app._attach_dispatcher(None))
        app._runtime = None
        app._wake = None
        app._file_busy = False
        app._state = "FAILED" if errors else "CLOSED"
        if app._destroy_requested:
            attempt(lambda: Container.destroy(app))
            app._state = "DESTROYED" if not errors else "FAILED"
        completion_error = (
            None
            if not errors
            else errors[0]
            if len(errors) == 1
            else BaseExceptionGroup("Window runtime and cleanup failed", errors)
        )
        if completion_error is None:
            self.presentation.complete(self._close_result, reason=self._close_reason)
        else:
            self.presentation.fail(completion_error)
        _unregister(self, completion_error)
        self._emit_closed()
        if completion_error is not None:
            raise completion_error

    def _emit_closed(self):
        if self.app._disposed:
            return
        # A failed first frame also ends an opening. Completion precedes user
        # notification, and terminal listeners cannot delay native cleanup.
        self.dispatcher._stopped = True
        try:
            self.dispatcher.emit(
                self.app.closed, UiEvent(source=self.app, origin="system"), terminal=True
            )
        except BaseException as error:
            asyncio.get_running_loop().call_exception_handler({
                "message": "Pysual closed notification failed", "exception": error,
            })


_active: set[Runtime] = set()
_idle: Future[None] = Future()
_idle.set_result(None)
_idle_error: BaseException | None = None
_hook_registered = False


def _register(runtime):
    global _idle, _idle_error, _hook_registered
    if not _active:
        _idle = Future()
        _idle_error = None
    _active.add(runtime)
    if not _hook_registered:
        from .backends import shutdown_managed_hosts

        get_engine().add_shutdown_hook(shutdown_managed_hosts, finalization=True)
        get_engine().add_shutdown_hook(_shutdown_windows)
        _hook_registered = True
    # Host construction may initialize platform resources, which
    # registers its own process-main-thread quit callback. Close on the video
    # owner first, including when a different backend was opened previously.
    prioritize_finalization()


def _unregister(runtime, error=None):
    global _idle_error
    _active.discard(runtime)
    if error is not None and _idle_error is None:
        _idle_error = error
    if not _active and not _idle.done():
        if _idle_error is not None:
            _idle.set_exception(_idle_error)
        else:
            _idle.set_result(None)


async def _shutdown_windows():
    runtimes = tuple(_active)
    for runtime in runtimes:
        runtime.force_close(reason="shutdown")
    if runtimes:
        await asyncio.gather(
            *(runtime.presentation.wait_async() for runtime in runtimes),
            return_exceptions=True,
        )


def open_window(
    app: App, backend: Host | BackendName | None = None, *, modal=False,
    owner: App | None = None, handlers: NamespaceHandlers | None = None,
) -> Presentation:
    """Owner-side admission; return the exact opening acknowledged by present."""
    from ._config import resolved_backend
    from ._engine import ensure_presentable
    from .backends import create_managed_host

    check_ui_thread()
    ensure_presentable()
    app._check_live()
    if app._destroy_requested:
        raise LifecycleError("A destroyed window cannot be reopened")
    if app._runtime is not None:
        presentation = app._runtime.presentation
        if app._state != "RUNNING":
            raise LifecycleError("Wait for the current opening to finish closing")
        if presentation.modal != modal or presentation.owner is not owner:
            raise LifecycleError(
                "The existing opening has a different modality or owner"
            )
        bind_handlers(app, handlers)
        return presentation
    owner_runtime = None
    if modal:
        if owner is None or not getattr(owner, "_is_app", False) or owner is app:
            raise LifecycleError(
                "A native modal requires a different live owner window"
            )
        owner._check_live()
        owner_runtime = owner._runtime
        if (
            not isinstance(owner_runtime, Runtime)
            or owner_runtime.app is not owner
            or not owner.is_open
            or not owner.visible
            or not owner.enabled
        ):
            raise LifecycleError("The modal owner must be open and visible")
        if owner_runtime._native_blockers:
            if asyncio.current_task() is not owner_runtime._close_task:
                raise LifecycleError("Use the currently eligible modal window as owner")
            # A close decision may need confirmation above its already-open
            # dialog. Parent it to the eligible leaf so the older native dialog
            # is blocked too; retain the requested logical owner on the record.
            while owner_runtime._native_blockers:
                if len(owner_runtime._native_blockers) != 1:
                    raise LifecycleError(
                        "The native modal ownership chain is ambiguous"
                    )
                owner_runtime = next(iter(owner_runtime._native_blockers))
        if "native_modal" not in owner.capabilities:
            raise CapabilityError(
                "This host does not support owner-scoped native modality; "
                "use SubWindow.show_modal for an in-app dialog"
            )
    elif owner is not None:
        raise LifecycleError("A modeless window does not take a modal owner")
    selected = resolve_backend(resolved_backend(backend))
    if isinstance(selected, str) and any(
        runtime._backend_name is not None and runtime._backend_name != selected
        for runtime in _active
    ):
        raise LifecycleError("All active native windows must use the same backend")
    host = create_managed_host(selected) if isinstance(selected, str) else selected
    if modal and (
        "native_modal" not in host.capabilities or not hasattr(host, "set_modal_owner")
    ):
        raise CapabilityError(
            "This host does not support owner-scoped native modality; "
            "use SubWindow.show_modal for an in-app dialog"
        )
    validate_owner = getattr(host, "validate_modal_owner", None)
    if owner_runtime is not None and validate_owner is not None:
        validate_owner(owner_runtime.host)
    runtime = Runtime(
        app, host, modal=modal, owner=owner_runtime, presentation_owner=owner
    )
    runtime._backend_name = selected if isinstance(selected, str) else None
    presentation = runtime.start(handlers=handlers)
    runtime._task = asyncio.create_task(
        runtime._main(), name=f"{type(app).__name__}: window"
    )
    runtime._task.add_done_callback(_report_runtime_failure)
    return presentation


def _report_runtime_failure(task):
    if not task.cancelled() and (error := task.exception()) is not None:
        asyncio.get_running_loop().call_exception_handler(
            {
                "message": "Pysual window failed",
                "exception": error,
                "task": task,
            }
        )


def run(app: App, backend: Host | BackendName | None = None):
    """Open a window and return immediately after acknowledged first presentation."""
    from ._engine import call_opening

    call_opening(open_window, app, backend=backend)
    return app


def wait(timeout: float | None = None) -> None:
    """Wait until no windows are open, including openings added while waiting.

    Return None at the first idle point; return immediately when already idle.
    A completed failure is reported to current waiters, then cleared for later
    idle waits. Use app.wait() instead for one opening's persistent Outcome.
    """
    ensure_wait_allowed()
    future = call(lambda: _idle)
    if sys.platform == "emscripten":
        from pyodide.ffi import run_sync  # type: ignore[import-not-found]

        waiter = _wait_for_idle(future)
        if timeout is not None:
            waiter = asyncio.wait_for(waiter, timeout)
        return run_sync(waiter)
    _idle_result(future, timeout)


def _consume_idle_error(future):
    global _idle, _idle_error
    # Existing waiters retain their snapshot. A delayed waiter from an earlier
    # opening must not replace a new active period's completion future.
    if _idle is future and not _active:
        _idle = Future()
        _idle.set_result(None)
        _idle_error = None


def _idle_result(future, timeout=None):
    try:
        return future.result(timeout)
    except BaseException as error:
        # A timeout racing completion has not observed the window's failure.
        if future.done() and not future.cancelled() and future.exception() is error:
            call(_consume_idle_error, future)
        raise


async def wait_async() -> None:
    """Async counterpart of module wait(), distinct from app.wait_async()."""
    future = await call_async(lambda: _idle)
    await _wait_for_idle(future)


async def _wait_for_idle(future):
    bridge = asyncio.wrap_future(future)
    bridge.add_done_callback(
        lambda done: None if done.cancelled() else done.exception()
    )
    await asyncio.wait((bridge,))
    _idle_result(future)
