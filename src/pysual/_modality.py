"""In-app presentation ownership and input scope, independent of caller waits."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, cast

from ._engine import _origin, capture_origin, critical_phase, ensure_presentable
from .errors import LifecycleError
from .presentation import Presentation

if TYPE_CHECKING:
    from .controls import Control
    from .runtime import Runtime
    from .widgets import SubWindow


class _Completable(Protocol):
    def done(self) -> bool: ...


def _pending(value: object) -> _Completable | None:
    if isinstance(value, asyncio.Future):
        return cast(_Completable, value)
    return None


def contains(parent: Control, control: Control | None) -> bool:
    """Identity-based physical ancestry without consulting public bindings."""
    while control is not None:
        if control is parent:
            return True
        control = control._parent
    return False


@dataclass
class _Opening:
    control: SubWindow
    presentation: Presentation
    owner: Control
    previous_focus: Control | None = None


class ModalManager:
    def __init__(self, runtime: Runtime):
        self.runtime = runtime
        self._openings: list[_Opening] = []

    @property
    def active(self) -> SubWindow | None:
        return next(
            (
                entry.control
                for entry in reversed(self._openings)
                if entry.presentation.modal
            ),
            None,
        )

    def _entry(self, control: Control | None) -> _Opening | None:
        return next(
            (entry for entry in self._openings if entry.control is control), None
        )

    def _guard(self) -> None:
        ensure_presentable()
        if self.runtime._phase is not None:
            raise LifecycleError(
                "Change presentations from a scheduled event handler, not a "
                "build, layout, paint, validation or input traversal hook"
            )

    def allowed(self, control: Control) -> bool:
        if self.runtime._native_blockers:
            return False
        active = self.active
        if active is None:
            return True
        if contains(active, control):
            return True
        popup = self.runtime.popup
        return bool(
            popup is not None
            and popup.contains(control)
            and contains(active, popup._owner)
        )

    def _owned_by(self, entry: _Opening, owner: Control) -> bool:
        seen: set[int] = set()
        current: Control | None = entry.control
        while current is not None and id(current) not in seen:
            if contains(owner, current):
                return True
            seen.add(id(current))
            current_entry = self._entry(current)
            current = (
                current_entry.owner if current_entry is not None else current._parent
            )
        return False

    def validate_visibility(self, control: Control, value: bool) -> None:
        if value:
            return
        for entry in self._openings:
            if entry.presentation.modal and self._owned_by(entry, control):
                raise LifecycleError(
                    "Close an active modal dialog before hiding it or its owner"
                )

    def show(
        self, control: SubWindow, *, modal: bool = False, owner: Control | None = None
    ) -> Presentation:
        self._guard()
        with critical_phase("presentation transition"):
            return self._show(control, modal=modal, owner=owner)

    def _show(
        self, control: SubWindow, *, modal: bool = False, owner: Control | None = None
    ) -> Presentation:
        control._check_live()
        runtime = self.runtime
        if control._root() is not runtime.app or not runtime.app.is_open:
            raise LifecycleError("In-app windows require a running owner Window")
        if control._presentation_error is not None:
            raise LifecycleError("This window's previous presentation did not clean up")
        existing = self._entry(control)
        if existing is not None:
            if existing.presentation.modal != modal:
                raise LifecycleError(
                    "The window is already open with a different modal mode"
                )
            if owner is not None and owner is not existing.owner:
                raise LifecycleError("An open window cannot change its owner")
            control.visible = True
            runtime.invalidate()
            runtime._paint_frame()
            return existing.presentation

        # A nested modal created on the native root defaults to the active
        # modal owner. Explicit owners are validated before any scope changes.
        if owner is None:
            owner = (
                self.active if modal and self.active is not None else control._parent
            )
        if owner is None or owner._root() is not runtime.app:
            raise LifecycleError("The owner must belong to the same running Window")
        owner._check_live()
        if owner is control or contains(control, owner):
            raise LifecycleError("Window ownership must not contain a cycle")
        current: Control | None = owner
        seen: set[int] = set()
        while current is not None:
            if current is control or id(current) in seen:
                raise LifecycleError("Window ownership must not contain a cycle")
            seen.add(id(current))
            current_entry = self._entry(current)
            current = (
                current_entry.owner if current_entry is not None else current._parent
            )
        if not runtime.router._visible(owner):
            raise LifecycleError("The owner must be visible and enabled")
        if modal and not self.allowed(owner):
            active_entry = self._entry(self.active)
            pending_close = _pending(getattr(owner, "_close_pending", None))
            if owner is runtime.app:
                pending_close = runtime._close_task
            confirming_owner_close = (
                pending_close is not None
                and not pending_close.done()
                and active_entry is not None
                and self._owned_by(active_entry, owner)
                and not runtime._native_blockers
            )
            if not confirming_owner_close:
                raise LifecycleError("The owner must be inside the active modal dialog")
        # Physical ancestors also determine whether this subtree can be shown.
        if control._parent is None or not runtime.router._visible(control._parent):
            raise LifecycleError(
                "The dialog's containing tree must be visible and enabled"
            )
        popup = runtime.popup
        if modal and popup is not None and contains(popup, control):
            raise LifecycleError("Mount modal dialogs outside the transient popup tree")

        router = runtime.router
        previous = router.focus
        if modal:
            if popup is not None:
                popup.dismiss(restore_focus=False)
            router.cancel_capture(clear_focus=True)
            router.set_focus(None)
            router._set_hover(None)
        control._generation += 1
        presentation = Presentation(control._generation, modal=modal, owner=owner)
        entry = _Opening(control, presentation, owner, previous)
        control._presentation = presentation
        control._close_pending = None
        self._openings.append(entry)
        # The opening transition may originate in the closing presentation's
        # callback. Permit framework setup of the new identity, then restore
        # the caller's old identity so its later source mutations still fail.
        origin_token = _origin.set(
            tuple(origin for origin in _origin.get() if origin[0] is not control)
            + capture_origin(control)
        )
        try:
            control.visible = True
            if modal:
                router._tab()
            runtime.invalidate()
            runtime._paint_frame()
        except BaseException as error:
            self._finish_entries([entry], None, "failed", error)
            raise
        finally:
            _origin.reset(origin_token)
        return presentation

    def finish(
        self, control: Control, result: object = None, *, reason: str = "closed"
    ) -> None:
        self._guard()
        with critical_phase("presentation transition"):
            return self._finish(control, result, reason=reason)

    def _finish(
        self, control: Control, result: object = None, *, reason: str = "closed"
    ) -> None:
        entries = [entry for entry in self._openings if self._owned_by(entry, control)]
        self._finish_entries(entries, result, reason, target=control)
        if not entries and not control._disposed:
            control.visible = False

    def destroying(self, control: Control) -> None:
        entries = [entry for entry in self._openings if self._owned_by(entry, control)]
        if entries:
            self._guard()
            with critical_phase("presentation transition"):
                self._finish_entries(entries, None, "destroyed", target=control)

    def shutdown(self, error: BaseException | None = None) -> None:
        with critical_phase("presentation transition"):
            self._finish_entries(list(self._openings), None, "owner_closed", error)

    def _finish_entries(
        self,
        entries: list[_Opening],
        result: object,
        reason: str,
        error: BaseException | None = None,
        *,
        target: Control | None = None,
    ) -> None:
        if not entries:
            return
        runtime = self.runtime
        previous = entries[0].previous_focus
        # Remove every owned scope together before hiding anything, so the
        # public visibility guard cannot leave an invisible live modal behind.
        self._openings[:] = [entry for entry in self._openings if entry not in entries]
        cleanup_failure = None
        try:
            popup = runtime.popup
            router = runtime.router
            if popup is not None and any(
                contains(entry.control, popup._owner) for entry in entries
            ):
                popup.dismiss(restore_focus=False)
            router.cancel_capture(clear_focus=True)
            router.set_focus(None)
            router._set_hover(None)
            for entry in reversed(entries):
                if not entry.control._disposed:
                    entry.control.visible = False
                    entry.control._close_armed = entry.control._drag = None
            if (
                reason == "closed"
                and target is not None
                and not target._disposed
                and not any(entry.control is target for entry in entries)
            ):
                target.visible = False
            if (
                runtime.app.is_open
                and previous is not None
                and not previous._disposed
                and router._visible(previous)
                and self.allowed(previous)
            ):
                router.set_focus(previous)
            elif runtime.app.is_open:
                router._tab()
            runtime.invalidate()
        except BaseException as cleanup_error:
            error = cleanup_error
            cleanup_failure = cleanup_error
        for entry in reversed(entries):
            if error is not None:
                if cleanup_failure is not None:
                    entry.control._presentation_error = cleanup_failure
                entry.presentation.fail(error)
            else:
                owned = target is not None and entry.control is not target
                entry.presentation.complete(
                    None if owned else result,
                    reason="owner_closed" if owned and reason == "closed" else reason,
                )
        if cleanup_failure is not None and reason != "failed":
            raise cleanup_failure

    def paint_order(self, children: Sequence[Control]) -> list[Control]:
        modals: list[Control] = [entry.control for entry in self._openings if entry.presentation.modal]
        popup = self.runtime.popup

        if popup is not None:
            modals.append(popup)
        ranks: dict[Control, int] = {}
        # Later openings replace earlier ranks on their shared ancestors.
        for rank, control in enumerate(modals, 1):
            node: Control | None = control
            while node is not None:
                ranks[node] = rank
                node = node._parent
        return sorted(children, key=lambda child: (ranks.get(child, 0), child._overlay))
