"""Three presentation targets sharing one control, layout and event model."""

import importlib
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Literal, cast
from ..host import CapabilityError, Host

BackendName = Literal["window", "terminal", "web"]

@dataclass(frozen=True)
class BackendSpec:
    name: BackendName
    label: str
    module: str
    host_class: str

BACKENDS = (
    BackendSpec("window", "Native window", "pysual.backends.native", "NativeHost"),
    BackendSpec("terminal", "Terminal", "pysual.backends.terminal", "TerminalHost"),
    BackendSpec("web", "Browser SVG", "pysual.backends.web", "WebHost"),
)
BACKEND_NAMES: tuple[BackendName, ...] = tuple(spec.name for spec in BACKENDS)
_BY_NAME = {spec.name: spec for spec in BACKENDS}
_OVERRIDE: ContextVar[BackendName | None] = ContextVar("pysual_build_backend", default=None)


def get_backend(name: str) -> BackendSpec:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError("backend must be 'window', 'terminal', 'web', or a Host instance") from None


def _validate_platform(spec: BackendSpec) -> None:
    if sys.platform == "emscripten" and spec.name != "web":
        raise CapabilityError(f"The {spec.name} backend requires native Python")


def resolve_backend(backend: Host | BackendName | None = None) -> Host | BackendName:
    if backend is not None and not isinstance(backend, str):
        return backend
    if isinstance(backend, str):
        get_backend(backend)
    selected = _OVERRIDE.get() or backend or ("web" if sys.platform == "emscripten" else "window")
    spec = get_backend(selected)
    _validate_platform(spec)
    return spec.name


def create_host(name: BackendName) -> Host:
    spec = get_backend(name)
    _validate_platform(spec)
    return cast(Host, getattr(importlib.import_module(spec.module), spec.host_class)())


class _SingleSurfaceSession:
    def __init__(self, name):
        self.name, self.active = name, None

    def close(self):
        if self.active is not None:
            self.active.close()


class _SingleSurfaceHost:
    def __init__(self, session, host):
        self._session, self._host = session, host

    def __getattr__(self, name):
        return getattr(self._host, name)

    @property
    def diagnostic_name(self):
        return getattr(self._host, "diagnostic_name", type(self._host).__name__)

    def open(self, *args):
        if self._session.active is not None:
            raise CapabilityError(f"The {self._session.name} backend owns one presentation surface")
        self._host.open(*args)
        self._session.active = self

    def close(self):
        try:
            self._host.close()
        finally:
            if self._session.active is self:
                self._session.active = None


_MANAGED_SESSIONS: dict[tuple[int, str], _SingleSurfaceSession] = {}


def create_managed_host(name: BackendName) -> Host:
    """Own terminal/DOM surfaces; each window has its own native process."""
    spec = get_backend(name)
    _validate_platform(spec)
    host = create_host(name)
    if name == "terminal" or (name == "web" and sys.platform == "emscripten"):
        key = (threading.get_ident(), name)
        session = _MANAGED_SESSIONS.setdefault(key, _SingleSurfaceSession(name))
        return cast(Host, _SingleSurfaceHost(session, host))
    return host


def shutdown_managed_hosts() -> None:
    failures = []
    for key, session in tuple(_MANAGED_SESSIONS.items()):
        if key[0] == threading.get_ident():
            del _MANAGED_SESSIONS[key]
            try:
                session.close()
            except Exception as exc:
                failures.append(exc)
    if failures:
        raise RuntimeError("Presentation shutdown failed") from failures[0]


@contextmanager
def backend_override(name: str) -> Iterator[None]:
    token = _OVERRIDE.set(get_backend(name).name)
    try:
        yield
    finally:
        _OVERRIDE.reset(token)
