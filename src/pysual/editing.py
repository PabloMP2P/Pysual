"""Reusable draft content and its optional popup presentation.

Content owns ordinary child controls and can be mounted in any Container.
The popup commits through the owner's property, preserving its validation and
change events; content never needs to know which control presents it.
"""

from typing import TYPE_CHECKING, ClassVar

from .controls import Button, Container, Control, Label
from .events import ClickEvent, Event, UiEvent
from .popup import Popup
from .widgets import ScrollArea

if TYPE_CHECKING:
    from .schema import _init_argument


class EditorContent(Container):
    """A draft editor that can be placed inline or supplied to EditorPopup.

    Implement build(value) and read(). Override is_untouched() when formatting
    loses precision, equivalent() for semantic equality, and error_message()
    for concise feedback. Call submit() to request acceptance from its host.
    """

    if TYPE_CHECKING:
        # Preserve the inherited required draft argument when type checkers
        # synthesize constructors for subclasses that only override hooks.
        _editor_value_input: object = _init_argument(alias="value", kw_only=False)

    submitted: ClassVar[Event[UiEvent]] = Event(UiEvent)
    hint: ClassVar[str] = ""
    popup_size: ClassVar[tuple[float, float]] = (300, 158)

    def __init__(self, value: object, **properties: object):
        super().__init__(**{"layout": "stack", "spacing": 8, **properties})
        try:
            self.build(value)
        except BaseException:
            self.destroy()
            raise

    def build(self, value: object) -> None:
        """Create the draft's child controls from its initial value."""
        raise NotImplementedError

    def read(self) -> object:
        """Read and parse the draft, raising ValueError/TypeError if invalid."""
        raise NotImplementedError

    def is_untouched(self) -> bool:
        """Whether accepting should preserve the owner's current value."""
        return False

    def equivalent(self, value: object, current: object) -> bool:
        """Opt into a semantic no-op; ordinary equality belongs to the property.

        By default even explicit reentry reaches the owner's validation hook.
        """
        return False

    def error_message(self, error: Exception, owner: Control) -> str:
        """Explain a rejected draft; owner provides the current constraints."""
        return str(error)

    def submit(self) -> None:
        """Request acceptance; inline hosts can connect to submitted."""
        self.submitted.emit(UiEvent(source=self, origin=self._origin))


class _EditorMessage(Label):
    def paint(self, p, /):
        style = p.body()
        p.text(
            p.elide(self.text, p.width, size=style.font_size, font_family=style.font_family),
            0,
            max(0, (p.height - style.font_size * 1.2) / 2),
            color=style.foreground,
            size=style.font_size,
            font_family=style.font_family,
        )


class EditorPopup(Popup):
    """Present supplied draft content and commit one validated owner property.

    Fresh content is owned and disposed by this popup, including when show()
    fails. Rejected drafts remain open; Escape and dismissal do not commit.
    The value owner must also be the popup anchor, so ordinary popup lifetime
    reconciliation closes the editor when its property target is disposed.
    """

    dismiss_on_tab: ClassVar[bool] = False

    def __init__(
        self,
        owner: Control,
        content: EditorContent,
        *,
        value_property: str = "value",
        **properties: object,
    ):
        owner._check_live()
        if not isinstance(value_property, str) or value_property not in owner._schema:
            raise ValueError("EditorPopup target must be an observable owner property")
        if not isinstance(content, EditorContent):
            raise TypeError("EditorPopup content must be an EditorContent")
        content._check_live()
        if content.parent is not None:
            raise ValueError("EditorPopup requires fresh, unmounted editor content")
        super().__init__(**{
            "width": content.popup_size[0], "height": content.popup_size[1],
            "layout": "stack", "padding": 12, "spacing": 8, **properties,
        })
        self._value_owner = owner
        self._value_property = value_property
        try:
            self.body = ScrollArea(layout="stack", spacing=8, flex=1)
            self.body.add(content)
            self.content = content
            self.message = _EditorMessage(
                parent=self.body, text=content.hint, height=26, font_size=12,
            )
            self.accept_button = Button(text="Apply", icon="check", height=36)
        except BaseException:
            self.destroy()
            # add() rolls back a failed on_attached hook before returning.
            # Such content is detached but still owned by this construction.
            content.destroy()
            raise

    def show(self, anchor: Control, *, at: tuple[float, float] | None = None) -> None:
        was_open = self.is_open
        try:
            if anchor is not self._value_owner:
                raise ValueError("EditorPopup must be anchored to its value owner")
            super().show(anchor, at=at)
        except BaseException:
            if not was_open:
                self.destroy()
            raise

    def content_on_submitted(self, event: UiEvent):
        self.commit()

    def accept_button_on_click(self, event: ClickEvent):
        self.commit()

    def commit(self) -> None:
        if self.content.is_untouched():
            self.dismiss()
            return
        owner = self._value_owner
        try:
            value = self.content.read()
            if self.content.equivalent(value, getattr(owner, self._value_property)):
                self.dismiss()
                return
            previous = owner._origin
            owner._origin = "user"
            try:
                setattr(owner, self._value_property, value)
            finally:
                owner._origin = previous
        except (ValueError, TypeError) as exc:
            self.message.text = self.content.error_message(exc, owner)
            self.message.tooltip = str(exc)
            self.message.foreground = self.effective_theme.tokens.danger
            return
        self.dismiss()
