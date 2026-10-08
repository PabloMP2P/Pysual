"""Thread-safe completion for one opening, independent of any particular waiter."""

from __future__ import annotations

import asyncio
import sys
from concurrent.futures import Future, InvalidStateError
from dataclasses import dataclass

from ._engine import is_ui_thread
from .errors import LifecycleError


def ensure_wait_allowed() -> None:
    """Desktop owners never block; Pyodide permits only a JSPI suspension."""
    if not is_ui_thread():
        return
    if sys.platform == "emscripten":
        from pyodide.ffi import can_run_sync  # type: ignore[import-not-found]

        if can_run_sync():
            return
        raise LifecycleError(
            "A browser wait requires JSPI and a suspension-enabled entry point"
        )
    raise LifecycleError("Blocking window operations cannot run on the UI owner")


@dataclass(frozen=True)
class Outcome:
    result: object = None
    reason: str = "closed"


class Presentation:
    """The identity and immutable completion of one native or in-app opening."""

    def __init__(self, generation: int, *, modal: bool = False, owner=None):
        self.generation = generation
        self.modal = modal
        self.owner = owner
        self._future: Future[Outcome] = Future()

    @property
    def done(self) -> bool:
        return self._future.done()

    def complete(self, result=None, *, reason: str = "closed") -> None:
        try:
            self._future.set_result(Outcome(result, reason))
        except InvalidStateError:
            # Completion can race a host failure during forced shutdown.
            pass

    def fail(self, error: BaseException) -> None:
        try:
            self._future.set_exception(error)
        except InvalidStateError:
            pass

    def wait(self, timeout: float | None = None) -> Outcome:
        ensure_wait_allowed()
        if sys.platform == "emscripten":
            from pyodide.ffi import run_sync  # type: ignore[import-not-found]

            waiter = self.wait_async()
            if timeout is not None:
                waiter = asyncio.wait_for(waiter, timeout)
            return run_sync(waiter)
        return self._future.result(timeout)

    async def wait_async(self) -> Outcome:
        # Waiting for completion does not cancel the bridge/shared operation.
        # Unlike shield(), it also does not separately log a later exception
        # after a cancelled waiter has detached (notably on Python 3.14).
        bridge = asyncio.wrap_future(self._future)
        bridge.add_done_callback(
            lambda future: None if future.cancelled() else future.exception()
        )
        await asyncio.wait((bridge,))
        return bridge.result()
