"""Terminal presentation with an optional C renderer and a pure Python fallback."""

import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from ..host import CapabilityError
from ._native_client import NativeHostError, native_executable
from ._term_session import TerminalSession, install_signal_restoration
from .native import NativeTermTextHost
from .term_text import TermTextHost


_OVERRIDE: ContextVar[str | None] = ContextVar("pysual_build_terminal_renderer", default=None)


@contextmanager
def terminal_renderer_override(name):
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
    def __init__(self, width, height):
        self.columns, self.rows = max(1, int(width / 8)), max(1, int(height / 16))

    def enter(self): pass
    def exit(self): pass
    def read(self, timeout=0): return b""
    def write(self, data): pass
    def dimensions(self): return self.columns, self.rows, self.columns * 8, self.rows * 16


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

    def __init__(self, *, renderer=None, hidden=False, color="auto", io=None,
                 executable=None, probe_timeout=0.15, environ=None):
        from .._config import current
        configured = current().terminal_renderer
        choice = renderer if renderer is not None else _OVERRIDE.get() or configured or os.environ.get("PYSUAL_TERMINAL", "auto")
        if choice not in {"auto", "c", "python"}:
            raise ValueError("terminal renderer must be 'auto', 'c' or 'python'")
        if color not in {"auto", "truecolor", "256", "16", "none"}:
            raise ValueError("color must be auto, truecolor, 256, 16 or none")
        if io is not None and choice == "c":
            raise ValueError("an injected terminal byte transport requires renderer='python'")
        self.requested_renderer, self.renderer = choice, None
        self.fallback_reason = None
        self._hidden, self._color, self._io = bool(hidden), color, io
        self._executable = Path(executable) if executable is not None else native_executable()
        self._probe_timeout, self._environ = probe_timeout, environ
        self._wake, self._namespace, self._policy = None, None, None
        self._host, self._opened, self._closed = None, False, False

    def __getattr__(self, name):
        host = self.__dict__.get("_host")
        if host is None:
            raise AttributeError(name)
        return getattr(host, name)

    @property
    def capabilities(self):
        return self._host.capabilities if self._host is not None else frozenset()

    @property
    def hidden(self):
        return self._hidden

    def set_wake_callback(self, callback):
        self._wake = callback
        if self._host is not None:
            wake = getattr(self._host, "set_wake_callback", None)
            if wake is not None:
                wake(callback)

    def set_session_namespace(self, name):
        self._namespace = name
        if self._host is not None:
            self._host.set_session_namespace(name)

    def configure_rendering(self, **settings):
        if self._host is not None:
            configure = getattr(self._host, "configure_rendering", None)
            if configure is not None:
                configure(**settings)
        self._policy = settings

    def _prepare(self, host):
        if self._namespace is not None:
            host.set_session_namespace(self._namespace)
        wake = getattr(host, "set_wake_callback", None)
        if wake is not None and self._wake is not None:
            wake(self._wake)
        if self._policy is not None:
            configure = getattr(host, "configure_rendering", None)
            if configure is not None:
                configure(**self._policy)

    def open(self, title, width, height, resizable, scale):
        if self._opened or self._closed:
            raise RuntimeError("Create a new terminal host for each opening")
        if scale not in (None, 1):
            raise CapabilityError("Terminal cells use ui_scale=1")
        use_c = self.requested_renderer != "python" and self._io is None
        if use_c:
            host = NativeTermTextHost(hidden=self._hidden, executable=self._executable, color=self._color)
            self._prepare(host)
            try:
                host.open(title, width, height, resizable, scale)
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

    def snapshot(self):
        if not self._opened:
            raise RuntimeError("Terminal is not open")
        if self.renderer == "c":
            return self._host.snapshot()
        renderer = self._host._renderer
        cells = renderer.cells
        return {
            "columns": renderer.columns, "rows": renderer.rows,
            "rows_text": ["".join(cell.text for cell in cells[start:start + renderer.columns])
                          for start in range(0, len(cells), renderer.columns)],
            "cells": [{"text": cell.text, "foreground": list(cell.foreground),
                       "background": list(cell.background), "width": cell.width,
                       "underline": cell.underline} for cell in cells],
        }

    def close(self):
        if not self._closed:
            self._closed = True
            if self._host is not None:
                self._host.close()
            self._opened = False


def terminal(*, renderer=None, **options):
    """Construct a terminal target, e.g. ``app.run(backend=terminal(renderer='python'))``."""
    return TerminalHost(renderer=renderer, **options)


def prepare_signal_restoration(backend=None) -> bool:
    """Own SIGTERM/SIGHUP console restoration before a Python terminal opening.

    Window gateways call this on the caller's thread, because sessions open on
    the UI owner and only the main thread can install handlers. The C renderer
    restores its own console when its process ends; other backends do nothing.
    """
    if backend is None or isinstance(backend, str):
        from .._config import resolved_backend
        from . import resolve_backend

        try:
            selected = resolve_backend(resolved_backend(backend))
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
