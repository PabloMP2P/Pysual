"""Shared, bounded cache of control paint results; hosts only own surfaces.

A control opts in with cache_paint=True. Its own body is cached (never its
children), after two consecutive observations of an unchanged paint key.
"""

from collections import OrderedDict
from dataclasses import asdict, dataclass
from math import ceil, floor
from time import perf_counter
from weakref import ref


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


class RenderCache:
    def __init__(self, host, budget=32 * 1024 * 1024, max_entries=8192):
        self.host, self.budget, self.max_entries = host, budget, max_entries
        self.entries = OrderedDict()
        self.pending = OrderedDict()
        self._counts = asdict(CacheStats())
        self._seen = set()
        # Cell hosts compose paint with existing text/colours. Unlike an alpha
        # texture, their cached result is valid only over the recorded backdrop.
        self._surface_matches = getattr(host, "surface_matches", None)

    @property
    def stats(self):
        return CacheStats(**{**self._counts, "entries": len(self.entries)})

    def _count(self, **changes):
        for key, value in changes.items():
            self._counts[key] += value

    def configure(self, budget):
        self.budget = budget
        while self.entries and self._counts["retained_bytes"] > budget:
            self.drop(next(iter(self.entries)), eviction=True)
        if not budget:
            self.pending.clear()

    def drop(self, owner, *, eviction=False):
        self.pending.pop(owner, None)
        entry = self.entries.pop(owner, None)
        if entry is not None:
            _, surface, size, _ = entry
            self.host.surface_release(surface)
            self._count(releases=1, retained_bytes=-size, evictions=int(eviction))

    def clear(self):
        for owner in tuple(self.entries):
            self.drop(owner)
        self.pending.clear()

    def start_frame(self):
        self._seen.clear()
        # Runtime constructs the cache before an adaptive host (TerminalHost)
        # opens and selects its renderer. Resolve its optional hook once per
        # frame, after opening, rather than freezing a pre-open absence.
        self._surface_matches = getattr(self.host, "surface_matches", None)

    def finish_frame(self):
        # Invisible/detached controls must not retain native surfaces indefinitely.
        for owner in tuple(self.entries):
            if owner not in self._seen:
                self.drop(owner)
        for owner in tuple(self.pending):
            if owner not in self._seen:
                self.pending.pop(owner, None)

    def paint_key(self, control, focus, theme):
        """Cheap inputs to effect bounds, before constructing a Painter.

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
        return (
            control._paint_revision,
            control._rect,
            control._clip,
            control._paint_clip,
            theme,
            control._committed_enabled(),
            control._hover,
            control._pressed,
            control is focus,
            self.host.render_scale,
            self.host.resource_revision,
        )

    def reuse(self, control, key):
        """Blit an unchanged body without preparing its styles or effects."""
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

    def _blit(self, surface):
        started = perf_counter()
        # The target was clipped while built; its transparent padding is already
        # in the pixels. A per-body scissor only breaks consecutive texture draws.
        self.host.clip(None)
        self.host.surface_blit(surface)
        self._counts["hits"] += 1
        self._counts["reuse_seconds"] += perf_counter() - started

    def paint(self, control, focus, draw, *, clip=None, prepared_key=None):
        if (
            not self.budget
            or not control._values["cache_paint"]
            or "render_surfaces" not in self.host.capabilities
        ):
            self._count(bypasses=1)
            draw()
            return
        owner = ref(control)
        self._seen.add(owner)
        clip = control._clip if clip is None else clip
        if prepared_key is None:
            scale = self.host.render_scale
            key = (
                control._paint_revision,
                control._rect,
                clip,
                control._clip,
                control._fast_theme(),
                control._committed_enabled(),
                control._hover,
                control._pressed,
                control is focus,
                scale,
                self.host.resource_revision,
            )
        else:
            # Misses use the already-observed inherited state too; otherwise
            # preflight would add another ancestor walk to animated painting.
            scale = prepared_key[-2]
            # Effect bounds size the surface; the body clip also determines
            # its pixels and can change while shadow bounds stay unchanged.
            key = (*prepared_key[:2], clip, prepared_key[2], *prepared_key[4:])
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
        elif estimate := getattr(self.host, "surface_byte_size", None):
            size = estimate(clip)
        else:
            size = (
                max(0, ceil(clip.right * scale) - floor(clip.x * scale))
                * max(0, ceil(clip.bottom * scale) - floor(clip.y * scale))
                * 4
            )
        if not size or size > self.budget:
            self.drop(owner)
            self._count(bypasses=1)
            draw()
            return
        self._count(misses=1)
        previous = self.pending.get(owner)
        if entry is not None and not reusable:
            self.drop(owner)
            entry = None
        # Admission avoids allocating a surface for a continuously changing body.
        if owner not in self.pending and len(self.pending) >= self.max_entries:
            # Keep the first candidates long enough to observe them next frame.
            # Cycling all visible controls through an LRU here starved admission
            # whenever a scene contained more controls than the metadata bound.
            self._count(bypasses=1)
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
            or self._counts["retained_bytes"] + size > self.budget
        ):
            # Retain resident visible bodies rather than thrashing them with
            # newly admitted bodies. finish_frame releases absent residents.
            self._count(bypasses=1)
            draw()
            return
        if entry is None:
            try:
                surface = self.host.surface_create(clip)
            except MemoryError:
                self._count(bypasses=1)
                draw()
                return
            self._count(allocations=1)
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
                self._count(releases=1)
                self.pending.pop(owner, None)
            else:
                self.drop(owner)
            raise
        self.entries[owner] = (key, surface, size, prepared_key)
        self.entries.move_to_end(owner)
        self.pending.pop(owner, None)
        # No strong reference to the control; explicit destroy releases immediately.
        control._release_paint = lambda: self.drop(owner)
        self._count(
            rebuilds=1,
            retained_bytes=size if entry is None else 0,
            build_seconds=perf_counter() - started,
        )
        self.host.clip(None)
        self.host.surface_blit(surface)
