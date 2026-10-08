"""Reusable independent windows and their caller-side presentation gateways."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, cast

from ._engine import call, call_async, call_opening, caller_side, check_origin
from ._namespace_handlers import capture_handlers
from .backends import BackendName
from .controls import Container, _ControlMeta
from .errors import LifecycleError
from .events import ChangeEvent, ClosingEvent, Event, UiEvent
from .geometry import Rect
from .host import (
    MAX_TEXT_BYTES,
    CapabilityError,
    Host,
    RenderDiagnostics,
    TextFile,
    Viewport,
    validate_session_key,
    validate_session_text,
)
from .inspection import ControlSnapshot, inspect_tree
from .render_cache import CacheStats
from .presentation import Outcome, Presentation, ensure_wait_allowed
from .schema import Dirty, check_ui_thread, prop
from .theme import Theme, dark

if TYPE_CHECKING:
    from .runtime import FrameTiming, Runtime


class Window(Container, metaclass=_ControlMeta):
    title: str = prop(default="Pysual")
    width: float | None = prop(default=960.0, minimum=1, affects=Dirty.MEASURE)
    height: float | None = prop(default=640.0, minimum=1, affects=Dirty.MEASURE)
    theme: Theme = prop(default=dark(), affects=Dirty.MEASURE | Dirty.PAINT)
    resizable: bool = prop(default=True)
    ui_scale: float | None = prop(
        default=None,
        minimum=0.25,
        affects=Dirty.MEASURE,
        doc="Explicit UI scale, or None to follow the host's automatic display DPI",
    )
    max_pending_handlers: int | None = prop(default=1000, minimum=1)
    shutdown_timeout: float = prop(default=2.0, minimum=0.01)
    fps_limit: int | None = prop(default=120, minimum=1, affects=Dirty.NONE)
    redraw_interval: float | None = prop(
        default=None,
        minimum=0,
        affects=Dirty.NONE,
        doc="Seconds since the last frame start before requesting another; None is on demand, zero is continuous. Requests obey fps_limit.",
    )
    render_cache_bytes: int = prop(
        default=32 * 1024 * 1024, minimum=0, affects=Dirty.NONE, persist=False
    )
    profile_frames: bool = prop(default=False, affects=Dirty.NONE, persist=False)
    reduce_motion: bool = prop(
        default=False,
        doc="Show interaction states immediately, without hover, press or toggle transitions",
    )
    loaded: ClassVar[Event[UiEvent]] = Event(UiEvent)
    closing: ClassVar[Event[ClosingEvent]] = Event(ClosingEvent)
    closed: ClassVar[Event[UiEvent]] = Event(UiEvent)
    suspended: ClassVar[Event[UiEvent]] = Event(UiEvent)
    resumed: ClassVar[Event[UiEvent]] = Event(UiEvent)
    viewport_changed: ClassVar[Event[ChangeEvent[Viewport]]] = Event(ChangeEvent)
    _is_app: ClassVar[bool] = True

    if not TYPE_CHECKING:
        # Static clients retain the property-derived constructor signature.
        def __init__(self, **properties: object) -> None:
            from ._config import window_defaults

            values = cast(dict[str, Any], {**window_defaults(), **properties})
            super().__init__(**values)

    def _initialize(self) -> None:
        super()._initialize()
        self._state = "CREATED"
        self._runtime: Runtime | None = None
        self._file_busy = False
        self._built = False
        self._build_failed = False
        self._destroy_requested = False
        self._generation = 0
        self._presentation: Presentation | None = None

    def _validate_live_update(self, name, value) -> None:
        super()._validate_live_update(name, value)
        if name == "visible" and not value and self._runtime is not None:
            if self._runtime.presentation.modal or self._runtime._owned:
                raise LifecycleError(
                    "An active modal window or its owner cannot be hidden"
                )
        if self._runtime is not None and self._state != "STARTING":
            if (
                name in ("resizable", "ui_scale", "max_pending_handlers")
                and value != self._values[name]
            ):
                raise LifecycleError(f"Set {name} in build() before the host opens")
            if (
                name in ("width", "height")
                and value != self._values[name]
                and "host_resize" not in self.capabilities
            ):
                raise CapabilityError("This platform owns the app viewport size")

    def build(self) -> None:
        """Override to create the interface. Called once by the runtime."""

    def _changed(self, field, old, value):
        super()._changed(field, old, value)
        if field.name in ("width", "height") and self._runtime is not None:
            self._runtime._pending_size[field.name] = value
        if field.name == "reduce_motion" and value and self._runtime is not None:
            self._runtime.motion.clear()

    @property
    def lifecycle_state(self) -> str:
        return self._state

    @property
    def is_open(self) -> bool:
        return self._state == "RUNNING"

    @caller_side
    def run(
        self,
        *,
        backend: Host | BackendName | None = None,
    ) -> Window:
        """Refresh caller namespace handlers and open through the first content frame."""
        from .runtime import open_window

        self._prepare_caller_side(backend)
        call_opening(open_window, self, backend=backend, handlers=capture_handlers(self))
        return self

    @caller_side
    def run_blocking(self, *, backend: Host | BackendName | None = None) -> Window:
        """Refresh caller namespace handlers, open, and wait for that opening to close."""
        self._require_waiting_caller()
        from .runtime import open_window

        self._prepare_caller_side(backend)
        presentation = call_opening(open_window, self, backend=backend, handlers=capture_handlers(self))
        presentation.wait()
        return self

    @staticmethod
    def _prepare_caller_side(backend) -> None:
        # Signal handlers belong to the main thread; the UI owner cannot install
        # them. A terminal opening restores the console on SIGTERM/SIGHUP.
        from .backends.terminal import prepare_signal_restoration

        prepare_signal_restoration(backend)

    @caller_side
    def wait(self, timeout: float | None = None) -> Outcome:
        """Wait for the captured opening without changing input modality."""
        self._require_waiting_caller()
        return call(self._capture_presentation).wait(timeout)

    @caller_side
    async def wait_async(self) -> Outcome:
        presentation = await call_async(self._capture_presentation)
        return await presentation.wait_async()

    @caller_side
    def show_modal(self, owner: Window, *, backend: Host | BackendName | None = None):
        """Open an owner-modal window on hosts supporting native_modal.

        The supplied backends do not advertise this capability. Use
        SubWindow.show_modal for a portable in-app dialog.
        Return the modal opening's submitted result.
        """
        self._require_waiting_caller()
        from .runtime import open_window

        presentation = call_opening(
            open_window, self, backend=backend, modal=True, owner=owner,
            handlers=capture_handlers(self),
        )
        return presentation.wait().result

    @caller_side
    async def show_modal_async(
        self, owner: Window, *, backend: Host | BackendName | None = None
    ):
        """Async show_modal; both hosts must support native_modal.

        Use SubWindow.show_modal_async for dialogs on the supplied backends.
        """
        from .runtime import open_window

        presentation = await call_async(
            open_window, self, backend=backend, modal=True, owner=owner,
            handlers=capture_handlers(self),
        )
        return (await presentation.wait_async()).result

    @staticmethod
    def _require_waiting_caller() -> None:
        ensure_wait_allowed()

    def _capture_presentation(self) -> Presentation:
        check_origin(self)
        if self._presentation is None:
            raise LifecycleError("This window has never been opened")
        return self._presentation

    def request_frame(self) -> None:
        """Request presentation without invalidating drawing or layout.

        Requests coalesce and obey fps_limit. Valid cached bodies can be reused.
        Before run(), the initial frame satisfies the request.
        """
        self.invalidate(Dirty.NONE)

    @property
    def capabilities(self) -> frozenset[str]:
        return self._runtime.host.capabilities if self._runtime else frozenset()

    @property
    def viewport(self) -> Viewport:
        """Current logical viewport, display scale, safe area and keyboard occlusion."""
        self._check_live()
        if self._runtime is not None:
            return self._runtime.viewport
        return Viewport(self.width or 960, self.height or 640, self.ui_scale or 1)

    @property
    def is_suspended(self) -> bool:
        self._check_live()
        return self._runtime is not None and self._runtime.suspended

    def content_bounds(self, bounds: Rect) -> Rect:
        return super().content_bounds(bounds.intersect(self.viewport.content_bounds))

    def inspect_tree(self) -> tuple[ControlSnapshot, ...]:
        """Capture immutable bounds, styles, handlers, tasks and rendering diagnostics."""
        return inspect_tree(self)

    async def read_session(self, key: str) -> str | None:
        """Read a recoverable UTF-8 draft or setting, independently of saved files."""
        self._check_live()
        validate_session_key(key)
        if self._runtime is None:
            raise LifecycleError("This window is not open")
        if "session_storage" not in self.capabilities:
            raise CapabilityError("This host does not provide session storage")
        text = await self._runtime.host.read_session(key)
        validate_session_text(text)
        return text

    async def write_session(self, key: str, text: str | None) -> None:
        """Store at most 8 MiB of UTF-8 text; None deletes the named session."""
        self._check_live()
        validate_session_key(key)
        validate_session_text(text)
        if self._runtime is None:
            raise LifecycleError("This window is not open")
        if "session_storage" not in self.capabilities:
            raise CapabilityError("This host does not provide session storage")
        await self._runtime.host.write_session(key, text)

    async def open_url(self, url: str) -> None:
        """Ask the host to open an absolute HTTP(S) or mailto URL."""
        from urllib.parse import urlsplit

        self._check_live()
        if not isinstance(url, str) or any(
            ord(char) < 32 or ord(char) == 127 for char in url
        ):
            raise ValueError("URL must be a string without control characters")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https", "mailto") or not (
            parsed.netloc if parsed.scheme != "mailto" else parsed.path
        ):
            raise ValueError("Use an absolute HTTP(S) or mailto URL")
        if self._runtime is None:
            raise LifecycleError("This window is not open")
        if "open_url" not in self.capabilities:
            raise CapabilityError("This host does not provide URL opening")
        await self._runtime.host.open_url(url)

    @property
    def frame_count(self) -> int:
        """Completed paint/present passes in the active run; zero outside a run."""
        check_ui_thread()
        if self._runtime is None or not self._runtime.frames:
            # build() runs with a Runtime attached before its host is open.
            return 0
        if self._runtime is not None and getattr(self._runtime.host, "native_retained", False):
            return int(self._runtime.host.native_stats()["frames"])
        return self._runtime.frames if self._runtime is not None else 0

    def diagnostics(self, *, include_native: bool = False) -> RenderDiagnostics:
        """Snapshot submissions and host identity without invalidating or polling.

        ``include_native=True`` makes one explicit native stats request (IPC)
        when supported and the first scene has completed. Otherwise native
        counters are None. Outside a run, counts are zero/None and identity is
        absent. Existing frame_count behavior is unchanged.
        """
        self._check_live()
        return self._runtime.diagnostics(include_native=include_native) if self._runtime else RenderDiagnostics()

    @property
    def cache_stats(self) -> CacheStats:
        check_ui_thread()
        return (
            self._runtime.render_cache.stats
            if self._runtime is not None
            else CacheStats()
        )

    @property
    def resource_errors(self) -> tuple[str, ...]:
        """Snapshot of up to 32 recoverable resource errors in this opening.

        Returns an empty tuple outside an active run; reading does not drain it.
        """
        check_ui_thread()
        return tuple(self._runtime.resource_errors) if self._runtime else ()

    def drain_frame_timings(self) -> tuple[FrameTiming, ...]:
        """Take up to 4096 Python scene updates recorded with profile_frames=True.

        Native cached presentations have independent host statistics. Timing
        numbers match runtime submissions, rather than native frame_count.
        """
        check_ui_thread()
        if self._runtime is None:
            return ()
        samples = tuple(self._runtime.frame_timings)
        self._runtime.frame_timings.clear()
        return samples

    def close(self, result=None) -> None:
        """Request closure; return without waiting for closing handlers or a veto."""
        check_ui_thread()
        if self._runtime is not None:
            self._runtime.request_close(result)

    async def close_async(self, result=None) -> bool:
        """Await the shared close decision; True is accepted, False is vetoed."""
        import asyncio

        runtime = self._runtime
        if runtime is None:
            return True
        dispatcher, task = runtime.dispatcher, asyncio.current_task()
        dispatcher.preserve_task(task)
        accepted = False
        try:
            accepted = await runtime.request_close(result)
            return accepted
        finally:
            if not accepted:
                dispatcher.release_task(task)

    async def open_text_file(self) -> TextFile | None:
        """Ask the user for a UTF-8 file; cancellation returns None."""
        return await self._file_operation("open")

    async def save_text_file(
        self,
        text: str,
        *,
        suggested_name: str = "document.txt",
        location: str | None = None,
    ) -> TextFile | None:
        """Save to an opened desktop location or ask the host for a destination."""
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("Text files must be UTF-8 and at most 8 MiB")
        if not suggested_name or any(c in suggested_name for c in "/\\\0"):
            raise ValueError("suggested_name must be a filename, without directories")
        return await self._file_operation("save", text, suggested_name, location)

    async def _file_operation(self, operation, *args):
        self._check_live()
        if self._runtime is None:
            raise LifecycleError("This window is not open")
        if "text_files" not in self.capabilities:
            raise CapabilityError("This host does not provide text file services")
        if self._file_busy:
            raise LifecycleError("Finish the current file operation first")
        self._file_busy = True
        try:
            host = self._runtime.host
            if operation == "open":
                return await host.open_text_file()
            return await host.save_text_file(*args)
        finally:
            self._file_busy = False

    def destroy(self) -> None:
        check_ui_thread()
        if self._disposed or self._destroy_requested:
            return
        self._destroy_requested = True
        if self._runtime is not None:
            self._runtime.force_close(reason="destroyed")
            return
        super().destroy()
        self._state = "DESTROYED"

    async def destroy_async(self) -> None:
        """Destroy terminally and await resource cleanup, bypassing close vetoes."""
        presentation = self._presentation
        self.destroy()
        if presentation is not None:
            await presentation.wait_async()


# Internal imports and existing applications can share the one concrete type.
App = Window
