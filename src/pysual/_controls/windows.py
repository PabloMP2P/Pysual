"""In-app window presentation, dragging, and closure."""

from __future__ import annotations

import asyncio
from math import ceil
from typing import ClassVar
from .._text_display import single_line
from .._engine import caller_side, call
from ..controls import Container, Control
from ..errors import LifecycleError
from ..events import ClosingEvent, Event
from ..geometry import Rect
from ..schema import Dirty, prop
from ..painting import resolve_style
from ..presentation import Outcome, Presentation, ensure_wait_allowed


class SubWindow(Container):
    cache_paint: bool = prop(default=True)
    title: str = prop(default="Window", affects=Dirty.MEASURE | Dirty.PAINT)
    width: float | None = prop(default=360.0, minimum=0, affects=Dirty.MEASURE)
    height: float | None = prop(default=240.0, minimum=0, affects=Dirty.MEASURE)
    padding: float = prop(default=12.0, minimum=0, affects=Dirty.MEASURE)
    draggable: bool = prop(default=True)
    resizable: bool = prop(default=True)
    closable: bool = prop(default=True, affects=Dirty.MEASURE | Dirty.PAINT)
    closing: ClassVar[Event[ClosingEvent]] = Event(ClosingEvent)
    _overlay: ClassVar[bool] = True
    _style_kind: ClassVar[str] = "SubWindow"
    style_parts: ClassVar[tuple[str, ...]] = ("body", "titlebar", "close")

    def _title_metrics(self):
        style = resolve_style(self, "titlebar")
        return style, ceil(style.font_size * 1.2 + style.padding * 2)

    def measure(self, host):
        return self.measure_available(host)

    def measure_available(self, host, *, width=None, height=None):
        style, title_height = self._title_metrics()
        w, h = self.measure_content(host, width=width,
            height=None if height is None else max(0, height - title_height))
        if self.width is None:
            title_width = host.measure(single_line(self.title), style.font_size, style.font_family == "mono")[0]
            w = max(w, title_width + style.padding * 2 + (title_height if self.closable else 0))
        if self.height is None:
            h += title_height
        return w, h

    def _close_bounds(self):
        _, height = self._title_metrics()
        return Rect(max(0, self._rect.width - height), 0,
                    min(height, self._rect.width), min(height, self._rect.height))

    def _title_drag_at(self, x, y):
        _, title_height = self._title_metrics()
        return (
            self.draggable and 0 <= x < self._rect.width and 0 <= y < title_height
            and not (self.closable and self._close_bounds().contains(x, y))
            and not (self.resizable and x > self._rect.width - 20
                     and y > self._rect.height - 20)
        )

    def content_bounds(self, bounds):
        area = super().content_bounds(bounds)
        _, height = self._title_metrics()
        return Rect(area.x, min(area.bottom, area.y + height), area.width, max(0, area.height - height))

    def _initialize(self):
        super()._initialize()
        self._drag = None
        self._close_pending: asyncio.Task[bool] | None = None
        self._close_armed = None
        self._close_hover = False
        self._presentation: Presentation | None = None
        self._generation = 0
        self._presentation_error: BaseException | None = None

    def _close_style(self):
        state = (
            "disabled" if not self.effective_enabled else
            "pressed" if self._close_armed is not None and self._close_hover else
            "hover" if self._close_hover and self._hover else
            "normal"
        )
        return resolve_style(self, "close", state=state)

    def paint(self, p, /):
        p.body()
        titlebar = p.style("titlebar")
        _, height = self._title_metrics()
        with p._clipped(Rect(0, 0, p.width, min(height, p.height))):
            p.surface(Rect(1, 1, max(0, p.width - 2), height), titlebar)
            close = self._close_bounds()
            available = (close.x if self.closable else p.width) - titlebar.padding * 2
            label = p.elide(single_line(self.title), max(0, available), size=titlebar.font_size,
                            font_family=titlebar.font_family)
            text_width, text_height = p.measure(label, size=titlebar.font_size,
                                                font_family=titlebar.font_family)
            text_y = max(0, (height - text_height) / 2)
            if label and titlebar.pattern not in (None, "none"):
                # Patterned chrome keeps a quiet strip behind the caption.
                # The surrounding titlebar still retains its original texture.
                strip_x = max(1, titlebar.padding - 3)
                strip_right = close.x if self.closable else p.width - 1
                p.rect(Rect(strip_x, text_y,
                            max(0, min(text_width + 6, strip_right - strip_x)),
                            text_height), titlebar.fill)
            p.text(label, titlebar.padding, text_y,
                   size=titlebar.font_size, color=titlebar.foreground,
                   font_family=titlebar.font_family)
            if self.closable:
                style = self._close_style()
                face = close.inset(style.padding)
                p.surface(face, style)
                size = min(style.font_size * 1.4, face.height * 0.8)
                if size > 0:
                    w, h = p.measure("×", size=size, font_family=style.font_family)
                    p.text("×", face.x + (face.width - w) / 2,
                           face.y + (face.height - h) / 2, size=size,
                           color=style.foreground, font_family=style.font_family)
        if self.resizable:
            p.line(
                p.width - 12,
                p.height - 4,
                p.width - 4,
                p.height - 12,
                p.theme.tokens.muted,
            )

    def handle_input(self, e, /):
        x, y = e.x - self._rect.x, e.y - self._rect.y
        if e.kind in ("pointer_down", "pointer_move", "pointer_up"):
            hovered = self.closable and self._close_bounds().contains(x, y)
            if self._close_hover != hovered:
                self._close_hover = hovered
                self.invalidate()
        if e.kind in ("blur", "pointer_cancel"):
            self._drag = None
            if self._close_hover or self._close_armed is not None:
                self.invalidate()
            self._close_hover = False
            self._close_armed = None
        elif e.kind == "pointer_down" and e.button == 1:
            if self.closable and self._close_bounds().contains(x, y):
                self._close_armed = e.pointer_id
                self.invalidate()
                return
            resize = (
                self.resizable
                and x > self._rect.width - 20
                and y > self._rect.height - 20
            )
            if resize or self._title_drag_at(x, y):
                assert self._parent is not None
                parent = self._parent.content_bounds(self._parent._rect)
                # Anchors can move the arranged window away from its
                # declared offsets. Keep this origin fixed for either gesture.
                left, top = self._rect.x - parent.x, self._rect.y - parent.y
                self._drag = (
                    e.x,
                    e.y,
                    left,
                    top,
                    self._rect.width,
                    self._rect.height,
                    resize,
                )
        elif e.kind == "pointer_move" and self._drag:
            px, py, left, top, w, h, resize = self._drag
            dx, dy = e.x - px, e.y - py
            if resize:
                assert self._parent is not None
                parent = self._parent.content_bounds(self._parent._rect)
                # Keep the grip reachable without overriding minimum sizes
                # when the parent has too little space to contain the window.
                self.width = max(120, self.min_width, min(w + dx, parent.width - left))
                self.height = max(80, self.min_height, min(h + dy, parent.height - top))
            else:
                assert self._parent is not None
                parent = self._parent.content_bounds(self._parent._rect)
                self.left = max(
                    32 - self._rect.width, min(parent.width - 32, left + dx)
                )
                self.top = max(0, min(parent.height - 24, top + dy))
        elif e.kind == "pointer_up":
            self._drag = None
            if self._close_armed == e.pointer_id:
                self._close_armed = None
                self.invalidate()
                if e.button == 1 and self.closable and self._close_bounds().contains(x, y):
                    self.create_task(self.close_async())
        elif e.kind == "key_down" and e.key == "Escape" and self.closable:
            self.create_task(self.close_async())

    def _request_close(self, result=None):
        if self._disposed:
            return None
        if not self.visible and (self._presentation is None or self._presentation.done):
            return None
        if self._close_pending is not None and not self._close_pending.done():
            return self._close_pending
        runtime = getattr(self._root(), "_runtime", None)
        if runtime is not None:
            runtime._modality._guard()
        dispatcher = self._dispatcher
        if dispatcher is None:
            if runtime is not None:
                runtime._modality.finish(self, result)
            else:
                self.visible = False
            return None

        presentation = self._presentation
        closing = self.closing

        async def decide():
            if (self._disposed or self._presentation is not presentation
                    or (presentation is not None and presentation.done)):
                return True
            accepted = await dispatcher.decide(closing, ClosingEvent(source=self))
            if (accepted and not self._disposed and self._presentation is presentation
                    and (presentation is None or not presentation.done)):
                if runtime:
                    runtime._modality.finish(self, result)
                elif not self._disposed:
                    self.visible = False
            return accepted

        # The close decision belongs to the containing window, not the button
        # which requested it. A normal close retains that button and its task.
        self._close_pending = dispatcher.create_task(decide(), self._root())
        return self._close_pending

    def close(self, result=None) -> None:
        """Request vetoable closure; retain this control tree for another run."""
        self._request_close(result)

    async def close_async(self, result=None) -> bool:
        """Await the shared closing decision without owning its cancellation."""
        pending = self._request_close(result)
        if pending is None:
            return True
        if pending is asyncio.current_task():
            raise LifecycleError("A closing handler cannot await its own close decision")
        return await asyncio.shield(pending)

    def show(self, *, modal: bool = False, owner: Control | None = None) -> Presentation:
        """Show this in-app window and return its exact opening handle."""
        self._check_live()
        runtime = getattr(self._root(), "_runtime", None)
        if runtime is None:
            raise LifecycleError("In-app windows require a running owner Window")
        return runtime._modality.show(self, modal=modal, owner=owner)

    def run(self):
        """Show modelessly through its first frame and return this instance."""
        self.show()
        return self

    @caller_side
    def run_blocking(self):
        """Show modelessly and wait for this exact opening to close."""
        self._check_waiting_caller("run_blocking", "wait_async")
        presentation = call(self.show)
        presentation.wait()
        return self

    @staticmethod
    def _check_waiting_caller(operation, alternative):
        try:
            ensure_wait_allowed()
        except LifecycleError as error:
            raise LifecycleError(
                f"{operation}: {error}; use await {alternative}()"
            ) from None

    def _opening(self) -> Presentation:
        self._check_live()
        if self._presentation is None:
            raise LifecycleError("This window has not been shown")
        return self._presentation

    @caller_side
    def wait(self, timeout: float | None = None) -> Outcome:
        """Wait for the captured opening, or return the last completed outcome."""
        self._check_waiting_caller("wait", "wait_async")
        return call(self._opening).wait(timeout)

    async def wait_async(self) -> Outcome:
        return await self._opening().wait_async()

    def hide(self) -> None:
        """Hide a modeless opening without completing it."""
        self.visible = False

    def destroy(self) -> None:
        if self._disposed:
            return
        self._close_armed = None
        self._drag = None
        runtime = getattr(self._root(), "_runtime", None)
        if runtime is not None:
            runtime._modality.destroying(self)
        super().destroy()

    @caller_side
    def show_modal(self, *, owner: Control | None = None):
        """Open owner-modally and wait on the invoking application thread."""
        self._check_waiting_caller("show_modal", "show_modal_async")
        return call(self.show, modal=True, owner=owner).wait().result

    async def show_modal_async(self, *, owner: Control | None = None):
        """Open owner-modally without blocking the UI event loop."""
        return (await self.show(modal=True, owner=owner).wait_async()).result


# Keep public imports, diagnostics, and reflection stable across source moves.
SubWindow.__module__ = "pysual.widgets"
