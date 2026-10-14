"""Terminal presentation with an optional C renderer and a pure Python fallback."""

import os
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Literal, Protocol, cast

from ..geometry import Rect
from ..host import CapabilityError
from ._native_client import NativeHostError, native_executable
from ._term_io import TerminalTransport
from ._term_session import TerminalSession, install_signal_restoration
from .native import NativeTermTextHost
from .term_text import TermTextHost


class _NativeTerminal(Protocol):
    def open(
        self,
        title: str,
        width: float,
        height: float,
        resizable: bool,
        scale: float | None,
    ) -> None: ...

    def snapshot(self) -> object: ...


_OVERRIDE: ContextVar[str | None] = ContextVar("pysual_build_terminal_renderer", default=None)


@contextmanager
def terminal_renderer_override(name: str) -> Generator[None, None, None]:
    """Keep a build's selected renderer consistent with its bundled resources."""
    if name not in {"auto", "c", "python"}:
        raise ValueError("terminal renderer must be 'auto', 'c' or 'python'")
    token = _OVERRIDE.set(name)
    try:
        yield
    finally:
        _OVERRIDE.reset(token)


class _HeadlessIO:
    """No console modes or writes: deterministic cell rendering for tests/tools."""

    def __init__(self, width: float, height: float) -> None:
        self.columns = max(1, int(width / 8))
        self.rows = max(1, int(height / 16))

    def enter(self) -> None:
        pass

    def exit(self) -> None:
        pass

    def read(self, timeout: float = 0) -> bytes:
        return b""

    def write(self, data: bytes) -> None:
        pass

    def dimensions(self) -> tuple[int, int, int, int]:
        return self.columns, self.rows, self.columns * 8, self.rows * 16


class TerminalHost:
    """Use ``renderer='auto'``, ``'c'`` or ``'python'`` (PYSUAL_TERMINAL default).

    Automatic selection falls back only while opening an unavailable native
    helper. It never hides malformed scenes, presentation errors or later native
    crashes. ``renderer`` exposes the implementation actually selected after
    opening; ``fallback_reason`` explains an automatic fallback. ``hidden=True``
    uses a bounded cell buffer without touching the caller's console.
    """
    backend = "terminal"
    text_row_height = 16

    def __init__(
        self,
        *,
        renderer: Literal["auto", "c", "python"] | None = None,
        hidden: bool = False,
        color: Literal["auto", "truecolor", "256", "16", "none"] = "auto",
        io: TerminalTransport | None = None,
        executable: str | Path | None = None,
        probe_timeout: float = 0.15,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        from .._config import current
        configured = current().terminal_renderer
        choice = renderer if renderer is not None else _OVERRIDE.get() or configured or os.environ.get("PYSUAL_TERMINAL", "auto")
        if choice not in {"auto", "c", "python"}:
            raise ValueError("terminal renderer must be 'auto', 'c' or 'python'")
        if color not in {"auto", "truecolor", "256", "16", "none"}:
            raise ValueError("color must be auto, truecolor, 256, 16 or none")
        if io is not None and choice == "c":
            raise ValueError("an injected terminal byte transport requires renderer='python'")
        self.requested_renderer = choice
        self.renderer: str | None = None
        self.fallback_reason: str | None = None
        self._hidden = bool(hidden)
        self._color = color
        self._io = io
        self._executable = Path(executable) if executable is not None else native_executable()
        self._probe_timeout = probe_timeout
        self._environ = environ
        self._wake: Callable[[], None] | None = None
        self._namespace: str | None = None
        self._policy: dict[str, object] | None = None
        self._host: NativeTermTextHost | TermTextHost | None = None
        self._opened = False
        self._closed = False

    def __getattr__(self, name: str) -> object:
        host = cast("dict[str, object]", self.__dict__).get("_host")
        if host is None:
            raise AttributeError(name)
        return cast(object, getattr(host, name))

    @property
    def capabilities(self) -> frozenset[str]:
        return self._host.capabilities if self._host is not None else frozenset()

    @property
    def hidden(self) -> bool:
        return self._hidden

    @property
    def render_scale(self) -> float:
        host = self._host
        if host is None:
            raise AttributeError("render_scale")
        return host.render_scale

    @property
    def resource_revision(self) -> int:
        host = self._host
        if host is None:
            raise AttributeError("resource_revision")
        return host.resource_revision

    def clip(self, rect: Rect | None) -> None:
        host = self._host
        if host is None:
            raise AttributeError("clip")
        host.clip(rect)

    def surface_blit(self, surface: object) -> None:
        host = self._host
        if isinstance(host, TermTextHost):
            host.surface_blit(surface)
            return
        if host is None:
            raise AttributeError("surface_blit")
        method = getattr(host, "surface_blit", None)
        if not callable(method):
            raise AttributeError("surface_blit")
        cast(Callable[[object], object], method)(surface)

    def set_wake_callback(self, callback: Callable[[], None] | None) -> None:
        self._wake = callback
        host = self._host
        if host is not None:
            wake = getattr(cast(object, host), "set_wake_callback", None)
            if wake is not None:
                cast(Callable[[Callable[[], None] | None], object], wake)(callback)

    def set_session_namespace(self, name: str) -> None:
        self._namespace = name
        if self._host is not None:
            self._host.set_session_namespace(name)

    def configure_rendering(self, **settings: object) -> None:
        host = self._host
        if host is not None:
            configure = getattr(cast(object, host), "configure_rendering", None)
            if configure is not None:
                cast(Callable[..., object], configure)(**settings)
        self._policy = settings

    def _prepare(self, host: NativeTermTextHost | TermTextHost) -> None:
        if self._namespace is not None:
            host.set_session_namespace(self._namespace)
        wake = getattr(cast(object, host), "set_wake_callback", None)
        if wake is not None and self._wake is not None:
            cast(Callable[[Callable[[], None]], object], wake)(self._wake)
        if self._policy is not None:
            configure = getattr(cast(object, host), "configure_rendering", None)
            if configure is not None:
                cast(Callable[..., object], configure)(**self._policy)

    def open(
        self,
        title: str,
        width: float,
        height: float,
        resizable: bool,
        scale: float | None,
    ) -> None:
        if self._opened or self._closed:
            raise RuntimeError("Create a new terminal host for each opening")
        if scale not in (None, 1):
            raise CapabilityError("Terminal cells use ui_scale=1")
        use_c = self.requested_renderer != "python" and self._io is None
        if use_c:
            host = cast(
                Callable[..., NativeTermTextHost], NativeTermTextHost
            )(hidden=self._hidden, executable=self._executable, color=self._color)
            self._prepare(host)
            try:
                cast(_NativeTerminal, host).open(title, width, height, resizable, scale)
            except (NativeHostError, OSError, TimeoutError) as error:
                host.close()
                if self.requested_renderer == "c":
                    raise
                self.fallback_reason = str(error)
            else:
                self._host, self.renderer, self._opened = host, "c", True
                return
        elif self.requested_renderer == "auto":
            self.fallback_reason = "An injected terminal byte transport requires Python rendering"
        io = self._io if self._io is not None else _HeadlessIO(width, height) if self._hidden else None
        host = TermTextHost(io=io, color=self._color, environ=self._environ,
                            probe_timeout=0 if self._hidden else self._probe_timeout)
        self._prepare(host)
        try:
            host.open(title, width, height, resizable, scale)
        except BaseException:
            host.close()
            raise
        self._host, self.renderer, self._opened = host, "python", True

    def snapshot(self) -> object:
        if not self._opened:
            raise RuntimeError("Terminal is not open")
        host = self._host
        if host is None:
            raise RuntimeError("Terminal is not open")
        if self.renderer == "c":
            snapshot = getattr(cast(object, host), "snapshot", None)
            if not callable(snapshot):
                raise RuntimeError("Native terminal host cannot capture cells")
            return cast(Callable[[], object], snapshot)()
        if not isinstance(host, TermTextHost):
            raise RuntimeError("Terminal host has no cell renderer")
        renderer = host._renderer
        cells = renderer.cells
        return {
            "columns": renderer.columns, "rows": renderer.rows,
            "rows_text": ["".join(cell.text for cell in cells[start:start + renderer.columns])
                          for start in range(0, len(cells), renderer.columns)],
            "cells": [{"text": cell.text, "foreground": list(cell.foreground),
                       "background": list(cell.background), "width": cell.width,
                       "underline": cell.underline} for cell in cells],
        }

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._host is not None:
                self._host.close()
            self._opened = False


def terminal(
    *,
    renderer: Literal["auto", "c", "python"] | None = None,
    hidden: bool = False,
    color: Literal["auto", "truecolor", "256", "16", "none"] = "auto",
    io: TerminalTransport | None = None,
    executable: str | Path | None = None,
    probe_timeout: float = 0.15,
    environ: Mapping[str, str] | None = None,
) -> TerminalHost:
    """Construct a terminal target, e.g. ``app.run(backend=terminal(renderer='python'))``."""
    return TerminalHost(
        renderer=renderer,
        hidden=hidden,
        color=color,
        io=io,
        executable=executable,
        probe_timeout=probe_timeout,
        environ=environ,
    )


def prepare_signal_restoration(backend: object = None) -> bool:
    """Own SIGTERM/SIGHUP console restoration before a Python terminal opening.

    Window gateways call this on the caller's thread, because sessions open on
    the UI owner and only the main thread can install handlers. The C renderer
    restores its own console when its process ends; other backends do nothing.
    """
    if backend is None or isinstance(backend, str):
        from .._config import resolved_backend
        from . import resolve_backend

        try:
            from . import BackendName
            selected = resolve_backend(
                resolved_backend(cast("BackendName | None", backend))
            )
        except Exception:
            return False  # open_window reports the invalid selection itself.
        if selected != "terminal":
            return False
    elif isinstance(backend, TerminalHost):
        if backend.requested_renderer == "c":
            return False
    elif not isinstance(backend, TerminalSession):
        return False
    return install_signal_restoration()
