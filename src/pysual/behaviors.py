"""Reusable interaction components for controls with independent presentation.

Create stateful behaviors per control in ``_initialize`` and delegate from the
ordinary control hooks. Behaviors retain no control, callbacks, or resources;
the control owns event emission, validation, painting, and lifetime.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ._engine import check_ui_thread
from .host import Input

if TYPE_CHECKING:
    from .controls import Control


class PressBehavior:
    """Recognize a primary-pointer or keyboard press followed by release.

    ``handle_input`` returns an activation request after clearing the gesture,
    so callers can safely open a popup, destroy themselves, or emit an event.
    Only the initiating pointer and hit part can complete a pointer gesture.
    Set ``keys=()`` when the control handles keyboard commands separately.

    Call from the owning control's hooks, which run on its UI thread. Keep one
    instance per control and call ``reset`` in the control's ``destroy`` hook.
    """

    def __init__(self, *, keys: tuple[str, ...] = ("Enter", "Space")) -> None:
        self._keys = tuple(keys)
        self._pointer: int | None = None
        self._part: object = None
        self._pointer_pressed = False
        self._key: str | None = None

    def _sync_pressed(self, owner: Control) -> None:
        pressed = self._pointer_pressed or self._key is not None
        if owner._pressed != pressed:
            owner._pressed = pressed
            if not owner._disposed:
                owner.invalidate()

    def reset(self, owner: Control, /) -> None:
        """Cancel the gesture and clear its visual state; safe during destroy."""
        check_ui_thread()
        self._pointer = None
        self._part = None
        self._pointer_pressed = False
        self._key = None
        self._sync_pressed(owner)

    def handle_input(
        self, owner: Control, event: Input, /, *, part: object = None
    ) -> bool:
        """Update pressed state and return whether to invoke the owner's action.

        ``part`` identifies the hit region at the event position, for example
        minus, value, or plus in a numeric editor. It must compare equal on
        press and release. Hit testing uses the owner's current bounds.
        """
        owner._check_live()
        if event.kind == "blur":
            self.reset(owner)
            return False
        if event.kind == "pointer_cancel":
            if self._pointer is None or event.pointer_id == self._pointer:
                self.reset(owner)
            return False

        activate = False
        inside = owner._rect.contains(event.x, event.y)
        if event.kind == "pointer_down":
            if event.button == 1 and inside and self._pointer is None:
                self._pointer = event.pointer_id
                self._part = part
                self._pointer_pressed = True
        elif event.kind == "pointer_move" and event.pointer_id == self._pointer:
            self._pointer_pressed = inside and part == self._part
        elif event.kind == "pointer_up" and event.pointer_id == self._pointer:
            if event.button == 1:
                activate = inside and part == self._part
                self._pointer = None
                self._part = None
                self._pointer_pressed = False
        elif event.kind == "key_down" and event.key in self._keys:
            if self._key is None:
                self._key = event.key
        elif event.kind == "key_up" and event.key == self._key:
            self._key = None
            activate = True
        self._sync_pressed(owner)
        return activate
