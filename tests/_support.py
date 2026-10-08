"""Internal lifecycle proof. This is not an advertised UI backend or App.run()."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

from pysual.app import App
from pysual.errors import LifecycleError
from pysual.events import ClosingEvent, Dispatcher, UiEvent
from pysual.schema import check_ui_thread
from pysual._engine import call_async, is_ui_thread


def make_pyodide_runtime(directory: Path) -> Path:
    """Complete build-input fixture; browser integration uses the real runtime."""
    directory.mkdir(exist_ok=True)
    for name in (
        "pyodide.mjs", "pyodide.asm.js", "pyodide.asm.wasm",
        "python_stdlib.zip", "pyodide-lock.json",
    ):
        (directory / name).write_bytes(b"fixture " + name.encode())
    (directory / "package.json").write_text('{"version":"0.29.0"}', encoding="utf-8")
    return directory


class Session:
    def __init__(self, app: App, *, max_pending_handlers: int | None = 1000):
        self.app = app
        self.dispatcher = Dispatcher(max_pending_handlers)
        self._close_task: asyncio.Task[bool] | None = None

    async def request_close(self) -> bool:
        if self.app.lifecycle_state == "CLOSED":
            return True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._decide_close())
        task = self._close_task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._close_task is task:
                self._close_task = None

    async def _decide_close(self) -> bool:
        accepted = await self.dispatcher.decide(
            self.app.closing, ClosingEvent(source=self.app)
        )
        if accepted:
            self.app._state = "CLOSING"
        return accepted


async def exercise(
    app: App,
    scenario: Callable[[Session], Awaitable[None]],
    *,
    max_pending_handlers: int | None = 1000,
) -> None:
    """Build, exercise and dispose a tree without pretending to draw anything."""
    if not is_ui_thread():
        return await call_async(exercise, app, scenario, max_pending_handlers=max_pending_handlers)
    check_ui_thread()
    if app.lifecycle_state != "CREATED":
        raise LifecycleError("Only a fresh App can start")
    session = Session(app, max_pending_handlers=max_pending_handlers)
    app._state = "STARTING"
    try:
        app.build()
        app._bind_handlers(app, type(app).__name__)
        app._attach_dispatcher(session.dispatcher)
        app._state = "RUNNING"
        app.loaded.emit(UiEvent(source=app, origin="system"))
        await scenario(session)
        await session.dispatcher.drain()
    finally:
        app._state = "CLOSING"
        try:
            if session._close_task is not None:
                session._close_task.cancel()
                await asyncio.gather(session._close_task, return_exceptions=True)
            await session.dispatcher.shutdown()
        finally:
            app.destroy()
