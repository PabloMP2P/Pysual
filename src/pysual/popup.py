"""One transient, app-owned control subtree; no native popup implementation."""

from typing import ClassVar
from .controls import Container, Control
from .errors import LifecycleError
from .schema import Dirty, prop


class Popup(Container):
    cache_paint: bool = prop(default=True)
    dismiss_on_tab: ClassVar[bool] = True
    visible: bool = prop(default=False, affects=Dirty.MEASURE | Dirty.HIT_TEST)
    _overlay: ClassVar[bool] = True

    def _initialize(self):
        super()._initialize()
        self._session = None
        self._owner: Control | None = None
        self._previous_focus = None
        self._point = None

    @property
    def is_open(self) -> bool:
        return self._session is not None

    def contains(self, control) -> bool:
        while control is not None:
            if control is self:
                return True
            control = control.parent
        return False

    def show(self, anchor: Control, *, at: tuple[float, float] | None = None) -> None:
        """Open once, below an anchor or at an app-logical point; close disposes it."""
        self._check_live()
        anchor._check_live()
        runtime = getattr(anchor._root(), "_runtime", None)
        if runtime is None or not runtime.app.is_open:
            raise LifecycleError("Popups require a running App")
        if (self.parent is not None and self.parent is not runtime.app) or self.is_open:
            raise LifecycleError("Show a fresh Popup created directly or on the App")
        if not runtime.router._visible(anchor):
            raise LifecycleError("The popup anchor must be visible and enabled")
        if not runtime._modality.allowed(anchor):
            raise LifecycleError("The popup anchor must be inside the active modal dialog")
        if runtime.popup is not None:
            if runtime.popup.contains(anchor):
                raise LifecycleError(
                    "Nested popups are unsupported; replace the popup content"
                )
            runtime.popup.dismiss()
        self._owner, self._point = anchor, at
        self._previous_focus = runtime.router.focus
        self.theme_override = anchor.effective_theme
        if self.parent is None:
            runtime.app.add(self)
        self.visible = True
        self._session = runtime
        runtime.popup = self
        runtime.router.cancel_capture(clear_focus=True)
        self.reposition()
        runtime.router.set_focus(None)
        runtime.router._tab()

    def reposition(self):
        runtime = self._session
        if runtime is None:
            return
        assert self._owner is not None
        children = runtime.app._children
        if children[-1] is not self:
            children.remove(self)
            children.append(self)
        area = runtime.app.content_bounds(runtime.app.bounds)
        width, height = self._measure_layout(runtime.host)
        anchor = self._owner.bounds
        x, y = self._point if self._point is not None else (anchor.x, anchor.bottom)
        if self._point is None and y + height > area.bottom:
            y = anchor.y - height
        self.left = max(area.x, min(x, area.right - width)) - area.x
        self.top = max(area.y, min(y, area.bottom - height)) - area.y

    def _layout_size(self, width, height):
        if self._session is not None:
            area = self._session.app.content_bounds(self._session.app.bounds)
            return min(area.width, width), min(area.height, height)
        return width, height

    def reconcile(self):
        if self._session is None:
            return
        assert self._owner is not None
        if (
            not self.visible
            or not self.enabled
            or self._owner._disposed
            or self._owner._root() is not self._session.app
            or not self._session.router._visible(self._owner)
            or not self._session._modality.allowed(self._owner)
            or self._owner._clip.width <= 0
            or self._owner._clip.height <= 0
        ):
            self.dismiss()

    def paint(self, painter, /):
        painter.body()

    def dismiss(self, *, restore_focus: bool = True) -> None:
        runtime = self._session
        if runtime is None:
            return
        self._session = None
        runtime.popup = None
        runtime.router.cancel_capture(clear_focus=True)
        previous = self._previous_focus
        self._previous_focus = None
        super().destroy()
        runtime.router.set_focus(
            previous
            if restore_focus
            and previous is not None
            and not previous._disposed
            and runtime.router._visible(previous)
            else None,
            reveal=False,
        )
        runtime.invalidate()

    def destroy(self) -> None:
        if self.is_open:
            self.dismiss(restore_focus=False)
        else:
            super().destroy()
