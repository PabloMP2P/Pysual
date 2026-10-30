"""Numbered workflows composed from ordinary, persistent content containers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from typing import TYPE_CHECKING, ClassVar, Literal, TypeVar, cast

from ._text_display import single_line
from ._engine import TaskHandle
from ._controls.viewports import ScrollArea
from ._scrolling import handle_scrollbars, paint_scrollbars
from .behaviors import PressBehavior
from .controls import Control
from .events import ChangeEvent, Dispatcher, Event, Validation, ValidationEvent
from .geometry import Rect
from .host import Host, Input
from .painting import Painter, resolve_style
from .schema import Dirty, Field, _init_argument, expect_number, prop
from .theme import ResolvedStyle, StyleState, _readable_ink

_C = TypeVar("_C", bound=Control)
_validating_step: ContextVar[Step | None] = ContextVar("pysual_validating_step", default=None)


class Step(ScrollArea):
    """One step's metadata, validation, and scrollable persistent content.

    When attached to a Stepper, visibility belongs to its selected_index.
    Child controls retain their values and individual visibility between steps.
    Prerequisite references never transfer ownership of the referenced steps.
    """

    title: str = prop(default="Step", affects=Dirty.MEASURE | Dirty.PAINT)
    optional: bool = prop(default=False)
    completed: bool = prop(default=False)
    editable: bool = prop(default=True)
    error: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    layout: Literal["absolute", "stack", "grid", "flow", "dock"] = prop(
        default="stack", affects=Dirty.MEASURE
    )
    padding: float = prop(default=16.0, minimum=0, affects=Dirty.MEASURE)
    spacing: float = prop(default=12.0, minimum=0, affects=Dirty.MEASURE)
    validating: ClassVar[Validation] = Validation()

    if TYPE_CHECKING:
        _requires_input: Sequence[Step] = _init_argument(default=(), alias="requires")
    else:
        def __init__(self, *, requires: Sequence[Step] = (), **properties: object) -> None:
            try:
                super().__init__(**properties)
                self.requires = requires
            except BaseException:
                if hasattr(self, "_children"):
                    self.destroy()
                raise

    def _initialize(self) -> None:
        super()._initialize()
        self._requires: tuple[Step, ...] = ()
        self._validation_generation = 0
        self._validation_error: str | None = None
        self._applying_validation = 0
        self._live_validation = False
        self._live_dirty = False
        self._live_origin: Literal["user", "program", "system"] = "program"
        self._live_token: object | None = None
        self._live_task: TaskHandle | None = None

    @property
    def requires(self) -> tuple[Step, ...]:
        """Steps that must be valid before this step can be entered."""
        self._check_live()
        return self._requires

    @requires.setter
    def requires(self, value: Sequence[Step]) -> None:
        self._check_live()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise TypeError("requires must be a sequence of Step controls")
        required = tuple(value)
        identities: set[int] = set()
        for step in required:
            if not isinstance(step, Step):
                raise TypeError("requires must contain only Step controls")
            step._check_live()
            if step is self:
                raise ValueError("A step cannot require itself")
            if id(step) in identities:
                raise ValueError("requires cannot contain duplicate steps")
            identities.add(id(step))
        pending = list(required)
        visited: set[int] = set()
        while pending:
            step = pending.pop()
            if step is self:
                raise ValueError("Step prerequisites cannot contain a cycle")
            if id(step) not in visited:
                visited.add(id(step))
                pending.extend(step._requires)
        self._check_requires_parent(required)
        if required != self._requires:
            self._requires = required
            self._validation_generation += 1
            if isinstance(self._parent, Stepper):
                self._parent._step_state_changed(navigation=True)

    def _check_requires_parent(self, required: tuple[Step, ...]) -> None:
        if self._parent is not None and required:
            if not isinstance(self._parent, Stepper) or any(
                step._parent is not self._parent or step._disposed for step in required
            ):
                raise ValueError("Prerequisites must be attached to the same Stepper first")

    def on_attached(self) -> None:
        super().on_attached()
        self._check_requires_parent(self._requires)

    def _attach_dispatcher(self, dispatcher: Dispatcher | None) -> None:
        if self._dispatcher is not dispatcher:
            self._validation_generation += 1
            self._cancel_live_validation()
        super()._attach_dispatcher(dispatcher)

    def _cancel_live_validation(self) -> None:
        task, self._live_task = self._live_task, None
        self._live_token = None
        self._live_dirty = False
        if task is not None:
            task.cancel()

    def _descendant_changed(self, event: ChangeEvent[object]) -> None:
        super()._descendant_changed(event)
        if _validating_step.get() is self:
            return  # Normalization inside a validator must not trigger itself.
        self._validation_generation += 1
        if not self._live_validation or self._dispatcher is None:
            return
        self._live_dirty = True
        self._live_origin = event.origin
        if self._live_token is None:
            token = self._live_token = object()
            try:
                self._live_task = self.create_task(self._revalidate_live(token))
            except BaseException:
                self._live_token = None
                raise

    async def _revalidate_live(self, token: object) -> None:
        try:
            await asyncio.sleep(0)  # Coalesce committed edits in the same turn.
            while self._live_token is token and self._live_dirty and self._live_validation:
                self._live_dirty = False
                await self._validate(show_error=True, origin=self._live_origin, live=True)
        finally:
            if self._live_token is token:
                self._live_token = None
                self._live_task = None

    def _apply_validation_result(self, error: str, *, show_error: bool) -> bool:
        generation = self._validation_generation
        self._applying_validation += 1
        try:
            self.update(completed=not error, error=error if show_error or self.error else "")
        finally:
            self._applying_validation -= 1
        if generation == self._validation_generation:
            self._validation_error = error
        return not error

    async def validate(self, *, show_error: bool = True) -> bool:
        """Await validating handlers and update completion and error feedback.

        Return False when rejected or superseded by a newer validation/reset.
        A step without handlers accepts unless set_validation recorded an error.
        """
        self._check_live()
        if type(show_error) is not bool:
            raise TypeError("show_error must be boolean")
        return await self._validate(show_error=show_error, origin=self._origin)

    async def _validate(
        self, *, show_error: bool, origin: Literal["user", "program", "system"],
        live: bool = False,
    ) -> bool:
        self._check_live()
        if not live:
            self._cancel_live_validation()
        self._validation_generation += 1
        generation = self._validation_generation
        channel = self.validating
        if channel._snapshot():
            event = ValidationEvent(source=self, origin=origin)
            token = _validating_step.set(self)
            try:
                accepted = await (self._dispatcher or Dispatcher()).decide(channel, event)
            finally:
                _validating_step.reset(token)
            error = "" if accepted else event.message or "Check this step"
        else:
            error = self._validation_error if self._validation_error is not None else self.error
        if self._disposed or generation != self._validation_generation:
            return False
        return self._apply_validation_result(error, show_error=show_error)

    def _reset_validation(self) -> None:
        self._check_live()
        self._validation_generation += 1
        self._live_validation = False
        self._cancel_live_validation()
        self._applying_validation += 1
        try:
            self.update(completed=False, error="", scroll_x=0, scroll_y=0)
        finally:
            self._applying_validation -= 1
        self._validation_error = None

    def set_validation(self, error: str, *, show_error: bool = True) -> bool:
        """Apply a validation result and return whether the step is valid.

        An empty message completes the step and clears its error. With
        show_error=False, invalid input stays quiet until an error is visible;
        an already visible message still updates to the current error.
        This changes completion and feedback together without navigating.
        """
        self._check_live()
        if type(error) is not str:
            raise TypeError("Validation error must be a string")
        if type(show_error) is not bool:
            raise TypeError("show_error must be boolean")
        self._validation_generation += 1
        return self._apply_validation_result(error, show_error=show_error)

    def _validate_update(self, name: str, value: object) -> None:
        super()._validate_update(name, value)
        if (name == "visible" and isinstance(self._parent, Stepper)
                and not self._parent._syncing):
            raise ValueError("Stepper owns step visibility; set selected_index instead")

    def _changed(self, field: Field, old: object, value: object) -> None:
        super()._changed(field, old, value)
        if field.name == "error" and value:
            self._live_validation = True
        if field.name in ("completed", "error") and not self._applying_validation:
            self._validation_generation += 1
            self._validation_error = self.error or ("" if self.completed else None)
        if (field.name in ("title", "optional", "completed", "editable", "error", "enabled")
                and isinstance(self._parent, Stepper)):
            self._parent._step_state_changed(
                navigation=field.name in ("optional", "editable", "enabled")
            )

    def destroy(self) -> None:
        if self._disposed:
            return
        self._validation_generation += 1
        self._cancel_live_validation()
        parent = self._parent
        selected = (parent.selected_step if isinstance(parent, Stepper)
                    and not parent._destroying else None)
        super().destroy()
        if isinstance(parent, Stepper) and not parent._destroying:
            parent._steps_changed(selected)


class Stepper(ScrollArea):
    """Horizontal or vertical step navigation with one visible content pane.

    select/next/previous and input respect linear progression and editability.
    Indicators and keyboard input validate the destination's prerequisites.
    Synchronous navigation methods consult the current validation state.
    Selecting a heading never submits a form. selected_index is an application
    override. Empty steppers have no selected_step.
    """

    cache_paint: bool = prop(default=True)
    orientation: Literal["horizontal", "vertical"] = prop(
        default="horizontal", affects=Dirty.MEASURE | Dirty.PAINT
    )
    linear: bool = prop(default=True)
    selected_index: int = prop(default=0, minimum=0, persist=False, changed="changed",
                               affects=Dirty.MEASURE | Dirty.HIT_TEST | Dirty.PAINT)
    header_height: float = prop(default=72.0, minimum=48, affects=Dirty.MEASURE)
    step_width: float = prop(default=180.0, minimum=72, affects=Dirty.MEASURE)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)
    changed: ClassVar[Event[ChangeEvent[int]]] = Event(ChangeEvent)
    style_parts: ClassVar[tuple[str, ...]] = (
        "body", "header", "label", "description", "indicator", "connector", "error",
        "track", "thumb",
    )

    def _initialize(self) -> None:
        super()._initialize()
        self._syncing = self._destroying = False
        self._press = PressBehavior()
        self._hot_step: Step | None = None
        self._armed_step: Step | None = None
        self._pending_action: object | None = None
        self._navigation_revision = 0
        self._step_content_height = 0.0
        self._reveal_selected = True

    @property
    def steps(self) -> tuple[Step, ...]:
        return cast(tuple[Step, ...], self.children)

    @property
    def selected_step(self) -> Step | None:
        self._check_live()
        return self.steps[self.selected_index] if self.steps else None

    def add(self, control: _C) -> _C:
        if not isinstance(control, Step):
            raise TypeError("Add Step children to Stepper, then add controls to each step")
        result = super().add(control)
        self._steps_changed()
        return result

    def on_attached(self) -> None:
        super().on_attached()
        self._navigation_revision += 1

    def _attach_dispatcher(self, dispatcher: Dispatcher | None) -> None:
        if self._dispatcher is not dispatcher:
            # A cancelled task may never enter its coroutine/finally block.
            self._pending_action = None
            self._navigation_revision += 1
        super()._attach_dispatcher(dispatcher)

    def _validate_update(self, name: str, value: object) -> None:
        super()._validate_update(name, value)
        if name == "selected_index" and expect_number(value) >= max(1, len(self.steps)):
            raise ValueError("Selected step index is outside the steps")

    def _validate_live_update(self, name: str, value: object) -> None:
        super()._validate_live_update(name, value)
        if name == "selected_index":
            # Preflight against live identities before an atomic update commits.
            # Calling the visibility hook avoids Step's ownership restriction.
            for index, step in enumerate(self.steps):
                if index != value:
                    step._validate_live_update("visible", False)

    def _clear_interaction(self) -> None:
        self._press.reset(self)
        self._hot_step = self._armed_step = None

    def _changed(self, field: Field, old: object, value: object) -> None:
        self._clear_interaction()
        if field.name == "orientation":
            self._reveal_selected = True
        if field.name in ("linear", "enabled"):
            self._navigation_revision += 1
        if field.name == "selected_index":
            self._navigation_revision += 1
            self._sync_steps()
        super()._changed(field, old, value)

    def _steps_changed(self, selected: Step | None = None) -> None:
        self._clear_interaction()
        self._navigation_revision += 1
        self.selected_index = (self.steps.index(selected) if selected in self.steps
                               else min(self.selected_index, max(0, len(self.steps) - 1)))
        self._sync_steps()

    def _step_state_changed(self, *, navigation: bool = False) -> None:
        self._clear_interaction()
        if navigation:
            self._navigation_revision += 1
        self.invalidate(Dirty.MEASURE | Dirty.PAINT)

    def _sync_steps(self) -> None:
        self._reveal_selected = True
        runtime = getattr(self._root(), "_runtime", None)
        node = runtime.router.focus if runtime is not None else None
        while node is not None and node._parent is not self:
            node = node._parent
        if isinstance(node, Step) and node is not self.selected_step:
            self.focus()
        self._syncing = True
        try:
            for index, step in enumerate(self.steps):
                step.visible = index == self.selected_index
        finally:
            self._syncing = False
        self.invalidate(Dirty.MEASURE | Dirty.HIT_TEST)

    def can_select(self, index: int) -> bool:
        """Whether navigation may enter index; an out-of-range index returns False."""
        self._check_live()
        if type(index) is not int:
            raise TypeError("Step index must be an integer")
        if not self.effective_enabled or not 0 <= index < len(self.steps):
            return False
        step = self.steps[index]
        if not step.enabled:
            return False
        if index == self.selected_index:
            return True
        if step.completed and not step.editable:
            return False
        required = self._requirements(step)
        return required is not None and all(previous.completed and not previous.error for previous in required)

    def _requirements(self, step: Step) -> tuple[Step, ...] | None:
        """Resolve the prerequisite closure without invoking application code."""
        steps = self.steps
        ordered: list[Step] = []
        visiting: set[Step] = set()
        visited: set[Step] = set()

        def visit(current: Step) -> bool:
            if current._disposed or current not in steps or current in visiting:
                return False
            if current in visited:
                return True
            visiting.add(current)
            dependencies = current.requires
            if self.linear:
                dependencies = (*dependencies, *(item for item in steps[:steps.index(current)] if not item.optional))
            for dependency in dependencies:
                if not visit(dependency):
                    return False
            visiting.remove(current)
            visited.add(current)
            if current is not step:
                ordered.append(current)
            return True

        return tuple(ordered) if visit(step) else None

    def select(self, index: int) -> bool:
        """Select a reachable step, returning False when navigation is blocked."""
        if not self.can_select(index):
            return False
        self.selected_index = index
        return True

    def next(self) -> bool:
        """Enter the next reachable step without changing completion flags."""
        self._check_live()
        return self._move(range(self.selected_index + 1, len(self.steps)))

    def previous(self) -> bool:
        """Return to the preceding reachable, editable step."""
        self._check_live()
        return self._move(range(self.selected_index - 1, -1, -1))

    def _move(self, indices: range) -> bool:
        return any(self.select(index) for index in indices)

    def _can_request(self, index: int) -> bool:
        """Header availability does not depend on a stale validation result."""
        if not self.effective_enabled or not 0 <= index < len(self.steps):
            return False
        step = self.steps[index]
        if not step.enabled or (index != self.selected_index and step.completed and not step.editable):
            return False
        required = self._requirements(step)
        return required is not None and all(item.enabled or item.completed for item in required)

    def _request_select(self, index: int) -> None:
        if (index == self.selected_index or self._pending_action is not None
                or not self._can_request(index)):
            return
        if self._dispatcher is None:
            # Detached low-level input has no event loop or owned tasks.
            self.select(index)
            return
        target = self.steps[index]
        required = self._requirements(target)
        if required is None:
            return
        token = self._pending_action = object()
        revision = self._navigation_revision
        try:
            self.create_task(self._select_validated(target, required, token, revision))
        except BaseException:
            if self._pending_action is token:
                self._pending_action = None
            raise

    async def _select_validated(
        self, target: Step, required: tuple[Step, ...], token: object, revision: int
    ) -> None:
        try:
            if not self._validation_is_current(target, token, revision):
                return
            for step in required:
                if not step.enabled and not step.completed:
                    return
                accepted = await step._validate(show_error=True, origin="user")
                if (not self._validation_is_current(target, token, revision)
                        or step._disposed or step._parent is not self):
                    return
                if not accepted:
                    if step.completed and not step.error:
                        return  # A newer successful result superseded this request.
                    previous = self._origin
                    self._origin = "user"
                    try:
                        self._reveal_selected = True
                        self.invalidate(Dirty.ARRANGE | Dirty.PAINT)
                        self.selected_index = self.steps.index(step)
                        if getattr(self._root(), "_runtime", None) is not None:
                            self.focus()
                    finally:
                        self._origin = previous
                    return
            if (any(not step.completed or step.error for step in required)
                    or self._requirements(target) != required):
                return
            previous = self._origin
            self._origin = "user"
            try:
                self.select(self.steps.index(target))
            finally:
                self._origin = previous
        finally:
            if self._pending_action is token:
                self._pending_action = None

    def _validation_is_current(self, target: Step, token: object, revision: int) -> bool:
        return (not self._disposed and self._pending_action is token
                and self._navigation_revision == revision and self.effective_enabled
                and not target._disposed and target._parent is self and target.enabled)

    def reset(self) -> None:
        """Reset selection, feedback and scrolling while retaining field values."""
        self._check_live()
        self._pending_action = None
        self._navigation_revision += 1
        for step in self.steps:
            step._reset_validation()
        self.scroll_x = self.scroll_y = 0
        self.selected_index = 0

    def _header_height(self) -> float:
        # Use at least two terminal rows for the title and optional/error line.
        return max(self.header_height, 2 * self.effective_row_height(0))

    def _active_error_height(self) -> float:
        step = self.selected_step
        return (max(24, self.effective_row_height(0))
                if self.orientation == "horizontal" and step is not None and step.error else 0)

    def measure(self, host: Host) -> tuple[float, float]:
        return self.measure_available(host)

    def measure_available(
        self, host: Host, *, width: float | None = None, height: float | None = None
    ) -> tuple[float, float]:
        from .layout import _measurement_rows, _size

        with _measurement_rows(self, host):
            count = len(self.steps)
            if not count:
                return self.padding * 2, self.padding * 2
            vertical = self.orientation == "vertical"
            indent = 48 if vertical else 0
            inner_width = None if width is None else max(0, width - self.padding * 2 - indent)
            step = self.selected_step
            cw, ch = _size(step, host, width=inner_width) if step is not None else (0, 0)
            return (max(self.step_width * (1 if vertical else count), cw + indent)
                    + self.padding * 2,
                    self._header_height() * (count if vertical else 1) + ch
                    + self.padding * 2 + self._active_error_height())

    def _geometry(self, area: Rect, *, scrolled: bool = True) -> tuple[tuple[Rect, ...], Rect]:
        count = len(self.steps)
        if not count:
            return (), Rect(area.x, area.y, 0, 0)
        vertical = self.orientation == "vertical"
        hh = self._header_height()
        error_height = self._active_error_height()
        content_height = max(self._step_content_height,
                             area.height - hh * (count if vertical else 1) - error_height)
        if scrolled:
            area = Rect(area.x - self.scroll_x, area.y - self.scroll_y, area.width, area.height)
        if vertical:
            headers = tuple(Rect(area.x, area.y + index * hh + (
                content_height if index > self.selected_index else 0
            ), area.width, hh) for index in range(count))
            indent = min(48, area.width)
            content = Rect(area.x + indent, headers[self.selected_index].bottom,
                           max(0, area.width - indent), content_height)
        else:
            pitch = area.width / count
            headers = tuple(Rect(area.x + index * pitch, area.y, pitch, hh)
                            for index in range(count))
            content = Rect(area.x, area.y + hh + error_height, area.width,
                           content_height)
        return headers, content

    def arrange_children(
        self, area: Rect, measure: Callable[[Control], tuple[float, float]]
    ) -> dict[Control, Rect] | None:
        step = self.selected_step
        self._scroll_axes = (False, False)
        if step is None:
            self._step_content_height = 0
            self._scroll_limits = (0, 0)
            self.scroll_x = self.scroll_y = 0
            return {}
        vertical = self.orientation == "vertical"
        while True:
            area = self.content_bounds(self._rect)
            width = max(0, area.width - (min(48, area.width) if vertical else 0))
            height = cast(Callable[..., tuple[float, float]], measure)(step, width=width)[1]
            # Vertical navigation is one scrolling sequence. Horizontal content
            # has its own viewport; retain one usable row even in a short window.
            self._step_content_height = height if vertical else min(height, self._header_height())
            headers, content = self._geometry(area, scrolled=False)
            bottom = max(content.bottom, headers[-1].bottom)
            self._scroll_limits = (0, max(0, bottom - area.bottom))
            if self._scroll_axes[1] or self._scroll_limits[1] <= 0:
                break
            self._scroll_axes = (False, True)
        self.scroll_x = 0
        self.scroll_y = min(self.scroll_y, self._scroll_limits[1])
        if self._reveal_selected:
            self._reveal_selected = False
            self.scroll_y = min(self._scroll_limits[1], max(0, headers[self.selected_index].y - area.y))
        return {step: content}

    def _hit_step(self, event: Input) -> Step | None:
        if not self._rect.contains(event.x, event.y):
            return None
        headers, _ = self._geometry(self.content_bounds(self._rect))
        return next((step for step, header in zip(self.steps, headers)
                     if header.contains(event.x, event.y)), None)

    def handle_input(self, event: Input, /) -> bool | None:
        self._check_live()
        if not self.effective_enabled:
            self._scroll_drag = None
        else:
            if event.kind == "wheel":
                ScrollArea.handle_input(self, event)
                return
            handled, self._scroll_drag, values = handle_scrollbars(
                event, self._scrollbars(), self._scroll_drag)
            for axis, value in values.items():
                setattr(self, "scroll_x" if axis == 0 else "scroll_y", value)
            if handled:
                self._clear_interaction()
                return
        if not self.effective_enabled or event.kind == "blur":
            self._clear_interaction()
            return
        step = self._hit_step(event) if event.kind.startswith("pointer") else self.selected_step
        if step is not None and not self._can_request(self.steps.index(step)):
            step = None
        if event.kind in ("pointer_move", "pointer_down", "pointer_up", "pointer_leave"):
            if self._hot_step is not step:
                self._hot_step = step
                self.invalidate()
        if event.kind == "key_down":
            backward, forward = (("ArrowUp", "ArrowDown") if self.orientation == "vertical"
                                 else ("ArrowLeft", "ArrowRight"))
            if event.key in (backward, forward, "Home", "End"):
                self._clear_interaction()
                indices = (range(len(self.steps)) if event.key == "Home"
                           else range(len(self.steps) - 1, -1, -1) if event.key == "End"
                           else range(self.selected_index - 1, -1, -1) if event.key == backward
                           else range(self.selected_index + 1, len(self.steps)))
                for index in indices:
                    if self._can_request(index):
                        self._request_select(index)
                        break
                return True
        if event.kind in ("pointer_down", "key_down") and step is None:
            return
        was_pressed = self._pressed
        activated = self._press.handle_input(self, event, part=step)
        if self._pressed and not was_pressed:
            self._armed_step = step
        if activated and step is not None:
            self._request_select(self.steps.index(step))
            if event.kind == "pointer_up":
                self._hot_step = step

    def _header_state(self, index: int) -> StyleState:
        step = self.steps[index]
        return ("disabled" if not self._can_request(index)
                else "pressed" if self._pressed and self._armed_step is step
                else "hover" if self._hover and self._hot_step is step else "normal")

    @staticmethod
    def _text(
        p: Painter, text: str, area: Rect, style: ResolvedStyle, *, color: str | None = None
    ) -> float:
        if area.width <= 0 or area.height <= 0:
            return 0
        with p._clipped(area):
            caption = p.elide(single_line(text), area.width, size=style.font_size,
                              font_family=style.font_family)
            p.text(caption, area.x, area.y, color=color or style.foreground,
                   size=style.font_size, font_family=style.font_family)
            return p.measure(caption, size=style.font_size, font_family=style.font_family)[0]

    def paint(self, p: Painter, /) -> None:
        p.body()
        area = self.content_bounds(Rect(0, 0, p.width, p.height))
        headers, content = self._geometry(area)
        vertical = self.orientation == "vertical"
        with p._clipped(area):
            states: list[StyleState] = [self._header_state(index) for index in range(len(headers))]
            label_ends = [self._paint_header(p, index, header, states[index])
                          for index, header in enumerate(headers)]
            step = self.selected_step
            if not vertical and step is not None and step.error:
                error = resolve_style(self, "error", state=states[self.selected_index])
                self._text(p, step.error, Rect(
                    area.x + 16, headers[0].bottom, max(0, area.width - 32),
                    max(0, content.y - headers[0].bottom),
                ), error)
            for index, header in enumerate(headers):
                if index + 1 < len(headers):
                    following = headers[index + 1]
                    connector = p.style("connector", selected=self.steps[index].completed)
                    if vertical:
                        x = header.x + min(28, header.width / 2)
                        top, bottom = header.y + header.height / 2 + 20, following.y + following.height / 2 - 20
                        if bottom > top:
                            p.line(x, top, x, bottom, connector.foreground)
                    else:
                        y = header.y + header.height / 2
                        left = label_ends[index] + 12
                        right = following.x + min(8, following.width / 4)
                        if right > left:
                            p.line(left, y, right, y, connector.foreground)
        paint_scrollbars(p, self._scrollbars())

    def _paint_header(self, p: Painter, index: int, area: Rect, state: StyleState) -> float:
        if area.width <= 0 or area.height <= 0:
            return area.right
        step = self.steps[index]
        active = index == self.selected_index
        with p._clipped(area):
            p.surface(area, resolve_style(self, "header", selected=active, state=state))
            size = min(24, area.width, area.height)
            x = area.x + min(16, max(0, (area.width - size) / 2))
            y = area.y + (area.height - size) / 2
            indicator = resolve_style(self, "indicator", selected=active or step.completed,
                                      state=state)
            error = resolve_style(self, "error", state=state)
            fill = error.fill if step.error else indicator.fill
            ink = (_readable_ink(indicator.foreground, fill,
                                 _readable_ink("#ffffff", fill, "#000000"))
                   if step.error else indicator.foreground)
            p.rect(Rect(x, y, size, size), fill, radius=size / 2)
            if step.completed and not step.error:
                p.icon("check", x + size / 6, y + size / 6, size=size * 2 / 3,
                       color=ink)
            else:
                number = "!" if step.error else str(index + 1)
                tw, th = p.measure(number, size=indicator.font_size,
                                   font_family=indicator.font_family)
                p.text(number, x + (size - tw) / 2, y + (size - th) / 2,
                       color=ink, size=indicator.font_size,
                       font_family=indicator.font_family)
            label = resolve_style(self, "label", selected=active, state=state)
            detail = step.error or ("Optional" if step.optional else "")
            if active and step.error and self.orientation == "horizontal":
                detail = ""
            description = error if step.error else resolve_style(self, "description", state=state)
            lh = p.measure("M", size=label.font_size, font_family=label.font_family)[1]
            dh = p.measure("M", size=description.font_size,
                           font_family=description.font_family)[1] if detail else 0
            top = area.y + max(0, (area.height - lh - (dh + 4 if detail else 0)) / 2)
            left = x + size + 8
            width = max(0, area.right - left - (24 if self.orientation == "horizontal" else 8))
            used = self._text(p, step.title, Rect(left, top, width, lh), label,
                              color=error.foreground if step.error else label.foreground)
            if detail:
                used = max(used, self._text(p, detail, Rect(left, top + lh + 4, width, dh), description))
            if self.focused and active and area.width > 8 and area.height > 4:
                p.line(area.x + 4, area.bottom - 2, area.right - 4, area.bottom - 2,
                       indicator.fill, 2)
            return left + used

    def destroy(self) -> None:
        self._destroying = True
        self._pending_action = None
        self._clear_interaction()
        super().destroy()


__all__ = ["Step", "Stepper"]
