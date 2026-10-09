"""Image presentation independent of layout and interaction."""

from __future__ import annotations

from typing import Literal
from ..controls import Control
from ..geometry import Rect
from ..host import CapabilityError
from ..schema import prop


class Image(Control):
    cache_paint: bool = prop(default=True)
    source: str = prop(default="")
    fit: Literal["stretch", "contain", "cover"] = prop(default="stretch")

    def reload(self) -> None:
        """Reload this source for every image in the current window."""
        self._check_live()
        runtime = getattr(self._root(), "_runtime", None)
        if self.source and runtime is not None and runtime._host_open:
            reload_image = getattr(runtime.host, "reload_image", None)
            if reload_image is None:
                raise CapabilityError("This host does not support image reload")
            reload_image(self.source)
        self.invalidate()

    def paint(self, p, /):
        if self.source:
            rect = Rect(0, 0, p.width, p.height)
            if self.fit == "stretch":
                p.image(self.source, rect)
            else:
                p.image(self.source, rect, fit=self.fit)


# Keep public imports, diagnostics, and reflection stable across source moves.
Image.__module__ = "pysual.widgets"
