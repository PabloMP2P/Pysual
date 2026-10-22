"""The only platform substitution boundary; no concrete controls or runtime."""

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast, overload

from .errors import PysualError
from .geometry import Rect

if TYPE_CHECKING:
    from .theme import ResolvedStyle, Style


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


_ILLEGAL_FILENAME_CHARACTERS = frozenset('/\\:*?"<>|')


def _control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


_EXTENSION = re.compile(r"\.[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*\Z")


@dataclass(frozen=True)
class PathDialog:
    """Validated options for one system open, save, or folder dialog."""

    save: bool
    directory: bool
    suggested_name: str
    title: str
    initial_directory: str | None
    extensions: tuple[str, ...] = ()
    all_files: bool = False


def _normalize_extension(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("extensions must be file suffixes such as '.txt' or '.md'")
    text = value.strip().lower()
    if text.startswith("*"):
        text = text[1:]
    if text and not text.startswith("."):
        text = "." + text
    if not text or len(text) > 32 or _EXTENSION.fullmatch(text) is None:
        raise ValueError("extensions must be file suffixes such as '.txt' or '.md'")
    return text


def _normalize_extensions(extensions: object) -> tuple[str, ...]:
    if isinstance(extensions, str) or not isinstance(extensions, Sequence):
        raise ValueError("extensions must be file suffixes such as '.txt' or '.md'")
    values = cast(Sequence[object], extensions)
    if len(values) > 32:
        raise ValueError("extensions accept at most 32 suffixes")
    unique: list[str] = []
    for item in values:
        suffix = _normalize_extension(item)
        if suffix not in unique:
            unique.append(suffix)
    return tuple(unique)


def matches_extension(name: str, extensions: tuple[str, ...]) -> bool:
    """True when ``name`` has one of the suffixes, or no suffixes were requested."""
    if not extensions:
        return True
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in extensions)


def ensure_allowed_extension(
    path: Path, *, save: bool, extensions: tuple[str, ...], all_files: bool = False
) -> Path:
    """Keep a matching name. A bare save name receives the first suffix."""
    if all_files or matches_extension(path.name, extensions):
        return path
    if save and "." not in path.name:
        return path.with_name(path.name + extensions[0])
    raise ValueError("Choose a file ending in " + ", ".join(extensions))


def prepare_path_dialog(
    *,
    save: bool = False,
    directory: bool = False,
    suggested_name: str = "",
    title: str = "",
    initial_directory: str | None = None,
    extensions: Sequence[str] = (),
    all_files: bool = False,
) -> PathDialog:
    """Reject dialog options that cannot be shown safely on every desktop."""
    if type(save) is not bool or type(directory) is not bool or type(all_files) is not bool:
        raise ValueError("save, directory, and all_files must be booleans")
    if (
        not isinstance(suggested_name, str)
        or len(suggested_name) > 255
        or (
            bool(suggested_name)
            and (
                suggested_name in (".", "..")
                or not suggested_name.strip()
                or suggested_name.endswith((" ", "."))
                or _control_character(suggested_name)
                or any(
                    character in _ILLEGAL_FILENAME_CHARACTERS
                    for character in suggested_name
                )
            )
        )
    ):
        raise ValueError(
            "suggested_name must be a file name, without directories or reserved characters"
        )
    if not isinstance(title, str) or len(title) > 120 or _control_character(title):
        raise ValueError("title must be at most 120 characters without control characters")
    initial: str | None = None
    if initial_directory is not None:
        if (
            not isinstance(initial_directory, str)
            or not initial_directory.strip()
            or "\0" in initial_directory
            or (os.name == "nt" and _control_character(initial_directory))
            or len(initial_directory) > 32767
        ):
            raise ValueError("initial_directory must be an existing folder")
        candidate = Path(initial_directory).expanduser()
        try:
            is_directory = candidate.is_dir()
        except (OSError, ValueError) as exc:
            raise ValueError("initial_directory must be an existing folder") from exc
        if not is_directory:
            raise ValueError("initial_directory must be an existing folder")
        initial = os.path.abspath(candidate)
        if len(initial) > 32767:
            raise ValueError("initial_directory must be an existing folder")
    suffixes = _normalize_extensions(extensions)
    if directory and suffixes:
        raise ValueError("extensions apply to file dialogs, not folder selection")
    if directory and all_files:
        raise ValueError("all_files applies to file dialogs, not folder selection")
    if (
        suggested_name
        and suffixes
        and not all_files
        and "." in suggested_name
        and not matches_extension(suggested_name, suffixes)
    ):
        raise ValueError("suggested_name must use one of the allowed extensions")
    return PathDialog(save, directory, suggested_name, title, initial, suffixes, all_files)


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
        try:
            valid = all(
                type(value) in (int, float) and isfinite(value) and value >= 0
                for value in values
            )
        except OverflowError:
            valid = False
        if not valid:
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
    Retained hosts may receive ``effect_clip``; other hosts are called without it.
    """

    def styled_rect(
        self, rect: Rect, style: "Style | ResolvedStyle", *, effect_clip: Rect | None = None,
    ) -> bool: ...


class IconHost(Protocol):
    """Optional representation of a validated catalog icon."""

    def icon(self, name: str, x: float, y: float, size: float, color: str) -> bool: ...


class MarkerHost(Protocol):
    """Optional explicit choice indicator or circular thumb representation.

    Retained hosts may receive ``effect_clip``. Other hosts are called without it.
    """

    def marker(
        self, rect: Rect, style: "Style | ResolvedStyle", *,
        shape: Literal["square", "circle"], checked: bool,
        effect_clip: Rect | None = None,
    ) -> bool: ...


class CaretHost(Protocol):
    """Optional insertion annotation that retains the underlying text."""

    def caret(self, x: float, y: float, height: float, color: str) -> bool: ...


class FocusRingHost(Protocol):
    """Optional focus indication around the full control bounds."""

    def focus_ring(self, rect: Rect, color: str, radius: float = 0) -> bool: ...


class MeasureManyHost(Protocol):
    """Optional batch text measurement. Absent hosts measure one string at a time."""

    def measure_many(
        self, texts: Iterable[str], size: float, mono: bool = False
    ) -> Iterable[tuple[float, float]]: ...


class LineBatchHost(Protocol):
    """Optional connected-stroke batch. Absent hosts draw each segment with line()."""

    def lines(
        self, points: Sequence[tuple[float, float]], color: str, width: float = 1
    ) -> None: ...


class SegmentBatchHost(Protocol):
    """Optional independent-stroke batch."""

    def segments(
        self,
        segments: Iterable[tuple[float, float, float, float]],
        color: str,
        width: float = 1,
    ) -> None: ...


class GradientHost(Protocol):
    """Optional single-call gradient. Return True when the host painted it."""

    def gradient_rect(
        self,
        rect: Rect,
        first: str,
        last: str,
        axis: Literal["vertical", "horizontal"],
        radius: float,
        border_width: float = 0,
    ) -> bool: ...


class ImageFitHost(Protocol):
    """image() with fit, present only when the image_fit capability is advertised.

    Every host already has the two-argument image(), so this is not detected
    with isinstance. The capability is the runtime proof.
    """

    def image(
        self,
        source: str,
        rect: Rect,
        *,
        fit: Literal["stretch", "contain", "cover"] = "stretch",
    ) -> None: ...


class SpriteHost(Protocol):
    """Optional native sprite-sheet playback."""

    def sprite(
        self,
        source: str,
        rect: Rect,
        *,
        frame_width: float,
        frame_height: float,
        frame_count: int,
        fps: float = 12,
        loop: bool = True,
        tint: str = "#ffffff",
    ) -> None: ...


class AnimatedRectHost(Protocol):
    """Optional native interpolation of a rectangle."""

    def animated_rect(
        self,
        rect: Rect,
        fill: str,
        *,
        radius: float = 0,
        border: str = "",
        border_width: float = 1,
        property: Literal["x", "y", "opacity"] = "x",
        from_value: float,
        to_value: float,
        duration: float = 1,
        loop: bool = True,
        yoyo: bool = True,
    ) -> None: ...


class NativeStatsHost(Protocol):
    """Explicit presentation counters for a native_retained host."""

    def native_stats(self) -> Mapping[str, Any]: ...


class RenderingHost(Protocol):
    """Optional rendering policy. Absent hosts keep their own defaults."""

    def configure_rendering(
        self,
        *,
        redraw_interval: float | None,
        fps_limit: int | float | None,
        reduce_motion: bool,
        vsync: bool | None = None,
    ) -> None: ...


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
    # Optional metrics_revision separates preferred-size changes (fonts, DPI,
    # metric resets) from image-only resource changes. Without it layout uses
    # resource_revision as before. Advance metrics_revision for every metric
    # change; advance resource_revision as well so cached painting refreshes.
    # Custom controls remain resource-dependent unless they explicitly opt in
    # with measure_resources=False. Paint caches always use resource_revision.

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
    # Optional event_driven=True and set_wake_callback(callback) allow the
    # runtime to sleep until input/resources arrive. Deliver callbacks on the
    # registering asyncio loop and release them on close. Optional
    # next_poll_delay is seconds until housekeeping (infinity by default);
    # wake when a new earlier deadline is established. Hosts without this
    # contract retain polling for geometry, Escape parsing and image completion.
    def present(self) -> None: ...
    def close(self) -> None: ...
    async def clipboard_read(self) -> str: ...
    async def clipboard_write(self, text: str) -> None: ...
    def text_input(self, rect: Rect | None) -> None: ...
    async def open_text_file(self) -> TextFile | None: ...
    async def save_text_file(
        self, text: str, suggested_name: str, location: str | None
    ) -> TextFile | None: ...
    # Optional "path_dialogs". None means the user cancelled. A packaged
    # browser does not advertise it: the page cannot reveal a filesystem path.
    async def choose_path(
        self,
        *,
        save: bool = False,
        directory: bool = False,
        suggested_name: str = "",
        title: str = "",
        initial_directory: str | None = None,
        extensions: Sequence[str] = (),
        all_files: bool = False,
    ) -> str | None: ...
    async def read_session(self, key: str) -> str | None: ...
    async def write_session(self, key: str, text: str | None) -> None: ...
    async def open_url(self, url: str) -> None: ...


_OPTIONAL_HOST_METHODS = frozenset({
    "animated_rect",
    "caret",
    "configure_rendering",
    "focus_ring",
    "gradient_rect",
    "icon",
    "lines",
    "marker",
    "measure_many",
    "native_stats",
    "segments",
    "sprite",
    "styled_rect",
})


@overload
def optional_host(host: Host, method: Literal["measure_many"]) -> MeasureManyHost | None: ...
@overload
def optional_host(host: Host, method: Literal["lines"]) -> LineBatchHost | None: ...
@overload
def optional_host(host: Host, method: Literal["segments"]) -> SegmentBatchHost | None: ...
@overload
def optional_host(host: Host, method: Literal["gradient_rect"]) -> GradientHost | None: ...
@overload
def optional_host(host: Host, method: Literal["sprite"]) -> SpriteHost | None: ...
@overload
def optional_host(host: Host, method: Literal["animated_rect"]) -> AnimatedRectHost | None: ...
@overload
def optional_host(host: Host, method: Literal["native_stats"]) -> NativeStatsHost | None: ...
@overload
def optional_host(host: Host, method: Literal["configure_rendering"]) -> RenderingHost | None: ...
@overload
def optional_host(host: Host, method: Literal["styled_rect"]) -> StyledRectHost | None: ...
@overload
def optional_host(host: Host, method: Literal["icon"]) -> IconHost | None: ...
@overload
def optional_host(host: Host, method: Literal["marker"]) -> MarkerHost | None: ...
@overload
def optional_host(host: Host, method: Literal["caret"]) -> CaretHost | None: ...
@overload
def optional_host(host: Host, method: Literal["focus_ring"]) -> FocusRingHost | None: ...


def optional_host(host: Host, method: str) -> Any:
    """Return the host when ``method`` is callable, including through ``__getattr__``.

    Forwarding wrappers do not expose the method to ``isinstance`` on a protocol.
    ``getattr`` follows that forwarding. The method name selects the protocol, so
    a call cannot pair ``lines`` with a segment host. A missing or non-callable
    attribute is absent. An unknown method name is rejected.
    """
    if method not in _OPTIONAL_HOST_METHODS:
        raise ValueError(f"Unknown optional host method {method!r}")
    if not callable(getattr(host, method, None)):
        return None
    return host


def image_fit_host(host: Host) -> ImageFitHost | None:
    """Return the host when it advertises ``image_fit``.

    Every host has the two-argument ``image()``. The capability is the proof
    that the same method accepts ``fit``.
    """
    if "image_fit" not in getattr(host, "capabilities", ()):
        return None
    return cast(ImageFitHost, host)


def read_native_stats(host: Host) -> Mapping[str, Any] | None:
    """Return the retained host's stats snapshot.

    Other hosts return None. A retained host that does not implement
    native_stats() is an incomplete adapter.
    """
    if not getattr(host, "native_retained", False):
        return None
    retained = optional_host(host, "native_stats")
    if retained is None:
        raise TypeError("A native_retained host must provide native_stats()")
    stats = retained.native_stats()
    if not isinstance(stats, Mapping):
        raise TypeError("native_stats must return a mapping")
    return stats


def native_frame_count(stats: Mapping[str, Any]) -> int | None:
    """Return presentation frames, or None when the snapshot omits them.

    Whole numbers are counters. Booleans, fractions and non-finite values are
    not. Callers that require a counter reject a missing key themselves.
    """
    if "frames" not in stats:
        return None
    value = stats["frames"]
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or (isinstance(value, float) and not value.is_integer())
    ):
        raise TypeError("native_stats frames must be a whole finite number")
    return int(value)


def configure_rendering(
    host: Host,
    *,
    redraw_interval: float | None,
    fps_limit: int | float | None,
    reduce_motion: bool,
    vsync: bool | None = None,
) -> None:
    """Apply a rendering policy on a host that advertises configure_rendering."""
    rendering = optional_host(host, "configure_rendering")
    if rendering is None:
        raise CapabilityError("This host does not provide rendering controls")
    rendering.configure_rendering(
        redraw_interval=redraw_interval,
        fps_limit=fps_limit,
        reduce_motion=reduce_motion,
        vsync=vsync,
    )
