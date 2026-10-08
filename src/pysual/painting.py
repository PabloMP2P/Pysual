"""Public control-local painter and one direct tree rendering path."""

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import pairwise
from math import ceil, erfc, hypot, sqrt
from typing import Literal
from weakref import WeakKeyDictionary

from ._engine import critical_phase, ui_method
from ._drawing import gradient_fallback, mix_colors as _mix
from ._nine_slice import MIN_COMPACT_PIXELS, nine_slice
from ._png import rgba_png_data_uri
from .geometry import Rect
from .graphemes import floor_boundary
from .icons import draw_icon
from .host import CapabilityError
from .schema import Dirty, _LAYOUT_MASK, _VISUAL_MASK, _mask
from .theme import Style


def _semantic_draw(host, operation, *args, **kwargs):
    """Dispatch an optional semantic host hook at the shared painter boundary."""
    hook = getattr(host, operation, None)
    return bool(hook is not None and hook(*args, **kwargs))


def _union(a, b):
    if a is b:
        return a
    x, y = min(a.x, b.x), min(a.y, b.y)
    return Rect(x, y, max(a.right, b.right) - x, max(a.bottom, b.bottom) - y)


def _has_shadow(style):
    return style.shadow and not (len(style.shadow) == 9 and style.shadow.endswith("00"))


@lru_cache(maxsize=32)
def _shadow_falloff(steps):
    return tuple(_shadow_coverage(1 - 2 * (i + 0.5) / steps) for i in range(steps))


_GAUSSIAN_TAIL = 0.5 * erfc(3 / sqrt(2))


def _shadow_coverage(distance):
    # Blur the silhouette across its contact edge: half coverage at the edge,
    # approaching opaque inward and zero outward. The former outside-only
    # Gaussian left a solid offset cutout underneath elevated surfaces.
    if distance <= -1:
        return 1
    if distance >= 1:
        return 0
    return (0.5 * erfc(3 * distance / sqrt(2)) - _GAUSSIAN_TAIL) / (
        1 - 2 * _GAUSSIAN_TAIL
    )


_SHADOW_MAX_PIXELS = 4 * 1024 * 1024
_SHADOW_MAX_EDGE = 4096
_SHADOW_IMAGE_BYTES = 2 * 1024 * 1024
_shadow_images: OrderedDict[tuple[int, int, float, int, str], str] = OrderedDict()
_shadow_image_bytes = 0


def _shadow_image(width, height, radius, blur, color):
    """One seamless, straight-alpha PNG; hosts own decoding and GPU lifetimes.

    Only the rounded corner bands need per-pixel math. Constant central spans,
    repeated middle rows, and the two reflection axes keep cold construction
    proportional to the corner area rather than the entire control area.
    """
    global _shadow_image_bytes
    image_width, image_height = width + blur * 2, height + blur * 2
    if (
        width <= 0
        or height <= 0
        or max(image_width, image_height) > _SHADOW_MAX_EDGE
        or image_width * image_height > _SHADOW_MAX_PIXELS
    ):
        return None
    radius = min(max(0, radius), width / 2, height / 2)
    # Include the inward feather too; central spans may repeat only once both
    # the rounded corner and the blur have reached uniform interior coverage.
    edge_x = min(image_width // 2, ceil(blur + max(radius, blur)))
    edge_y = min(image_height // 2, ceil(blur + max(radius, blur)))
    if edge_x * edge_y > 65_536:
        return None
    key = width, height, radius, blur, color
    if key in _shadow_images:
        _shadow_images.move_to_end(key)
        return _shadow_images[key]
    value = color[1:] + ("ff" if len(color) == 7 else "")
    rgb = bytes.fromhex(value[:6])
    opacity = int(value[6:], 16)

    def alpha(distance):
        if not blur:
            return opacity if distance <= 0 else 0
        return round(opacity * _shadow_coverage(distance / blur))

    x_distances = [
        image_width / 2 - (x + 0.5) - (width / 2 - radius) for x in range(edge_x)
    ]

    def row(y):
        dy = abs(y + 0.5 - image_height / 2) - (height / 2 - radius)
        left = [
            rgb
            + bytes(
                (alpha(hypot(max(dx, 0), max(dy, 0)) + min(max(dx, dy), 0) - radius),)
            )
            for dx in x_distances
        ]
        # Odd, narrow masks can have a one-pixel central span that still lies
        # inside the horizontal feather, so evaluate its complete distance too.
        dx = image_width / 2 - (edge_x + 0.5) - (width / 2 - radius)
        center = rgb + bytes(
            (alpha(hypot(max(dx, 0), max(dy, 0)) + min(max(dx, dy), 0) - radius),)
        )
        return (
            b"\0"
            + b"".join(left)
            + center * (image_width - edge_x * 2)
            + b"".join(reversed(left))
        )

    top = [row(y) for y in range(edge_y)]
    raw = (
        b"".join(top)
        + row(edge_y) * (image_height - edge_y * 2)
        + b"".join(reversed(top))
    )

    source = rgba_png_data_uri(image_width, image_height, raw)
    if len(source) > _SHADOW_IMAGE_BYTES:
        return None
    while _shadow_images and (
        len(_shadow_images) >= 64
        or _shadow_image_bytes + len(source) > _SHADOW_IMAGE_BYTES
    ):
        _, previous = _shadow_images.popitem(last=False)
        _shadow_image_bytes -= len(previous)
    _shadow_images[key] = source
    _shadow_image_bytes += len(source)
    return source


def _compact_shadow_image(width, height, radius, blur, color):
    """Keep exact corner/feather samples and one copy of each constant span."""
    # Keep native coordinates finite; _shadow_image enforces resource budgets
    # after the constant output spans have been collapsed into a small atlas.
    if (
        any(
            type(edge) is not int or not 0 < edge <= 2**31 - 1
            for edge in (width, height)
        )
        or type(blur) is not int
        or not 0 <= blur <= _SHADOW_MAX_EDGE
        or not -(2**31) <= radius <= 2**31 - 1
    ):
        return None
    image_width, image_height = width + blur * 2, height + blur * 2
    if max(image_width, image_height) > 2**31 - 1:
        return None
    radius = min(max(0, radius), width / 2, height / 2)
    edge = ceil(blur + max(radius, blur))
    compact = nine_slice(image_width, image_height, edge, edge)
    if compact is None:
        return None
    compact_width, compact_height, edges = compact
    source = _shadow_image(
        compact_width - blur * 2, compact_height - blur * 2, radius, blur, color
    )
    return (source, edges) if source is not None else None


def shadow_bounds(rect, style):
    if not _has_shadow(style):
        return rect
    blur = style.shadow_blur or 0
    return _union(
        rect,
        Rect(
            rect.x + (style.shadow_x or 0) - blur,
            rect.y + (style.shadow_y or 0) - blur,
            rect.width + blur * 2,
            rect.height + blur * 2,
        ),
    )


@lru_cache(maxsize=512)
def _glow_shadow(color, width):
    """A soft, centered halo reuses the bounded shadow-mask pipeline."""
    return Style(shadow=_mix(color[:7] + "00", color, 0.55), shadow_blur=width * 1.5)


def surface_bounds(rect, style):
    bounds = shadow_bounds(rect, style)
    width = min(style.glow_width or 0, rect.width / 2, rect.height / 2)
    if style.glow and width:
        bounds = _union(bounds, shadow_bounds(rect, _glow_shadow(style.glow, width)))
    return bounds


@lru_cache(maxsize=256)
def _style_kinds(control_type):
    kinds = []
    for cls in reversed(control_type.__mro__):
        kinds.extend(vars(cls).get("style_fallbacks", ()))
        if hasattr(cls, "_style_kind"):
            kinds.append(cls._style_kind)
    excluded = control_type.style_excludes
    return tuple(kind for kind in dict.fromkeys(kinds) if kind not in excluded)


def _effect_parts(theme, control_type):
    # Like Theme's resolved-style cache, this metadata belongs to the immutable
    # theme. Do not hash the whole theme/rule graph on every uncached paint.
    cache = theme.__dict__.get("_paint_effect_parts")
    if cache is None:
        cache = theme.__dict__["_paint_effect_parts"] = {}
    if control_type in cache:
        return cache[control_type]
    kinds = _style_kinds(control_type)
    contained = getattr(control_type, "_contained_style_parts", ())
    declared = control_type.style_parts
    candidates = {
        rule.part
        for rule in theme.rules
        if rule.control in kinds
        and rule.part in declared
        and rule.part not in contained
        and (rule.style.shadow or rule.style.glow)
    }
    parts = tuple(part for part in declared if part in candidates)
    if len(cache) >= 256:
        cache.pop(next(iter(cache)))
    cache[control_type] = parts
    return parts


@lru_cache(maxsize=1024)
def _overridden(style, background, foreground, size, family):
    return replace(
        style,
        **{
            name: value
            for name, value in zip(
                ("fill", "foreground", "font_size", "font_family"),
                (background, foreground, size, family),
            )
            if value is not None
        },
    )


@ui_method
def resolve_style(c, part="body", *, selected=False, state=None, checked=None):
    c._check_live()
    state = state or (
        "disabled"
        if not c._committed_enabled()
        else "pressed"
        if c._pressed
        else "hover"
        if c._hover
        else "normal"
    )
    if part not in c.style_parts:
        raise ValueError(f"{type(c).__name__} has no style part {part!r}")
    s = c._fast_theme().resolve(
        _style_kinds(type(c)),
        part,
        state,
        getattr(c, "checked", False) if checked is None else checked,
        selected,
    )
    # The lifecycle boundary is checked above, including when effective state is
    # customized. Schema descriptors store only validated values.
    values = c._values
    overrides = (
        values["background"],
        values["foreground"],
        values["font_size"],
        values["font_family"],
    )
    return _overridden(s, *overrides) if any(v is not None for v in overrides) else s


class Painter:
    def __init__(self, host, rect, theme, control=None):
        self.host, self.bounds, self.theme, self.control = host, rect, theme, control
        self.width, self.height = rect.width, rect.height
        self._styles = {}
        self._clip = control._clip if control is not None else rect
        self._effect_clip = self._clip

    def effect_bounds(self):
        assert self.control is not None
        bounds = self.control.paint_bounds()
        if self.theme._has_shadows or self.theme._has_glows:
            # Parts such as slider thumbs and check glyphs can touch the body's
            # edge. Their effects need padding too, even if the body has none.
            # The full control rectangle conservatively contains built-in part
            # geometry; custom surfaces outside it still declare paint_bounds().
            # Include selected parts used by rows/tabs as well as live motion.
            # Resolve only parts with possible effects. Preparing every normal
            # and selected part doubled live Slider work even in plain themes.
            for part in _effect_parts(self.theme, type(self.control)):
                for selected in (False, True):
                    bounds = _union(
                        bounds,
                        surface_bounds(
                            self.bounds, self.style(part, selected=selected)
                        ),
                    )
        self._effect_clip = (
            self.control._clip
            if bounds is self.bounds
            else bounds.intersect(self.control._paint_clip)
        )
        return self._effect_clip

    @contextmanager
    def _clipped(self, rect: Rect):
        """Constrain a control part, including its effects, in local coordinates."""
        previous, effects = self._clip, self._effect_clip
        area = Rect(
            self.bounds.x + rect.x, self.bounds.y + rect.y, rect.width, rect.height
        )
        self._clip = previous.intersect(area)
        self._effect_clip = effects.intersect(self._clip)
        self.host.clip(self._clip)
        try:
            yield
        finally:
            self._clip, self._effect_clip = previous, effects
            self.host.clip(previous)

    def rect(
        self, rect, fill, *, radius: float = 0, border="", border_width: float = 1
    ):
        self.host.rect(
            Rect(
                self.bounds.x + rect.x, self.bounds.y + rect.y, rect.width, rect.height
            ),
            fill,
            radius,
            border,
            border_width,
        )

    def text(self, text, x, y, *, color=None, size=None, mono=False, font_family=None):
        # Styled parts already supply these values. Resolve the body only for
        # missing defaults, especially in row-heavy controls.
        if color is None or size is None or font_family is None:
            style = self._text_style()
            if color is None:
                color = style.foreground
            if size is None:
                size = style.font_size
            if font_family is None:
                font_family = "mono" if mono else style.font_family
        if font_family not in ("ui", "mono"):
            raise ValueError("font_family must be ui or mono")
        self.host.text(
            str(text),
            self.bounds.x + x,
            self.bounds.y + y,
            color,
            size,
            font_family == "mono",
        )

    def _text_style(self):
        return self.style() if self.control is not None else self.theme.tokens

    def measure(self, text, *, size=None, mono=False, font_family=None):
        if size is None or font_family is None:
            style = self._text_style()
            if size is None:
                size = style.font_size
            if font_family is None:
                font_family = "mono" if mono else style.font_family
        if font_family not in ("ui", "mono"):
            raise ValueError("font_family must be ui or mono")
        return self.host.measure(str(text), size, font_family == "mono")

    def elide(self, text, width, *, size=None, mono=False, font_family=None):
        """Fit a single-line label using the host's actual metrics."""
        text = str(text)
        if size is None or font_family is None:
            style = self._text_style()
            if size is None:
                size = style.font_size
            if font_family is None:
                font_family = "mono" if mono else style.font_family
        if font_family not in ("ui", "mono"):
            raise ValueError("font_family must be ui or mono")
        if width <= 0:
            return ""
        mono_flag = font_family == "mono"
        many = getattr(self.host, "measure_many", None)
        if many is not None:
            return self._elide_many(text, width, size, mono_flag, many)
        if self.host.measure(text, size, mono_flag)[0] <= width:
            return text
        if self.host.measure("…", size, mono_flag)[0] > width:
            return ""
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if self.host.measure(text[:middle] + "…", size, mono_flag)[0] <= width:
                low = middle
            else:
                high = middle - 1
        # Keep the binary search cheap, then avoid leaving half an emoji or
        # dropping a combining mark from the last visible character.
        return text[: floor_boundary(text, low)] + "…"

    def _elide_many(self, text, width, size, mono, many):
        """Measure truncation candidates in one host round trip when possible."""
        if not text:
            return text
        if self.host.measure(text, size, mono)[0] <= width:
            return text
        # Batch short captions; longer captions need only the fitting interval.
        if len(text) <= 32:
            samples = ["…", *(text[:index] + "…" for index in range(len(text) + 1))]
            widths = [item[0] for item in many(samples, size, mono)]
            if widths[0] > width:
                return ""
            best = 0
            for index, sample in enumerate(widths[1:]):
                if sample <= width:
                    best = index
                else:
                    break
            return text[:floor_boundary(text, best)] + "…"
        probes = []
        step = 1
        while step < len(text):
            probes.append(step)
            step *= 2
        if probes[-1] != len(text):
            probes.append(len(text))
        samples = ["…", *(text[:index] + "…" for index in probes)]
        widths = [item[0] for item in many(samples, size, mono)]
        if widths[0] > width:
            return ""
        fitted = 0
        upper = len(text)
        for probe, sample in zip(probes, widths[1:]):
            if sample <= width:
                fitted = probe
            else:
                upper = probe
                break
        if upper - fitted <= 1:
            return text[:floor_boundary(text, fitted)] + "…"
        span = range(fitted + 1, upper + 1)
        extra = [item[0] for item in many([text[:index] + "…" for index in span], size, mono)]
        best = fitted
        for index, sample in zip(span, extra):
            if sample <= width:
                best = index
            else:
                break
        return text[:floor_boundary(text, best)] + "…"

    def line(self, x1, y1, x2, y2, color, width=1):
        self.host.line(
            self.bounds.x + x1,
            self.bounds.y + y1,
            self.bounds.x + x2,
            self.bounds.y + y2,
            color,
            width,
        )

    def lines(self, points, color, width=1):
        """Draw connected line segments in local coordinates.

        Hosts can batch opaque strokes. Segment overlap and translucent colors
        retain the same composition as consecutive calls to ``line``.
        """
        points = tuple((self.bounds.x + x, self.bounds.y + y) for x, y in points)
        if len(points) < 2:
            return
        lines = getattr(self.host, "lines", None)
        if lines is not None:
            lines(points, color, width)
        else:
            for (x1, y1), (x2, y2) in pairwise(points):
                self.host.line(x1, y1, x2, y2, color, width)

    def segments(self, segments, color, width=1):
        """Draw independent (x1, y1, x2, y2) strokes in their original order."""
        segments = tuple(
            (self.bounds.x + x1, self.bounds.y + y1,
             self.bounds.x + x2, self.bounds.y + y2)
            for x1, y1, x2, y2 in segments
        )
        if not segments:
            return
        draw = getattr(self.host, "segments", None)
        if draw is not None:
            draw(segments, color, width)
        else:
            for x1, y1, x2, y2 in segments:
                self.host.line(x1, y1, x2, y2, color, width)

    def caret(self, x, y, height, color):
        """Paint an insertion marker; fixed-cell hosts can retain the glyph."""
        if _semantic_draw(
            self.host, "caret",
            self.bounds.x + x, self.bounds.y + y, height, color
        ):
            return
        self.line(x, y, x, y + height, color, 1)

    def icon(
        self, name: str, x: float, y: float, *, size: float = 20, color: str | None = None
    ) -> None:
        style = self._text_style()
        draw_icon(self, name, x, y, size, style.foreground if color is None else color)

    def _native_icon(self, name, x, y, size, color):
        """Give hosts with semantic glyphs the original icon before tessellation."""
        return _semantic_draw(
            self.host, "icon", name,
            self.bounds.x + x, self.bounds.y + y, size, color,
        )

    def marker(
        self, rect: Rect, style: Style, *,
        shape: Literal["square", "circle"] = "square", checked: bool = False,
    ) -> None:
        """Draw a choice indicator or thumb with an explicit shape and state.

        Cell hosts can use one glyph. Other hosts draw the themed surface plus a
        check or inner dot. Ordinary surfaces never imply marker semantics.
        """
        if shape not in ("square", "circle"):
            raise ValueError("Marker shape must be square or circle")
        if type(checked) is not bool:
            raise TypeError("Marker checked state must be a bool")
        if rect.width <= 0 or rect.height <= 0:
            return
        if checked and style.foreground is None:
            style = replace(style, foreground=self._text_style().foreground)
        if _semantic_draw(
            self.host, "marker",
            Rect(self.bounds.x + rect.x, self.bounds.y + rect.y, rect.width, rect.height),
            style, shape=shape, checked=checked,
            **({"effect_clip": self._effect_clip} if getattr(self.host, "native_retained", False) else {}),
        ):
            return
        diameter = min(rect.width, rect.height)
        self.surface(
            rect, replace(style, radius=diameter / 2) if shape == "circle" else style
        )
        if checked:
            if shape == "square":
                self.icon(
                    "check", rect.x + (rect.width - diameter) / 2,
                    rect.y + (rect.height - diameter) / 2,
                    size=diameter, color=style.foreground,
                )
            else:
                self.rect(
                    rect.inset(diameter * 0.3), style.foreground, radius=diameter * 0.2
                )

    def focus_ring(self, color: str, radius: float = 0) -> None:
        """Indicate keyboard focus using the host's full-bounds representation."""
        if not _semantic_draw(self.host, "focus_ring", self.bounds, color, radius):
            self.host.rect(self.bounds.inset(1), "", max(0, radius - 1), color, 2)

    def content_rect(self, part="body"):
        """Return control-local space inside the resolved normal part padding."""
        return Rect(0, 0, self.width, self.height).inset(self.style(part).padding)

    def image(self, source, rect, *, fit="stretch"):
        if fit not in ("stretch", "contain", "cover"):
            raise ValueError("Image fit must be stretch, contain or cover")
        bounds = Rect(
            self.bounds.x + rect.x, self.bounds.y + rect.y, rect.width, rect.height
        )
        if fit == "stretch":
            self.host.image(source, bounds)
        elif "image_fit" in getattr(self.host, "capabilities", ()):
            self.host.image(source, bounds, fit=fit)
        else:
            raise CapabilityError("This host does not support image fitting")

    def sprite(self, source, rect, *, frame_width, frame_height, frame_count,
               fps=12, loop=True, tint="#ffffff"):
        """Declare a sprite sheet once; native C advances row-major frames."""
        hook = getattr(self.host, "sprite", None)
        if hook is None:
            raise CapabilityError("This host does not provide native sprite playback")
        hook(source, Rect(self.bounds.x + rect.x, self.bounds.y + rect.y,
                          rect.width, rect.height), frame_width=frame_width,
             frame_height=frame_height, frame_count=frame_count, fps=fps,
             loop=loop, tint=tint)

    def animated_rect(self, rect, fill, *, radius=0, border="", border_width=1,
                      property="x", from_value, to_value, duration=1,
                      loop=True, yoyo=True):
        """Declare native interpolation; values use control-local coordinates."""
        hook = getattr(self.host, "animated_rect", None)
        if hook is None:
            raise CapabilityError("This host does not provide native property animation")
        if property not in ("x", "y", "opacity"):
            raise ValueError("Animated rectangle property must be x, y or opacity")
        offset = self.bounds.x if property == "x" else self.bounds.y if property == "y" else 0
        hook(Rect(self.bounds.x + rect.x, self.bounds.y + rect.y, rect.width, rect.height),
             fill, radius=radius, border=border, border_width=border_width,
             property=property, from_value=from_value + offset, to_value=to_value + offset,
             duration=duration, loop=loop, yoyo=yoyo)

    def style(self, part="body", *, selected=False):
        assert self.control is not None
        key = (part, selected)
        if key not in self._styles:
            style = resolve_style(self.control, part, selected=selected)
            motion = self.control._motion
            self._styles[key] = (
                motion.style(part, style)
                if motion is not None and not selected
                else style
            )
        return self._styles[key]

    def body(self, part="body"):
        s = self.style(part)
        self.surface(Rect(0, 0, self.width, self.height), s)
        return s

    def _gradient(self, rect, first, last, axis, radius, border_width: float = 0):
        # Hosts can paint the same device-aligned color bands in one operation.
        # The fallback remains available to third-party hosts and large surfaces.
        absolute = Rect(
            self.bounds.x + rect.x, self.bounds.y + rect.y, rect.width, rect.height
        )
        gradient = getattr(self.host, "gradient_rect", None)
        if gradient is not None and gradient(
            absolute, first, last, axis, radius, border_width
        ):
            return
        # Device-aligned bands avoid fractional-scale seams. Each band clips the
        # same rounded geometry; corners and alpha follow the ordinary host path.
        gradient_fallback(
            self.host, absolute, first, last, axis, radius, border_width, self._clip
        )

    def surface(self, rect: Rect, style: Style) -> None:
        """Paint a typed surface inside rect; also available to custom controls.

        Shadows and glow halos use declared effect bounds, clipped by the parent;
        other effects stay inside the body. Neither changes layout or hit testing.
        """
        if rect.width <= 0 or rect.height <= 0:
            return
        # A cell host needs the complete surface to choose one readable border.
        # Expanding glow, shadow and bevel layers into separate pixel strokes
        # first loses that distinction when several layers share the same cell.
        if _semantic_draw(
            self.host, "styled_rect",
            Rect(
                self.bounds.x + rect.x, self.bounds.y + rect.y, rect.width, rect.height
            ),
            style,
            **({"effect_clip": self._effect_clip} if getattr(self.host, "native_retained", False) else {}),
        ):
            return
        radius = min(style.radius or 0, rect.width / 2, rect.height / 2)
        if _has_shadow(style):
            self._shadow(rect, radius, style)
        glow_width = min(style.glow_width or 0, rect.width / 2, rect.height / 2)
        if style.glow and glow_width:
            self._shadow(rect, radius, _glow_shadow(style.glow, glow_width))
        width = style.border_width or 0
        axis = style.gradient_axis or "vertical"
        if style.fill:
            if style.fill_end and style.fill_end != style.fill:
                self._gradient(rect, style.fill, style.fill_end, axis, radius)
            else:
                self.rect(rect, style.fill, radius=radius)
        inset = max(width, radius, 2)
        inside = rect.inset(inset)
        if style.pattern in ("scanlines", "grid") and style.pattern_color:
            step = style.pattern_spacing or 6
            # At most 128 lines per axis, even on huge or malicious theme surfaces.
            for vertical in (False, True) if style.pattern == "grid" else (False,):
                length = inside.width if vertical else inside.height
                for i in range(min(128, max(0, ceil(length / step)))):
                    pos = i * step
                    band = (
                        Rect(inside.x + pos, inside.y, 1, inside.height)
                        if vertical
                        else Rect(inside.x, inside.y + pos, inside.width, 1)
                    )
                    self.rect(band, style.pattern_color)
        if style.glow and glow_width:
            for fraction in (1, 0.75, 0.5, 0.25):
                self.rect(
                    rect.inset(width),
                    "",
                    radius=max(0, radius - width),
                    border=_mix(style.glow[:7] + "00", style.glow, 0.16),
                    border_width=glow_width * fraction,
                )
        if style.border and width:
            if style.border_end and style.border_end != style.border:
                self._gradient(
                    rect, style.border, style.border_end, axis, radius, width
                )
            else:
                self.rect(
                    rect, "", radius=radius, border=style.border, border_width=width
                )
        if style.inner_border:
            self.rect(
                rect.inset(width + 3),
                "",
                radius=max(0, radius - width - 3),
                border=style.inner_border,
                border_width=1,
            )
        if style.bevel in ("raised", "sunken"):
            light, dark = style.bevel_light or "#ffffff", style.bevel_dark or "#404040"
            if style.bevel == "sunken":
                light, dark = dark, light
            thickness = min(
                style.bevel_width if style.bevel_width is not None else 2,
                rect.width / 2,
                rect.height / 2,
            )
            # Beveled themes use square corners; inset edges also work with radius.
            area = rect.inset(radius)
            self.rect(Rect(area.x, area.y, area.width, thickness), light)
            self.rect(Rect(area.x, area.y, thickness, area.height), light)
            self.rect(
                Rect(area.x, area.bottom - thickness, area.width, thickness), dark
            )
            self.rect(
                Rect(area.right - thickness, area.y, thickness, area.height), dark
            )
        if style.highlight and inside.width > 0:
            self.rect(
                Rect(inside.x, rect.y + max(2, width), inside.width, 1), style.highlight
            )

    def _shadow(self, rect, radius, style):
        blur = style.shadow_blur or 0
        area = Rect(
            rect.x + (style.shadow_x or 0),
            rect.y + (style.shadow_y or 0),
            rect.width,
            rect.height,
        )
        scale = getattr(self.host, "render_scale", 1)
        pixels = ceil(blur * scale)
        steps = min(32, max(1, pixels * 2))
        falloff = _shadow_falloff(steps)
        x = round((self.bounds.x + area.x) * scale)
        y = round((self.bounds.y + area.y) * scale)
        w, h = round(area.width * scale), round(area.height * scale)
        base_radius = round(radius * scale)
        try:
            self.host.clip(self._effect_clip)
            capabilities = getattr(self.host, "capabilities", ())
            if "shadow_masks" in capabilities:
                # A tintable white mask is independent of theme color/opacity.
                # Hover fades reuse one decoded texture instead of rasterizing,
                # compressing and uploading a new PNG for every animation step.
                tinted = "tinted_images" in capabilities
                image_nine = getattr(self.host, "image_nine", None)
                compact = (
                    _compact_shadow_image(
                        w, h, base_radius, pixels, "#ffffff" if tinted else style.shadow
                    )
                    if image_nine is not None
                    and (w + pixels * 2) * (h + pixels * 2) >= MIN_COMPACT_PIXELS
                    else None
                )
                if compact is not None and image_nine is not None:
                    source, edges = compact
                    tint = {"tint": style.shadow} if tinted else {}
                    image_nine(
                        source,
                        Rect(
                            (x - pixels) / scale,
                            (y - pixels) / scale,
                            (w + pixels * 2) / scale,
                            (h + pixels * 2) / scale,
                        ),
                        edges,
                        **tint,
                    )
                    return
                source = _shadow_image(
                    w, h, base_radius, pixels, "#ffffff" if tinted else style.shadow
                )
                if source is not None:
                    tint = {"tint": style.shadow} if tinted else {}
                    self.host.image(
                        source,
                        Rect(
                            (x - pixels) / scale,
                            (y - pixels) / scale,
                            (w + pixels * 2) / scale,
                            (h + pixels * 2) / scale,
                        ),
                        **tint,
                    )
                    return
            # Bounded fallback for enormous geometry or hosts without images.
            # Disjoint rings avoid accumulating alpha rounding on cache blits.
            for i in range(steps):
                spread = pixels - pixels * 2 * i // steps
                next_spread = pixels - pixels * 2 * (i + 1) // steps
                color = _mix(style.shadow[:7] + "00", style.shadow, falloff[i])
                if spread > next_spread and min(w, h) + spread * 2 > 0:
                    self.rect(
                        Rect(
                            (x - spread) / scale - self.bounds.x,
                            (y - spread) / scale - self.bounds.y,
                            (w + spread * 2) / scale,
                            (h + spread * 2) / scale,
                        ),
                        "",
                        radius=max(0, base_radius + spread) / scale,
                        border=color,
                        border_width=(spread - next_spread) / scale,
                    )
            center = area.inset(pixels / scale)
            if center.width and center.height:
                self.rect(center, style.shadow, radius=max(0, radius - pixels / scale))
        finally:
            self.host.clip(self._clip)


@dataclass
class _PaintSegment:
    identity: str
    key: object = None
    start_index: int = 0
    end_index: int = 0


def _custom_paint(control):
    """Inherited framework painters obey local invalidation independently."""
    return any(
        not getattr(getattr(type(control), hook), "__module__", "").startswith("pysual.")
        for hook in ("paint", "paint_bounds")
    )


class RetainedPaintTree:
    """Record changed control bodies without revisiting the rest of the tree.

    Layout or inherited-policy changes reconcile lightweight scene metadata.
    Ordinary interaction changes consume exact invalidation origins instead.
    Native segments retain command storage, z order and independent clips.
    """

    def __init__(self, host):
        self.host = host
        self._records = {}
        self._control_order = []
        self._identities = WeakKeyDictionary()
        self._next_identity = 0
        self._dirty = set()
        self._uncached_custom = set()
        self._reconcile = True
        self._force = True
        self._order = []
        self._focus = None
        self._drop_target = None
        self._environment = None
        self._modality_order = ()
        self._pending_remove = set()
        self._metadata_dirty = set()
        self._layout_controls = set()
        self._layout_seen = False

    def invalidate(self, control, affects):
        # Only planner state changes here; host calls and custom hooks happen
        # during the next paint pass.
        mask = _mask(affects)
        if mask & (_LAYOUT_MASK | int(Dirty.HIT_TEST)):
            self._reconcile = True
            self._metadata_dirty.add(control)
        if mask & _VISUAL_MASK:
            self._dirty.add(control)
            # Custom container paint hooks may depend on descendant state, as
            # they did with propagated RenderCache revisions before patching.
            node = control._parent
            while node is not None:
                if _custom_paint(node):
                    self._dirty.add(node)
                node = node._parent

    def invalidate_all(self):
        self._force = self._reconcile = True

    def layout_started(self):
        self._layout_seen = True

    def layout_changed(self, control):
        self._layout_controls.add(control)

    def clear(self):
        """Release control references after this planner's host scene closes."""
        self._records.clear()
        self._control_order.clear()
        self._identities.clear()
        self._dirty.clear()
        self._uncached_custom.clear()
        self._order.clear()
        self._pending_remove.clear()
        self._metadata_dirty.clear()
        self._layout_controls.clear()
        self._layout_seen = False
        self._focus = self._drop_target = self._environment = None
        self._modality_order = ()
        self.invalidate_all()

    def _identity(self, control):
        identity = self._identities.get(control)
        if identity is None:
            self._next_identity += 1
            identity = self._identities[control] = f"control:{self._next_identity}"
        return identity

    def _key(self, control, focus):
        # A few framework containers paint geometry derived during layout even
        # when their own rectangle/properties are unchanged (scrollbars and
        # split dividers). Declare that dependency rather than treating every
        # descendant edit as a body change on every ancestor.
        layout_key = getattr(control, "_paint_layout_key", None)
        return (
            control._paint_local_revision,
            control._rect,
            control._clip,
            control._paint_clip,
            control._fast_theme(),
            control._committed_enabled(),
            control._hover,
            control._pressed,
            control is focus,
            layout_key() if layout_key is not None else None,
        )

    def _reconcile_tree(self, root, runtime, focus, affected, *, full=False, prepared=None):
        previous = self._records
        ranges = {control: (record.start_index, record.end_index)
                  for control, record in previous.items()}
        paths = {root}
        for control in affected:
            node = control
            while node is not None and node not in paths:
                paths.add(node)
                node = node._parent
        records, controls, order, uncached = {}, [], [], set()
        prepared = {} if prepared is None else prepared

        def visit(control, inherited_changed=False):
            record = previous.get(control)
            if record is not None and not full and not inherited_changed and control not in paths:
                # An unaffected subtree keeps its exact geometry, policies and
                # membership. Copy only references/order, without UI reads or
                # paint-key checks for every control below it.
                start, end = ranges[control]
                offset = len(order) - start
                for child in self._control_order[start:end]:
                    cached = previous[child]
                    old_start, old_end = ranges[child]
                    cached.start_index, cached.end_index = old_start + offset, old_end + offset
                    records[child] = cached
                    controls.append(child)
                    order.append(cached.identity)
                    if child in self._uncached_custom:
                        uncached.add(child)
                return
            if control._disposed or not control._values["visible"]:
                return
            if record is None:
                record = _PaintSegment(self._identity(control))
            key = prepared.get(control)
            if key is None:
                key = prepared[control] = self._key(control, focus)
            inherited_changed = inherited_changed or (
                record.key is not None and record.key[4:6] != key[4:6]
            )
            record.start_index = len(order)
            records[control] = record
            controls.append(control)
            order.append(record.identity)
            if not control._values["cache_paint"] and _custom_paint(control):
                # Noncacheable custom painters retain their original update-
                # time behavior, including explicit request_frame() calls.
                uncached.add(control)
            children = getattr(control, "_children", ())
            if children and control._paint_clip.width > 0 and control._paint_clip.height > 0:
                ordered = (
                    runtime._modality.paint_order(children)
                    if runtime is not None
                    else sorted(children, key=lambda child: child._overlay)
                )
                for child in ordered:
                    visit(child, inherited_changed)
            record.end_index = len(order)

        visit(root)
        removed = [record.identity for control, record in previous.items() if control not in records]
        changed = order != self._order
        self._records, self._control_order, self._order, self._uncached_custom = records, controls, order, uncached
        return removed, changed, prepared

    def _draw_body(self, control, record, focus, key):
        host = self.host
        theme = key[4]
        painter = Painter(host, control._rect, theme, control)
        effect = painter.effect_bounds()
        # Text and focus use the body clip; shadows use the effect clip. Their
        # union is conservative even when a custom effect excludes the body.
        bounds = _union(effect, control._clip)
        host.begin_segment(record.identity, bounds)
        started = False
        completed = False
        try:
            if effect.width > 0 and effect.height > 0:
                started = (
                    "isolated_paint" in getattr(host, "capabilities", ())
                    and control._values["cache_paint"] and host.paint_begin(effect)
                )
                host.clip(control._clip)
                control.paint(painter)
                completed = True
            if started:
                host.paint_end(completed)
                started = False
            if control is focus:
                host.clip(control._clip)
                control.paint_focus(painter)
        finally:
            if started:
                host.paint_end(completed)
            host.end_segment()
        record.key = key

    def paint(self, root, focus=None, *, layout_changed=False):
        try:
            return self._paint(root, focus, layout_changed=layout_changed)
        except BaseException:
            # Recording and the native acknowledgment form one scene update.
            # Re-send bodies, order and outstanding removals after a failure;
            # no whole-tree snapshots are needed on successful local changes.
            self.invalidate_all()
            raise

    def _paint(self, root, focus=None, *, layout_changed=False):
        host = self.host
        runtime = getattr(root._root(), "_runtime", None)
        target = runtime.router.drop_target if runtime is not None else None
        modality_order = tuple(
            entry.control
            for entry in getattr(getattr(runtime, "_modality", None), "_openings", ())
        )
        if modality_order != self._modality_order:
            self._reconcile = True
            self._metadata_dirty.update(self._modality_order)
            self._metadata_dirty.update(modality_order)
        environment = (
            getattr(host, "render_scale", 1),
            getattr(host, "resource_revision", 0),
        )
        if environment != self._environment:
            self.invalidate_all()
        dirty, self._dirty = self._dirty, set()
        metadata_dirty, self._metadata_dirty = self._metadata_dirty, set()
        layout_controls, self._layout_controls = self._layout_controls, set()
        layout_seen, self._layout_seen = self._layout_seen, False
        force, self._force = self._force, False
        reconcile, self._reconcile = self._reconcile or layout_changed, False
        for control in (self._focus, focus):
            if self._focus is not focus and control is not None:
                dirty.add(control)
        remove, order_changed, prepared = [], False, {}
        if not reconcile:
            # Custom inherited policies may change private state and call the
            # ordinary invalidate() (PAINT only). Check already-dirty containers
            # before taking the local-body path; unchanged policies still avoid
            # visiting descendants. Reuse these keys during reconciliation.
            for control in dirty:
                record = self._records.get(control)
                if (record is not None and not control._disposed
                        and getattr(control, "_children", ())):
                    key = prepared[control] = self._key(control, focus)
                    if record.key is not None and record.key[4:6] != key[4:6]:
                        reconcile = True
        if reconcile:
            affected = metadata_dirty | layout_controls | dirty
            remove, order_changed, prepared = self._reconcile_tree(
                root, runtime, focus, affected,
                full=force or layout_changed and not layout_seen,
                prepared=prepared,
            )
            self._pending_remove.update(remove)
        # cache_paint itself is a local drawing change. Refresh admission even
        # when unrelated topology changes reused this control's subtree.
        for control in dirty:
            if control in self._records and not control._disposed and _custom_paint(control):
                if control._values["cache_paint"]:
                    self._uncached_custom.discard(control)
                else:
                    self._uncached_custom.add(control)
        candidates = set(prepared) | dirty | self._uncached_custom
        host.begin_scene(root._fast_theme().tokens.background)
        for control in candidates:
            record = self._records.get(control)
            if record is None or control._disposed:
                continue
            key = prepared.get(control)
            if key is None:
                key = self._key(control, focus)
            if (force or record.key != key or control in self._uncached_custom
                    or control in dirty and _custom_paint(control)):
                self._draw_body(control, record, focus, key)
        if target not in self._records:
            target = None
        target_changed = target is not self._drop_target
        if self._drop_target is not None and target_changed:
            self._pending_remove.add(self._identity(self._drop_target) + ":drag")
        if target is not None and (target_changed or reconcile or force or target in dirty):
            host.begin_segment(self._identity(target) + ":drag", target._clip)
            host.clip(target._clip)
            host.rect(target._rect.inset(2), "", 0, target._fast_theme().tokens.accent, 3)
            host.end_segment()
        order = None
        if order_changed or target_changed or force:
            order = list(self._order)
            if target is not None:
                order.insert(self._records[target].end_index, self._identity(target) + ":drag")
        if self._pending_remove:
            live = set(self._order)
            if target is not None:
                live.add(self._identity(target) + ":drag")
            # A control may be reattached between a failed update and its retry.
            remove = [identity for identity in self._pending_remove if identity not in live]
        host.end_scene(order=order, remove=remove)
        self._pending_remove.clear()
        self._focus, self._drop_target, self._environment = focus, target, environment
        self._modality_order = modality_order


@ui_method
def paint_tree(root, host, focus=None, cache=None, *, retained=None, layout_changed=False):
    with critical_phase("layout and paint"):
        if retained is not None:
            return retained.paint(root, focus, layout_changed=layout_changed)
        return _paint_tree(root, host, focus, cache)


def _paint_tree(root, host, focus=None, cache=None):
    host.begin(root._fast_theme().tokens.background)
    isolate = "isolated_paint" in getattr(host, "capabilities", ())
    runtime = getattr(root._root(), "_runtime", None)
    drop_target = runtime.router.drop_target if runtime is not None else None
    group_begin = getattr(host, "_paint_group_begin", None)
    group_end = getattr(host, "_paint_group_end", None)
    if cache is not None:
        cache.start_frame()

    def visit(c):
        if c._disposed or not c._values["visible"]:
            return
        theme = c._fast_theme()
        children = getattr(c, "_children", ())
        key = cache.paint_key(c, focus, theme) if cache is not None else None
        painter = None
        # Retained scene hosts need one stable body slot across cache admission
        # and focus changes. Pixel hosts keep their existing immediate path.
        if group_begin is not None:
            group_begin()
        try:
            if cache is None or key is None or not cache.reuse(c, key):
                painter = Painter(host, c._rect, theme, c)
                clip = painter.effect_bounds()

                def draw():
                    started = isolate and c._values["cache_paint"] and host.paint_begin(clip)
                    completed = False
                    try:
                        host.clip(c._clip)
                        c.paint(painter)
                        completed = True
                    finally:
                        if started:
                            host.paint_end(completed)

                if clip.width > 0 and clip.height > 0:
                    if cache is None:
                        draw()
                    else:
                        cache.paint(c, focus, draw, clip=clip, prepared_key=key)
                elif not children or c._paint_clip.width <= 0 or c._paint_clip.height <= 0:
                    return
                # A transparent row can be completely outside a viewport while
                # its child's shadow/halo still reaches inside. Cull bodies
                # separately from descendant effects and their viewport clips.
            if c is focus:
                if painter is None:
                    painter = Painter(host, c._rect, theme, c)
                host.clip(c._clip)
                c.paint_focus(painter)
        finally:
            if group_end is not None:
                group_end()
        if children:
            ordered = (
                runtime._modality.paint_order(children)
                if runtime is not None
                else sorted(children, key=lambda x: x._overlay)
            )
            for child in ordered:
                visit(child)
        # Drag feedback is transient and stays outside cached bodies, just like
        # keyboard focus. Draw after children so a container target is visible.
        if c is drop_target:
            host.clip(c._clip)
            host.rect(c._rect.inset(2), "", 0, theme.tokens.accent, 3)

    visit(root)
    if cache is not None:
        cache.finish_frame()
    host.clip(None)
    host.present()
