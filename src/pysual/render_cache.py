"""Shared, bounded cache of control paint results; hosts only own surfaces.

A control opts in with cache_paint=True. Its own body is cached (never its
children), after two consecutive observations of an unchanged paint key.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil, floor
from time import perf_counter
from typing import cast
from weakref import ReferenceType, ref

from .controls import Control
from .geometry import Rect
from .host import Host
from ._paint_state import PaintInputs, paint_inputs
from .theme import Theme


@dataclass(frozen=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    rebuilds: int = 0
    evictions: int = 0
    bypasses: int = 0
    allocations: int = 0
    releases: int = 0
    retained_bytes: int = 0
    entries: int = 0
    build_seconds: float = 0.0
    reuse_seconds: float = 0.0


PaintKey = PaintInputs
CacheEntry = tuple[PaintKey, object, int, PaintKey | None]
Owner = ReferenceType[Control]


@dataclass(slots=True)
class _Counters:
    """Mutable accounting, separate from the public immutable snapshots."""

    hits: int = 0
    misses: int = 0
    rebuilds: int = 0
    evictions: int = 0
    bypasses: int = 0
    allocations: int = 0
    releases: int = 0
    retained_bytes: int = 0
    build_seconds: float = 0.0
    reuse_seconds: float = 0.0


def _match_hook(host: Host) -> Callable[[object], bool] | None:
    method: object = getattr(host, "surface_matches", None)
    if not callable(method):
        return None
    return cast(Callable[[object], bool], method)


def _byte_size(host: Host, rect: Rect, scale: float) -> int:
    estimate: object = getattr(host, "surface_byte_size", None)
    if callable(estimate):
        size = cast(Callable[[Rect], object], estimate)(rect)
        if isinstance(size, bool) or not isinstance(size, int):
            raise TypeError("surface_byte_size must return a nonnegative integer")
        if size < 0:
            raise ValueError("surface_byte_size must return a nonnegative integer")
        return size
    return (
        max(0, ceil(rect.right * scale) - floor(rect.x * scale))
        * max(0, ceil(rect.bottom * scale) - floor(rect.y * scale))
        * 4
    )


class RenderCache:
    def __init__(
        self, host: Host, budget: int = 32 * 1024 * 1024, max_entries: int = 8192
    ) -> None:
        self.host = host
        self.budget = budget
        self.max_entries = max_entries
        self.entries: OrderedDict[Owner, CacheEntry] = OrderedDict()
        self.pending: OrderedDict[Owner, PaintKey] = OrderedDict()
        self._counts = _Counters()
        self._seen: set[Owner] = set()
        # Cell hosts compose paint with existing text/colours. Unlike an alpha
        # texture, their cached result is valid only over the recorded backdrop.
        self._surface_matches = _match_hook(host)

    @property
    def stats(self) -> CacheStats:
        counts = self._counts
        return CacheStats(
            hits=counts.hits,
            misses=counts.misses,
            rebuilds=counts.rebuilds,
            evictions=counts.evictions,
            bypasses=counts.bypasses,
            allocations=counts.allocations,
            releases=counts.releases,
            retained_bytes=counts.retained_bytes,
            entries=len(self.entries),
            build_seconds=counts.build_seconds,
            reuse_seconds=counts.reuse_seconds,
        )

    def configure(self, budget: int) -> None:
        self.budget = budget
        while self.entries and self._counts.retained_bytes > budget:
            self.drop(next(iter(self.entries)), eviction=True)
        if not budget:
            self.pending.clear()

    def drop(self, owner: Owner, *, eviction: bool = False) -> None:
        self.pending.pop(owner, None)
        entry = self.entries.pop(owner, None)
        if entry is not None:
            _, surface, size, _ = entry
            self.host.surface_release(surface)
            self._counts.releases += 1
            self._counts.retained_bytes -= size
            self._counts.evictions += int(eviction)

    def clear(self) -> None:
        for owner in tuple(self.entries):
            self.drop(owner)
        self.pending.clear()

    def start_frame(self) -> None:
        self._seen.clear()
        # Runtime constructs the cache before an adaptive host (TerminalHost)
        # opens and selects its renderer. Resolve its optional hook once per
        # frame, after opening, rather than freezing a pre-open absence.
        self._surface_matches = _match_hook(self.host)

    def finish_frame(self) -> None:
        # Invisible/detached controls must not retain native surfaces indefinitely.
        for owner in tuple(self.entries):
            if owner not in self._seen:
                self.drop(owner)
        for owner in tuple(self.pending):
            if owner not in self._seen:
                self.pending.pop(owner, None)

    def paint_key(
        self, control: Control, focus: Control | None, theme: Theme
    ) -> PaintKey | None:
        """Cheap inputs to effect bounds, before constructing a Painter.

        Known body inputs let unchanged built-ins share retained-scene reuse.
        Custom paint_bounds hooks follow the same invalidate() contract as
        custom paint hooks. Include both layout clips: a parent's effect clip
        can change while the child's body remains entirely inside it.
        """
        if (
            not self.budget
            or not control._values["cache_paint"]
            or "render_surfaces" not in self.host.capabilities
        ):
            return None
        entry = self.entries.get(ref(control))
        return paint_inputs(control, self.host, focus, theme,
                            previous=None if entry is None else entry[3])

    def reuse(self, control: Control, key: PaintKey | None) -> bool:
        """Blit an unchanged body without preparing its styles or effects."""
        if key is None:
            return False
        owner = ref(control)
        entry = self.entries.get(owner)
        if entry is None or entry[3] != key or entry[2] > self.budget:
            return False
        if self._surface_matches is not None and not self._surface_matches(entry[1]):
            return False
        self._seen.add(owner)
        self.entries.move_to_end(owner)
        self.pending.pop(owner, None)
        self._blit(entry[1])
        return True

    def _blit(self, surface: object) -> None:
        started = perf_counter()
        # The target was clipped while built; its transparent padding is already
        # in the pixels. A per-body scissor only breaks consecutive texture draws.
        self.host.clip(None)
        self.host.surface_blit(surface)
        self._counts.hits += 1
        self._counts.reuse_seconds += perf_counter() - started

    def paint(
        self,
        control: Control,
        focus: Control | None,
        draw: Callable[[], None],
        *,
        clip: Rect | None = None,
        prepared_key: PaintKey | None = None,
    ) -> None:
        if (
            not self.budget
            or not control._values["cache_paint"]
            or "render_surfaces" not in self.host.capabilities
        ):
            self._counts.bypasses += 1
            draw()
            return
        owner = ref(control)
        self._seen.add(owner)
        clip = control._clip if clip is None else clip
        if prepared_key is None:
            # Public callers may supply an arbitrary draw callback, even for a
            # built-in control. Only the tree's prepared body contract permits
            # value-based reuse; this callback retains revision invalidation.
            scale = self.host.render_scale
            key = PaintInputs(
                control._paint_revision, control._rect, clip, control._clip,
                control._fast_theme(), control._committed_enabled(),
                control._hover, control._pressed, control is focus, None,
                scale, self.host.resource_revision,
            )
        else:
            # Misses use the already-observed inherited state too; otherwise
            # preflight would add another ancestor walk to animated painting.
            scale = prepared_key[-2]
            # Effect bounds size the surface; the body clip also determines
            # its pixels and can change while shadow bounds stay unchanged.
            key = prepared_key._replace(clip=clip, paint_clip=prepared_key.clip)
        entry = self.entries.get(owner)
        if (entry is not None and entry[0] == key and entry[2] <= self.budget
                and (self._surface_matches is None or self._surface_matches(entry[1]))):
            self.entries.move_to_end(owner)
            self.pending.pop(owner, None)
            self.entries[owner] = (*entry[:3], prepared_key)
            self._blit(entry[1])
            return
        # An unchanged key includes the clip and scale, so hits can reuse the
        # resident byte size without repeating pixel rounding for every control.
        reusable = (
            entry is not None
            and entry[0][1:3] == key[1:3]
            and entry[0][-2:] == key[-2:]
        )
        if entry is not None and reusable:
            size = entry[2]
        else:
            size = _byte_size(self.host, clip, scale)
        if not size or size > self.budget:
            self.drop(owner)
            self._counts.bypasses += 1
            draw()
            return
        self._counts.misses += 1
        previous = self.pending.get(owner)
        if entry is not None and not reusable:
            self.drop(owner)
            entry = None
        # Admission avoids allocating a surface for a continuously changing body.
        if owner not in self.pending and len(self.pending) >= self.max_entries:
            # Keep the first candidates long enough to observe them next frame.
            # Cycling all visible controls through an LRU here starved admission
            # whenever a scene contained more controls than the metadata bound.
            self._counts.bypasses += 1
            draw()
            return
        self.pending[owner] = key
        if previous != key:
            # Keep compatible storage, never its stale pixels. A body that keeps
            # changing still paints directly; a stable-again body can rebuild
            # without allocating and destroying the same native target again.
            draw()
            return
        if entry is None and (
            len(self.entries) >= self.max_entries
            or self._counts.retained_bytes + size > self.budget
        ):
            # Retain resident visible bodies rather than thrashing them with
            # newly admitted bodies. finish_frame releases absent residents.
            self._counts.bypasses += 1
            draw()
            return
        if entry is None:
            try:
                surface = self.host.surface_create(clip)
            except MemoryError:
                self._counts.bypasses += 1
                draw()
                return
            self._counts.allocations += 1
        else:
            surface = entry[1]
        started = perf_counter()
        try:
            # surface_begin prepares the entire target, including a prior clip.
            self.host.surface_begin(surface)
            try:
                self.host.clip(clip)
                draw()
            finally:
                self.host.surface_end()
        except BaseException:
            if entry is None:
                self.host.surface_release(surface)
                self._counts.releases += 1
                self.pending.pop(owner, None)
            else:
                self.drop(owner)
            raise
        self.entries[owner] = (key, surface, size, prepared_key)
        self.entries.move_to_end(owner)
        self.pending.pop(owner, None)
        # No strong reference to the control; explicit destroy releases immediately.
        control._release_paint = lambda: self.drop(owner)
        self._counts.rebuilds += 1
        self._counts.retained_bytes += size if entry is None else 0
        self._counts.build_seconds += perf_counter() - started
        self.host.clip(None)
        self.host.surface_blit(surface)
