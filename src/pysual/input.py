"""Focus, hit testing and pointer capture use layout's computed rectangles."""

from inspect import isawaitable, iscoroutine
from math import ceil, floor, sqrt

from .commands import normalize_shortcut
from .errors import LifecycleError
from .events import DragEvent, DragPayload, KeyEvent, PointerEvent, UiEvent
from .host import CapabilityError, Input
from .motion import interaction_state
from .schema import Dirty


class _HitIndex:
    """Spatial candidates in the same order as the visible paint tree."""

    def __init__(self, root, modality, *, excluded=None, inherited_clip=None):
        records = []
        self.excluded = excluded
        self.metadata = {}

        def state(control):
            return (
                control._clip, control._values["visible"],
                tuple(child for child in getattr(control, "_children", ())
                      if child is not excluded),
            )

        def visit(control, inherited_clip=None):
            if control is excluded or control._disposed:
                return
            metadata = self.metadata[control] = state(control)
            if not metadata[1]:
                return
            clip = control._clip
            if inherited_clip is not None:
                clip = clip.intersect(inherited_clip)
            if clip.width <= 0 or clip.height <= 0:
                return
            records.append((len(records), control, clip))
            children = metadata[2]
            if not children:
                return
            ordered = (
                modality.paint_order(children) if modality is not None
                else sorted(children, key=lambda child: child._overlay)
            )
            for child in ordered:
                visit(child, clip)

        visit(root, inherited_clip)
        # Increase spatial resolution with control density. Large containers
        # stay in one shared list instead of filling every cell in the window.
        area = max(1, root._clip.width * root._clip.height)
        self.cell_size = max(4, min(64, sqrt(area / max(1, len(records)))))
        self.buckets, self.large = {}, []
        for record in records:
            clip = record[2]
            x0, y0 = floor(clip.x / self.cell_size), floor(clip.y / self.cell_size)
            x1, y1 = ceil(clip.right / self.cell_size), ceil(clip.bottom / self.cell_size)
            if (x1 - x0) * (y1 - y0) > 64:
                self.large.append(record)
                continue
            for cell_y in range(y0, y1):
                for cell_x in range(x0, x1):
                    self.buckets.setdefault((cell_x, cell_y), []).append(record)

    def changed(self, control, popup):
        previous = self.metadata.get(control)
        if previous is None:
            return False
        if control._disposed:
            return True
        children = tuple(child for child in getattr(control, "_children", ())
                         if child is not self.excluded and child is not popup)
        return previous != (control._clip, control._values["visible"], children)

    def candidates(self, x, y):
        local = self.buckets.get((floor(x / self.cell_size), floor(y / self.cell_size)), ())
        local_index, large_index = len(local) - 1, len(self.large) - 1
        while local_index >= 0 or large_index >= 0:
            if large_index < 0 or (
                local_index >= 0 and local[local_index][0] > self.large[large_index][0]
            ):
                record = local[local_index]
                local_index -= 1
            else:
                record = self.large[large_index]
                large_index -= 1
            if record[2].contains(x, y):
                yield record[1]


class Router:
    def __init__(self, runtime):
        self.runtime = runtime
        self.focus = None
        self.capture = None
        self.capture_id = None
        self.hover = None
        self._focus_revision = 0
        self._focus_to_reveal = None
        self.drag_source = None
        self.drag_payload: DragPayload[object] | None = None
        self.drop_target = None
        self._pointer = Input("pointer_move")
        self._hit_revision = 0
        self._hit_index_revision = -1
        self._hit_index = None
        self._hit_order = None
        self._hit_dirty = set()
        self._popup_hit_index = None
        self._popup_hit_scope = None
        self._popup_hit_clip = None

    def clear(self):
        """Release input geometry and control references after session shutdown."""
        self._hit_dirty.clear()
        self._hit_index = self._popup_hit_index = None
        self._popup_hit_scope = self._popup_hit_clip = self._hit_order = None
        self._hit_index_revision = -1
        self._hit_revision += 1
        self.focus = self.capture = self.hover = self._focus_to_reveal = None
        self.capture_id = None
        self.drag_source = self.drag_payload = self.drop_target = None
        self._focus_revision += 1

    def invalidate(self, affects, control=None):
        """Invalidate geometry/policy metadata for the next layout pass."""
        if affects & (Dirty.MEASURE | Dirty.ARRANGE | Dirty.HIT_TEST):
            self._hit_revision += 1
            if control is None:
                self._hit_index = self._popup_hit_index = None
            else:
                self._hit_dirty.add(control)

    def layout_control(self, control):
        """Observe controls whose layout cache was updated in this pass."""
        self._hit_dirty.add(control)

    def layout_changed(self, *, observed=False):
        if not observed:
            # Private callers may replace computed clips directly. The normal
            # arranger reports only the controls whose geometry it visited.
            self._hit_index = self._popup_hit_index = None
        app = getattr(self.runtime, "app", None)
        if app is not None and getattr(app, "_runtime", None) is self.runtime:
            # Prepare geometry with layout, so the first pointer event over a
            # newly arranged heavy scene does not have to build its hit index.
            self._refresh_hit_index(app)

    def _refresh_hit_index(self, app):
        modality = getattr(self.runtime, "_modality", None)
        popup = getattr(self.runtime, "popup", None)
        order = tuple(entry.control for entry in getattr(modality, "_openings", ()))
        dirty, self._hit_dirty = self._hit_dirty, set()
        if (self._hit_index is None or self._hit_order != order
                or any(self._hit_index.changed(control, popup) for control in dirty)):
            self._hit_index = _HitIndex(app, modality, excluded=popup)
        self._hit_index_revision = self._hit_revision
        self._hit_order = order
        if popup is None:
            self._popup_hit_index = self._popup_hit_scope = self._popup_hit_clip = None
            return
        # Popup input is isolated from the rest of the app. Retain the main
        # index while transient overlays are inserted, moved and dismissed.
        clip = popup._clip
        parent = popup._parent
        while parent is not None:
            clip = clip.intersect(parent._clip)
            parent = parent._parent
        if (self._popup_hit_scope is not popup or self._popup_hit_index is None
                or self._popup_hit_clip != clip
                or any(self._popup_hit_index.changed(control, None) for control in dirty)):
            self._popup_hit_index = _HitIndex(popup, modality, inherited_clip=clip)
            self._popup_hit_scope, self._popup_hit_clip = popup, clip

    def _set_hover(self, control):
        previous = self.hover
        if previous is control:
            return
        self.hover = control
        for node, hovered in ((previous, False), (control, True)):
            if node is None or node._disposed:
                continue
            before = interaction_state(node)
            node._hover = hovered
            self.runtime.motion.changed(node, before)
            # Both sides must invalidate their cached bodies, including when
            # the new hit is empty or the pointer leaves the window.
            node.invalidate()

    def begin_drag(
        self, source, payload: DragPayload[object], *, pointer_id: int | None = None
    ) -> None:
        """Begin from a currently captured pointer; one gesture belongs to this Router."""
        source._check_live()
        if not isinstance(payload, DragPayload):
            raise TypeError("Use a DragPayload record")
        if self.drag_source is not None:
            raise LifecycleError("A drag is already active")
        if source._root() is not self.runtime.app or not self._visible(source):
            raise LifecycleError("The drag source must belong to the active App")
        if not self.runtime._modality.allowed(source):
            raise LifecycleError("The drag source must remain inside the modal dialog")
        node = self.capture
        while node is not None and node is not source:
            node = node._parent
        if node is None or (pointer_id is not None and pointer_id != self.capture_id):
            raise LifecycleError("Begin a drag from this control's captured pointer")
        if self.runtime.popup is not None:
            raise LifecycleError("Dismiss the popup before beginning a drag")
        if self.capture is not None:
            self.capture._pressed = False
            self.capture.invalidate()
        self.capture = source
        self.drag_source, self.drag_payload = source, payload
        try:
            self._update_drop_target(self._pointer)
        except BaseException:
            self.cancel_drag()
            raise

    def _drag_event(
        self, owner, name, event, *, target=None, accepted=False, cancelled=False
    ):
        if owner is not None and not owner._disposed:
            assert self.drag_payload is not None
            getattr(owner, name).emit(
                DragEvent(
                    source=owner,
                    origin="user",
                    payload=self.drag_payload,
                    drag_source=self.drag_source,
                    target=target,
                    x=event.x,
                    y=event.y,
                    pointer_id=event.pointer_id,
                    pointer_kind=event.pointer_kind,
                    accepted=accepted,
                    cancelled=cancelled,
                )
            )

    def _update_drop_target(self, event):
        scope = self.runtime.modal or self.runtime.app
        target = self.hit(scope, event.x, event.y)
        while target is not None:
            accepted = target.accepts_drop(self.drag_payload)
            if self.drag_source is None:
                if iscoroutine(accepted):
                    accepted.close()
                return
            if isawaitable(accepted):
                if iscoroutine(accepted):
                    accepted.close()
                self.cancel_drag()
                raise TypeError("accepts_drop must return bool synchronously")
            if type(accepted) is not bool:
                self.cancel_drag()
                raise TypeError("accepts_drop must return bool synchronously")
            if (
                accepted
                and self._visible(target)
                and target._root() is self.runtime.app
            ):
                break
            target = None if target is scope else target._parent
        previous = self.drop_target
        self.drop_target = target
        if previous is not target:
            self._drag_event(previous, "drag_leave", event, target=previous)
            if previous is not None and not previous._disposed:
                previous.invalidate()
            self._drag_event(target, "drag_enter", event, target=target, accepted=True)
        self._drag_event(target, "drag_over", event, target=target, accepted=True)
        if target is not None:
            target.invalidate()
        self.runtime.invalidate()

    def cancel_drag(self) -> None:
        """Cancel capture and send terminal notifications to surviving owners."""
        self._finish_drag(cancelled=True)

    def _finish_drag(self, *, cancelled, reset_source=True):
        if self.drag_source is None:
            return
        source, target = self.drag_source, self.drop_target
        accepted = target is not None and not cancelled
        try:
            if accepted:
                self._drag_event(
                    target, "drop", self._pointer, target=target, accepted=True
                )
            self._drag_event(
                target,
                "drag_leave",
                self._pointer,
                target=target,
                accepted=accepted,
                cancelled=cancelled,
            )
            self._drag_event(
                source,
                "drag_end",
                self._pointer,
                target=target,
                accepted=accepted,
                cancelled=cancelled,
            )
        finally:
            self.drag_source = self.drag_payload = self.drop_target = None
            self.capture = self.capture_id = None
            for control in (source, target):
                if control is not None and not control._disposed:
                    if control is source and reset_source:
                        control.handle_input(Input("blur"))
                        if control._disposed:
                            continue
                    control._pressed = False
                    control.invalidate()
            self.runtime.invalidate()

    def _visible(self, c):
        while c is not None:
            if c._disposed or not c._values["visible"] or not c._committed_enabled():
                return False
            c = c._parent
        return True

    def hit(self, c, x, y):
        # Check geometry before inherited public policies, even for standalone
        # routers without a runtime-owned index.
        if not c._clip.contains(x, y) or not self._visible(c):
            return None
        app = getattr(self.runtime, "app", None)
        if app is not None and getattr(app, "_runtime", None) is self.runtime:
            root = c
            while root._parent is not None:
                root = root._parent
            if root is app:
                self._refresh_hit_index(app)
                scopes = (self._popup_hit_index, self._hit_index)
                for candidate in (
                    candidate for index in scopes if index is not None
                    for candidate in index.candidates(x, y)
                ):
                    if c is not app:
                        node = candidate
                        while node is not None and node is not c:
                            node = node._parent
                        if node is None:
                            continue
                    if self._visible(candidate):
                        return candidate
                return None
        return self._hit_direct(c, x, y)

    def _hit_direct(self, c, x, y):
        for child in reversed(
            sorted(getattr(c, "_children", ()), key=lambda c: c._overlay)
        ):
            hit = self.hit(child, x, y)
            if hit is not None:
                return hit
        return c

    def set_focus(self, c, *, reveal=True):
        if c is not None and (
            not c._values["focusable"] or not self._visible(c)
            or not self.runtime._modality.allowed(c)
        ):
            return
        popup = self.runtime.popup
        if popup is not None and c is not None and not popup.contains(c):
            popup.dismiss(restore_focus=False)
        previous = self.focus
        self.focus = c
        self._focus_to_reveal = c if reveal else None
        if previous is not c:
            self._focus_revision += 1
            revision = self._focus_revision
            if previous is not None and getattr(previous, "_text_input", False):
                self.runtime.host.text_input(None)
            if previous is not None and not previous._disposed:
                before = interaction_state(previous)
                previous.handle_input(Input("blur"))
                if not previous._disposed:
                    self.runtime.motion.changed(previous, before)
                    if getattr(previous, "_text_input", False):
                        previous.invalidate()
            if self._focus_revision != revision:
                return
            if c is not None and not c._disposed:
                c.handle_input(Input("focus"))
                if self._focus_revision != revision:
                    return
                if not c._disposed and getattr(c, "_text_input", False):
                    c.invalidate()
            for node in (previous, c):
                if node is not None and not node._disposed:
                    node.focused_changed.emit(UiEvent(source=node, origin="system"))
        focused = self.focus
        self.runtime.host.text_input(
            focused._rect if focused is not None and not focused._disposed
            and getattr(focused, "_text_input", False) else None
        )
        self.runtime.invalidate()

    def take_focus_reveal(self):
        """Consume the latest valid request for the runtime's layout pass."""
        control, self._focus_to_reveal = self._focus_to_reveal, None
        if (
            control is not None
            and control is self.focus
            and self._visible(control)
            and control._root() is self.runtime.app
        ):
            return control
        return None

    def _tab(self, back=False):
        targets = []

        def visit(c):
            if not self._visible(c):
                return
            if c._values["focusable"]:
                targets.append(c)
            for x in getattr(c, "_children", ()):
                visit(x)

        visit(self.runtime.popup or self.runtime.modal or self.runtime.app)
        targets.sort(key=lambda c: c._values["tab_index"])
        if targets:
            index = (
                targets.index(self.focus)
                if self.focus in targets
                else (0 if back else -1)
            )
            self.set_focus(targets[(index + (-1 if back else 1)) % len(targets)])

    def clipboard(self, key):
        """Capture this request before returning its scheduled I/O coroutine."""
        c = self.focus
        if c is not None and getattr(c, "_cell_copy", False):
            snapshot = (
                (c.selected_key, c.selected_column, c.selected_cell_text)
                if key == "c" and not c._disposed else (None, None, None)
            )
            return self._copy_grid(c, snapshot, self._focus_revision)
        snapshot = (
            (c.text, c.selection_range)
            if c is not None
            and not c._disposed
            and getattr(c, "_text_input", False)
            and not (key in ("v", "x") and c.read_only)
            and not (key in ("c", "x") and c.password)
            else None
        )
        return self._clipboard(key, c, snapshot, self._focus_revision)

    async def _copy_grid(self, grid, snapshot, focus_revision):
        if (
            snapshot[2] is None
            or grid._disposed
            or self.focus is not grid
            or self._focus_revision != focus_revision
            or not self._visible(grid)
            or (grid.selected_key, grid.selected_column, grid.selected_cell_text) != snapshot
        ):
            return
        try:
            await self.runtime.host.clipboard_write(snapshot[2])
        except CapabilityError:
            return
        self.runtime.invalidate()

    async def _clipboard(self, key, c, snapshot, focus_revision):
        def replace(text):
            origin = c._origin
            c._origin = "user"
            try:
                c.replace_selection(text)
            finally:
                c._origin = origin

        def unchanged():
            return (
                not c._disposed
                and self.focus is c
                and self._focus_revision == focus_revision
                and self._visible(c)
                and (key == "c" or not c.read_only)
                and (c.text, c.selection_range) == snapshot
            )

        # Later events in the same host batch can change focus or selection
        # before this task starts, as well as while clipboard I/O is pending.
        if snapshot is None or not unchanged():
            return
        try:
            if key == "v":
                text = await self.runtime.host.clipboard_read()
                if unchanged():
                    replace(text)
            elif key in ("c", "x") and not c.password:
                await self.runtime.host.clipboard_write(c.selection_text)
                if key == "x" and unchanged():
                    replace("")
        except CapabilityError:
            # Clipboard permission and availability are recoverable service
            # failures. In particular, a denied cut must retain the selection.
            return
        self.runtime.invalidate()

    def reconcile(self):
        popup = self.runtime.popup
        if popup is not None:
            popup.reconcile()
        if self.drag_source is not None and (
            not self._visible(self.drag_source)
            or not self.runtime._modality.allowed(self.drag_source)
            or self.drag_source._root() is not self.runtime.app
        ):
            self.cancel_drag()
        elif self.drop_target is not None and (
            not self._visible(self.drop_target)
            or not self.runtime._modality.allowed(self.drop_target)
            or self.drop_target._root() is not self.runtime.app
            or self.drop_target._clip.width <= 0
            or self.drop_target._clip.height <= 0
        ):
            previous, self.drop_target = self.drop_target, None
            self._drag_event(previous, "drag_leave", self._pointer, target=previous)
            if not previous._disposed:
                previous.invalidate()
        for name in ("capture", "focus", "hover"):
            c = getattr(self, name)
            if c is not None and (
                not self._visible(c) or not self.runtime._modality.allowed(c)
                or (name == "focus" and not c._values["focusable"])
            ):
                if name == "capture":
                    self.cancel_capture(clear_focus=True)
                if name == "hover":
                    self._set_hover(None)
                if name == "focus":
                    self.set_focus(None)
                else:
                    setattr(self, name, None)

    def cancel_capture(self, *, clear_focus=False):
        control = self.capture
        before = (
            interaction_state(control)
            if control is not None and not control._disposed
            else None
        )
        self._finish_drag(cancelled=True, reset_source=False)
        self.capture = None
        self.capture_id = None
        if control is not None:
            if clear_focus and control is self.focus:
                self.set_focus(None)
                before = None  # set_focus already retargeted the blur transition.
            elif not control._disposed:
                control.handle_input(Input("blur"))
            control._pressed = False
            if hasattr(control, "_drag"):
                control._drag = None
            if not control._disposed:
                if before is not None:
                    self.runtime.motion.changed(control, before)
                control.invalidate()

    def process(self, e):
        self.reconcile()
        if getattr(self.runtime, "_native_blockers", ()):
            return
        if e.kind == "pointer_up" and e.button != 1:
            return
        if (
            e.kind.startswith("pointer")
            and self.capture is not None
            and e.pointer_id != self.capture_id
        ):
            return
        if e.kind.startswith("pointer"):
            self._pointer = e
        if self.drag_source is not None:
            if e.kind in ("blur", "pointer_cancel") or (
                e.kind == "key_down" and e.key == "Escape"
            ):
                self.cancel_capture(clear_focus=e.kind == "blur")
                if e.kind != "blur":
                    return
            elif e.kind == "pointer_leave":
                previous, self.drop_target = self.drop_target, None
                self._drag_event(previous, "drag_leave", e, target=previous)
                if previous is not None and not previous._disposed:
                    previous.invalidate()
            elif e.kind in ("pointer_move", "pointer_up"):
                try:
                    self._update_drop_target(e)
                    if e.kind == "pointer_up":
                        self._finish_drag(cancelled=False)
                except BaseException:
                    self.cancel_drag()
                    raise
                return
        if e.kind == "pointer_cancel":
            self.cancel_capture()
        if e.kind in ("pointer_cancel", "pointer_leave"):
            self._set_hover(None)
            self.runtime.invalidate()
            return
        local_edit = (
            e.ctrl
            and e.key.lower() in ("a", "c", "v", "x", "z", "y")
            and self.focus is not None
            and getattr(self.focus, "_text_input", False)
        )
        if (
            e.kind == "key_down"
            and not local_edit
            and (e.ctrl or (e.key.startswith("F") and e.key[1:].isdigit()))
        ):
            raw = ("Ctrl+" if e.ctrl else "") + ("Shift+" if e.shift else "") + e.key
            try:
                chord = normalize_shortcut(raw)
            except ValueError:
                chord = ""

            def invoke(node):
                if not self._visible(node):
                    return False
                origin = node._origin
                node._origin = "user"
                previous_popup = self.runtime.popup
                try:
                    if chord and node.invoke_shortcut(chord):
                        popup = self.runtime.popup
                        if (
                            popup is not None
                            and popup is previous_popup
                            and not popup.contains(node)
                        ):
                            popup.dismiss()
                        return True
                finally:
                    node._origin = origin
                return any(invoke(child) for child in getattr(node, "_children", ()))

            if invoke(self.runtime.modal or self.runtime.app):
                return
        popup = self.runtime.popup
        if popup is not None:
            if getattr(popup, "handle_owner_input", lambda event: False)(e):
                return
            if (
                e.kind == "key_down"
                and e.key == "Escape"
                and getattr(popup, "handle_escape", lambda: False)()
            ):
                return
            if e.kind == "blur" or (
                e.kind == "key_down"
                and (e.key == "Escape" or (e.key == "Tab" and popup.dismiss_on_tab))
            ):
                popup.dismiss()
                if e.kind == "key_down" and e.key == "Escape":
                    return
            elif e.kind in ("pointer_down", "wheel") and not popup._clip.contains(
                e.x, e.y
            ):
                owner = popup._owner
                forward = (
                    e.kind == "pointer_down" and e.button == 1
                    and getattr(owner, "_popup_press_passthrough", lambda event: False)(e)
                )
                popup.dismiss()
                # Editing regions may use the dismissing press. Popup openers
                # and unrelated controls still require a separate gesture.
                if not forward or self.hit(
                    self.runtime.modal or self.runtime.app, e.x, e.y
                ) is not owner:
                    return
        if e.kind == "blur":
            self.cancel_capture(clear_focus=True)
            self._set_hover(None)
            self.set_focus(None)
            # The root receives actual host focus loss even when no child is
            # focused. Local focus transfers only send blur to the old child.
            app = self.runtime.app
            if not app._disposed:
                app.handle_input(e)
            return
        if e.kind.startswith("pointer"):
            if e.button == 3 and e.kind == "pointer_down":
                target = self.hit(
                    self.runtime.popup or self.runtime.modal or self.runtime.app,
                    e.x,
                    e.y,
                )
                if target is not None:
                    target.context_menu.emit(
                        PointerEvent(
                            source=target,
                            x=e.x,
                            y=e.y,
                            pointer_id=e.pointer_id,
                            pointer_kind=e.pointer_kind,
                            origin="user",
                        )
                    )
                return
            if e.button != 1:
                return
            hit = self.hit(
                self.runtime.popup or self.runtime.modal or self.runtime.app, e.x, e.y
            )
            if e.kind == "pointer_move":
                self._set_hover(hit)
                target = self.capture or hit
            elif e.kind == "pointer_down":
                if self.capture is not None:
                    return
                target = hit
                self.capture = target
                self.capture_id = e.pointer_id if target is not None else None
                node = target
                while node is not None:
                    if node._overlay and node._parent:
                        node._parent._children.remove(node)
                        node._parent._children.append(node)
                        node._parent.invalidate(Dirty.HIT_TEST)
                        break
                    node = node._parent
                if target and target._values["focusable"]:
                    self.set_focus(target, reveal=False)
                else:
                    from ._controls.windows import SubWindow

                    owner = None
                    if isinstance(target, SubWindow) and target._title_drag_at(
                        e.x - target._rect.x, e.y - target._rect.y
                    ):
                        owner = self.focus
                        while owner is not None and owner is not target:
                            owner = owner._parent
                    if owner is None:
                        self.set_focus(None)
            else:
                target = self.capture or hit
                self.capture = None
                self.capture_id = None
        elif e.kind == "wheel":
            scope = self.runtime.popup or self.runtime.modal or self.runtime.app
            target = self.hit(scope, e.x, e.y)
            while target is not None and not target._disposed:
                before = interaction_state(target)
                parent = target._parent
                target._origin = "user"
                try:
                    consumed = target.scroll_input(e)
                finally:
                    target._origin = "program"
                if not target._disposed:
                    self.runtime.motion.changed(target, before)
                    if consumed:
                        target.invalidate(Dirty.PAINT)
                if consumed or target is scope:
                    break
                target = parent
            self.runtime.invalidate()
            return
        else:
            target = self.focus
            if e.kind == "key_down":
                if e.key == "Tab" and (
                    e.ctrl or not (target and getattr(target, "multiline", False))
                ):
                    self._tab(e.shift)
                    return
                if e.ctrl and e.key.lower() in ("c", "x", "v") and (
                    getattr(target, "_text_input", False)
                    or (e.key.lower() == "c" and getattr(target, "_cell_copy", False))
                ):
                    self.runtime.app.create_task(self.clipboard(e.key.lower()))
                    return
                if e.key == "Escape" and self.runtime.modal:
                    target = self.runtime.modal
        if target is not None and not target._disposed:
            before = interaction_state(target)
            target._origin = "user"
            try:
                target.handle_input(e)
                if target._disposed:
                    return
                if e.kind in ("pointer_down", "pointer_move", "pointer_up"):
                    getattr(target, e.kind).emit(
                        PointerEvent(
                            source=target,
                            origin="user",
                            x=e.x,
                            y=e.y,
                            pointer_id=e.pointer_id,
                            pointer_kind=e.pointer_kind,
                        )
                    )
                elif e.kind in ("key_down", "key_up"):
                    getattr(target, e.kind).emit(
                        KeyEvent(
                            source=target,
                            origin="user",
                            key=e.key,
                            shift=e.shift,
                            ctrl=e.ctrl,
                        )
                    )
            finally:
                target._origin = "program"
            if not target._disposed:
                self.runtime.motion.changed(target, before)
                if e.kind != "pointer_move":
                    target.invalidate(Dirty.PAINT)
        if e.kind != "pointer_move":
            self.runtime.invalidate()
