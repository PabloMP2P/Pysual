"""Small presentational controls sharing the normal schema and input path."""

from typing import TYPE_CHECKING, ClassVar, Literal, cast

from ._text_display import single_line
from .controls import Button, Container, Control
from .errors import LifecycleError
from .geometry import Rect
from .host import CapabilityError
from .painting import resolve_style
from .schema import Dirty, prop

if TYPE_CHECKING:
    from .app import App


class GroupBox(Container):
    """A container with a measured heading above its ordinary content area."""

    cache_paint: bool = prop(default=True)
    title: str = prop(default="", affects=Dirty.MEASURE | Dirty.PAINT)
    padding: float = prop(default=12.0, minimum=0, affects=Dirty.MEASURE)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "heading")
    _style_kind: ClassVar[str] = "GroupBox"

    def measure(self, host):
        return self.measure_available(host)

    def measure_available(self, host, *, width=None, height=None):
        style = resolve_style(self, "heading")
        heading = style.font_size * 1.4 + 8 if self.title else 0
        measured_width, measured_height = self.measure_content(
            host,
            width=width,
            height=max(0, height - heading) if height is not None else None,
        )
        if self.title:
            title_width, _ = host.measure(
                single_line(self.title), style.font_size, style.font_family == "mono"
            )
            measured_width = max(measured_width, title_width + self.padding * 2)
        return measured_width, measured_height + heading

    def content_bounds(self, bounds):
        area = super().content_bounds(bounds)
        height = resolve_style(self, "heading").font_size * 1.4 + 8 if self.title else 0
        return Rect(area.x, area.y + height, area.width, max(0, area.height - height))

    def paint(self, p, /):
        p.body()
        if self.title:
            style = p.style("heading")
            p.text(
                p.elide(
                    single_line(self.title),
                    max(0, p.width - self.padding * 2),
                    size=style.font_size,
                    font_family=style.font_family,
                ),
                self.padding,
                self.padding,
                color=style.foreground,
                size=style.font_size,
                font_family=style.font_family,
            )


class Separator(Control):
    orientation: Literal["horizontal", "vertical"] = prop(
        default="horizontal", affects=Dirty.MEASURE
    )
    _style_kind: ClassVar[str] = "Separator"

    def measure(self, host):
        return (40, 9) if self.orientation == "horizontal" else (9, 40)

    def paint(self, p, /):
        style = p.style()
        if self.orientation == "horizontal":
            p.line(0, p.height / 2, p.width, p.height / 2, style.foreground)
        else:
            p.line(p.width / 2, 0, p.width / 2, p.height, style.foreground)


class Hyperlink(Button):
    """Keyboard-accessible link using the running App's bounded host service."""

    url: str = prop(default="")
    _style_kind: ClassVar[str] = "Hyperlink"
    style_excludes: ClassVar[tuple[str, ...]] = ("Button",)

    def activate(self):
        self._check_activation()
        if not self.effective_enabled:
            return
        super().activate()
        if self.url:
            root = self._root()
            if not getattr(root, "_is_app", False):
                raise LifecycleError("Hyperlinks must be attached to an App")
            url = self.url

            async def open_link():
                try:
                    await cast("App", root).open_url(url)
                except CapabilityError:
                    # Cancelling or denying a host service leaves the UI usable.
                    # Unexpected errors still follow normal task error handling.
                    pass

            self.create_task(open_link())
