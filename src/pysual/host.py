"""The only platform substitution boundary; no concrete controls or runtime."""

import re
from dataclasses import dataclass
from math import isfinite
from typing import TYPE_CHECKING, Literal, Protocol

from .errors import PysualError
from .geometry import Rect

if TYPE_CHECKING:
    from .theme import Style


class CapabilityError(PysualError):
    pass


@dataclass(frozen=True)
class TextFile:
    """UTF-8 file content. Location is absent for browser imports/exports."""

    name: str
    text: str
    location: str | None = None


@dataclass(frozen=True)
class RenderDiagnostics:
    """One opening's counters and host identity, sampled without requesting paint.

    Native presentations are optional and do not measure physical screen refresh.
    Unknown host facts and native counters that were not sampled are None.
    """

    scene_submissions: int = 0
    native_presentations: int | None = None
    host_name: str | None = None
    backend: str | None = None
    web_execution: str | None = None
    terminal_renderer: str | None = None
    hidden: bool | None = None


MAX_TEXT_BYTES = 8 * 1024 * 1024
MAX_SESSION_BYTES = 8 * 1024 * 1024


def validate_session_key(key: str) -> None:
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key) is None:
        raise ValueError(
            "Session keys use 1–128 ASCII letters, digits, dots, underscores or hyphens"
        )


def validate_session_text(text: str | None) -> None:
    if text is not None and (
        not isinstance(text, str) or len(text.encode("utf-8")) > MAX_SESSION_BYTES
    ):
        raise ValueError("Session text must be UTF-8 and at most 8 MiB")


@dataclass(frozen=True)
class Viewport:
    """Host facts in logical units; safe-area order is top, right, bottom, left.

    Keyboard occlusion is measured from the viewport bottom. It overlaps the
    bottom safe area, so usable space subtracts the larger of the two values.
    """

    width: float
    height: float
    scale: float = 1.0
    safe_area: tuple[float, float, float, float] = (0, 0, 0, 0)
    keyboard_occlusion: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.safe_area, tuple) or len(self.safe_area) != 4:
            raise ValueError("safe_area must be a tuple of four logical insets")
        values = (
            self.width,
            self.height,
            self.scale,
            *self.safe_area,
            self.keyboard_occlusion,
        )
        if any(
            type(value) not in (int, float) or not isfinite(value) or value < 0
            for value in values
        ):
            raise ValueError(
                "Viewport dimensions and insets must be finite and nonnegative"
            )
        if self.scale <= 0:
            raise ValueError("Viewport scale must be positive")

    @property
    def content_bounds(self) -> Rect:
        top, right, bottom, left = self.safe_area
        return Rect(
            min(left, self.width),
            min(top, self.height),
            max(0, self.width - left - right),
            max(0, self.height - top - max(bottom, self.keyboard_occlusion)),
        )


@dataclass(frozen=True)
class Input:
    """Normalized host input in logical coordinates.

    A positive wheel delta scrolls down. One unit is one SDL wheel step or
    100 browser CSS pixels; browser lines use 16 pixels and pages use viewport
    height. Fractional steps survive normalization. Shift selects the existing
    horizontal-scroll gesture; a native horizontal wheel axis is not exposed.
    Text events marked ``paste`` form a separate undo step; ordinary text may
    contain several characters from one host poll without becoming a paste.
    """

    kind: str
    x: float = 0
    y: float = 0
    key: str = ""
    text: str = ""
    delta: float = 0
    shift: bool = False
    ctrl: bool = False
    button: int = 1
    pointer_id: int = 0
    pointer_kind: str = "mouse"
    viewport: Viewport | None = None
    paste: bool = False


def text_row_height(host: object, fallback: float) -> float:
    """Resolve an optional fixed text-row advance in logical units.

    Fixed-cell hosts expose a positive ``text_row_height`` so built-in and
    custom controls can share one advance for painting, picking and scrolling.
    Pixel hosts omit the hint and retain the control's requested spacing.
    """
    height = getattr(host, "text_row_height", None)
    if (
        isinstance(height, (int, float))
        and not isinstance(height, bool)
        and isfinite(height)
        and height > 0
    ):
        return float(height)
    return fallback


class StyledRectHost(Protocol):
    """Optional surface lowering before shared pixel effects expand.

    Semantic hooks receive absolute logical coordinates and honor the current
    clip. Return True after handling the whole operation; False keeps the shared
    primitive fallback. Hosts can implement any subset of these hook protocols.
    """

    def styled_rect(self, rect: Rect, style: "Style") -> bool: ...


class IconHost(Protocol):
    """Optional representation of a validated catalog icon."""

    def icon(self, name: str, x: float, y: float, size: float, color: str) -> bool: ...


class MarkerHost(Protocol):
    """Optional explicit choice indicator or circular thumb representation."""

    def marker(
        self, rect: Rect, style: "Style", *,
        shape: Literal["square", "circle"], checked: bool,
    ) -> bool: ...


class CaretHost(Protocol):
    """Optional insertion annotation that retains the underlying text."""

    def caret(self, x: float, y: float, height: float, color: str) -> bool: ...


class FocusRingHost(Protocol):
    """Optional focus indication around the full control bounds."""

    def focus_ring(self, rect: Rect, color: str, radius: float = 0) -> bool: ...


class Host(Protocol):
    capabilities: frozenset[str]
    size: tuple[float, float]
    # Optional diagnostic identity: backend, diagnostic_name, web_execution,
    # renderer (terminal only), and hidden. Wrappers should forward these facts.
    # native_retained hosts provide explicit native_stats() JSON snapshots;
    # reading basic Window diagnostics does not call that optional operation.

    @property
    def viewport(self) -> Viewport: ...

    def open(
        self,
        title: str,
        width: float,
        height: float,
        resizable: bool,
        scale: float | None,
    ) -> None: ...
    def poll(self) -> list[Input]: ...
    def set_title(self, title: str) -> None: ...
    def set_size(self, width: float, height: float) -> None: ...
    def begin(self, background: str) -> None: ...
    def clip(self, rect: Rect | None) -> None: ...
    # Optional fixed-grid layout hint (outside the required Host protocol):
    # text_row_height is a positive logical row advance. Controls can use the
    # text_row_height(host, fallback) helper for shared row geometry.
    # Optional semantic operations are described by StyledRectHost, IconHost,
    # MarkerHost, CaretHost and FocusRingHost, outside the required Host protocol.
    def rect(
        self,
        rect: Rect,
        fill: str,
        radius: float = 0,
        border: str = "",
        border_width: float = 1,
    ) -> None: ...
    def text(
        self, text: str, x: float, y: float, color: str, size: float, mono: bool = False
    ) -> None: ...
    def measure(
        self, text: str, size: float, mono: bool = False
    ) -> tuple[float, float]: ...
    def line(
        self, x1: float, y1: float, x2: float, y2: float, color: str, width: float = 1
    ) -> None: ...
    # Optional "shadow_masks" capability opts into generated PNG data images
    # for native shadow compositing. Hosts retain ownership of their bounded
    # decoded image cache. Other hosts use the shared rounded-ring fallback.
    # Optional "image_fit" adds keyword fit="contain"/"cover" to image().
    # Default stretch keeps the two-argument contract for third-party hosts.
    def image(self, source: str, rect: Rect) -> None: ...
    # Optional reload_image(source) discards that source's successful/failed
    # loads and increments resource_revision so shared cached paint rebuilds.
    # Pending decodes must not publish a result started before the reload.

    # Optional when "render_surfaces" is advertised. Handles stay host-owned.
    # A host may also provide surface_byte_size(rect) -> int to include padding
    # or another pixel storage format in admission/accounting. Without it the
    # cache uses the enclosing device-pixel rectangle at four bytes per pixel.
    # Optional surface_matches(surface) -> bool checks destination-dependent
    # paint (e.g. terminal cells composed over a backdrop). False requires
    # repainting; hosts with independent alpha surfaces need no such hook.
    @property
    def render_scale(self) -> float: ...

    @property
    def resource_revision(self) -> int: ...

    def surface_create(self, rect: Rect) -> object: ...
    # Reset clipping and prepare the full target before every build. Independent
    # surfaces clear pixels; destination-dependent ones capture their backdrop.
    def surface_begin(self, surface: object) -> None: ...
    def surface_end(self) -> None: ...
    def surface_blit(self, surface: object) -> None: ...
    def surface_release(self, surface: object) -> None: ...
    # Optional "isolated_paint" capability for cache_paint=True bodies:
    # paint_begin(visible_effect_clip)
    # returns whether a body scope was started. For a started scope,
    # paint_end(completed) restores the target in all cases and composites only
    # when completed=True. Cached target builds can return False (already isolated).
    def present(self) -> None: ...
    def close(self) -> None: ...
    async def clipboard_read(self) -> str: ...
    async def clipboard_write(self, text: str) -> None: ...
    def text_input(self, rect: Rect | None) -> None: ...
    async def open_text_file(self) -> TextFile | None: ...
    async def save_text_file(
        self, text: str, suggested_name: str, location: str | None
    ) -> TextFile | None: ...
    async def read_session(self, key: str) -> str | None: ...
    async def write_session(self, key: str, text: str | None) -> None: ...
    async def open_url(self, url: str) -> None: ...
