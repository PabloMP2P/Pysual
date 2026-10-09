"""Bounded PNG decoding and sampling for character-cell image approximations."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .._nine_slice import slice_regions
from .._png import MAX_ENCODED_BYTES, decode_png, png_source_pixels
from .._image_fit import image_rects


class ImageCache:
    """Own decoded pixels, charging source strings against the same LRU budget."""

    MAX_BYTES = 32 * 1024 * 1024
    MAX_ENTRIES = 64

    def __init__(self):
        self._entries = OrderedDict()
        self.byte_size = 0

    def get(self, source):
        if not isinstance(source, str):
            raise TypeError("Image sources must be strings")
        if source in self._entries:
            self._entries.move_to_end(source)
            return self._entries[source][0]
        result = self._decode(source)
        self._store(source, result)
        return result

    @staticmethod
    def _decode(source):
        if source.startswith("data:"):
            return png_source_pixels(source)
        if "://" in source:
            raise ValueError("Text images require a local PNG or PNG data URI")
        with Path(source).open("rb") as stream:
            return decode_png(stream.read(MAX_ENCODED_BYTES + 1))

    def _store(self, source, result):
        # Four bytes per character also bounds non-ASCII native path strings.
        size = len(result[2]) + len(source) * 4
        if size <= self.MAX_BYTES and self.MAX_ENTRIES > 0:
            while self._entries and (
                len(self._entries) >= self.MAX_ENTRIES
                or self.byte_size + size > self.MAX_BYTES
            ):
                _, (_, old_size) = self._entries.popitem(last=False)
                self.byte_size -= old_size
            self._entries[source] = result, size
            self.byte_size += size
            return True
        return False

    def clear(self):
        self._entries.clear()
        self.byte_size = 0

    def invalidate(self, source):
        entry = self._entries.pop(source, None)
        if entry is not None:
            self.byte_size -= entry[1]

    def close(self):
        self.clear()


class AsyncImageCache(ImageCache):
    """Owner-published image loads, with one decode and no queued backlog.

    ``get`` returns None while cold; the terminal polls completion before paint.
    Plain ImageCache and standalone CellRenderer remain synchronous.
    If the visible working set pressures the cache, use the original synchronous
    LRU for this opening instead of repeatedly evicting/loading placeholders.
    """

    MAX_SOURCE_CHARS = ((MAX_ENCODED_BYTES + 2) // 3) * 4 + 128

    def __init__(self):
        super().__init__()
        self._executor = None
        self._pending = None
        self._pending_invalidated = False
        self._failures = OrderedDict()
        self._failure_bytes = 0
        # One valid image may exceed the LRU charge once its source is included.
        # Keep that completion usable instead of repeatedly showing a placeholder.
        self._uncached = None
        self._closed = False
        self._synchronous = False

    def get(self, source):
        if not isinstance(source, str):
            raise TypeError("Image sources must be strings")
        if len(source) > self.MAX_SOURCE_CHARS:
            raise ValueError("Image source exceeds the PNG input budget")
        if source in self._entries:
            self._entries.move_to_end(source)
            return self._entries[source][0]
        if self._uncached is not None and self._uncached[0] == source:
            result, error = self._uncached[1:]
            if error is not None:
                raise ValueError(error)
            return result
        if source in self._failures:
            self._failures.move_to_end(source)
            raise ValueError(self._failures[source])
        if self._synchronous and not self._closed:
            return super().get(source)
        if not self._closed and self._pending is None:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pysual-image")
            # Submit the pure decoder, never an object which owns UI state.
            self._pending = source, self._executor.submit(self._decode, source)
        return None

    def poll(self):
        """Publish at most one result on the owner; return whether to repaint."""
        if self._closed or self._pending is None or not self._pending[1].done():
            return False
        source, future = self._pending
        self._pending = None
        if self._pending_invalidated:
            # Keep one worker and no queued backlog while an old decode runs.
            # Its completion only wakes painting to request the fresh source.
            self._pending_invalidated = False
            return True
        try:
            result = future.result()
        except (OSError, ValueError) as exc:
            message = str(exc)[:512]
            size = len(source) * 4 + len(message) * 4
            if (size + self._failure_bytes > self.MAX_BYTES or
                    len(self._failures) >= self.MAX_ENTRIES):
                self._use_synchronous_cache()
                self._uncached = source, None, message
            else:
                self._failures[source] = message
                self._failure_bytes += size
        else:
            size = len(result[2]) + len(source) * 4
            if (size + self.byte_size > self.MAX_BYTES or
                    len(self._entries) >= self.MAX_ENTRIES):
                self._use_synchronous_cache()
            if not self._store(source, result):
                self._uncached = source, result, None
        return True

    def _use_synchronous_cache(self):
        self._synchronous = True
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def finish_frame(self):
        # A result too large for the LRU is available during its first repaint,
        # then follows the existing synchronous uncached path. There is never
        # both a spare completion and another background job.
        self._uncached = None

    def invalidate(self, source):
        super().invalidate(source)
        message = self._failures.pop(source, None)
        if message is not None:
            self._failure_bytes -= len(source) * 4 + len(message) * 4
        if self._uncached is not None and self._uncached[0] == source:
            self._uncached = None
        if self._pending is not None and self._pending[0] == source:
            self._pending_invalidated = True
            self._pending[1].cancel()

    def clear(self):
        # No completion callback can repopulate a closed cache. A new opening
        # gets another instance; running decoding only retains its own input.
        if self._pending is not None:
            self._pending[1].cancel()
            self._pending = None
        self._pending_invalidated = False
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None
        self._failures.clear()
        self._failure_bytes = 0
        self._uncached = None
        self._closed = False
        self._synchronous = False
        super().clear()

    def close(self):
        self.clear()
        self._closed = True


def image_cells(image, rect, bounds, cell_width, cell_height, tint, edges=None, *, fit="stretch"):
    """Yield two RGBA samples per visible cell with constant work per cell.

    Four taps per half cell soften downsampling without scanning source-sized
    areas. Average premultiplied colors so transparent RGB cannot create halos.
    Nine-slice regions map sample positions before the final cell is composed.
    """
    width, height, pixels = image
    regions = (
        tuple(slice_regions(width, height, rect.width, rect.height, edges))
        if edges is not None
        else (((0, 0, width, height), (0, 0, rect.width, rect.height)),)
    )
    if edges is None and fit != "stretch":
        crop, destination = image_rects(width, height, rect, fit)
        regions = (((crop.x, crop.y, crop.width, crop.height),
                    (destination.x - rect.x, destination.y - rect.y,
                     destination.width, destination.height)),)

    def sample(x, y):
        # Regions can overlap when the destination is smaller than its edges;
        # the last region wins, matching the painter order of slice_regions.
        for (sx, sy, sw, sh), (dx, dy, dw, dh) in reversed(regions):
            if dx <= x < dx + dw and dy <= y < dy + dh:
                px = min(width - 1, max(0, int(sx + (x - dx) / dw * sw)))
                py = min(height - 1, max(0, int(sy + (y - dy) / dh * sh)))
                offset = (py * width + px) * 4
                return pixels[offset : offset + 4]
        return (0, 0, 0, 0)

    def half(column, row, lower):
        red = green = blue = alpha = 0
        for y in (0.125, 0.375):
            for x in (0.25, 0.75):
                r, g, b, a = sample(
                    (column + x) * cell_width - rect.x,
                    (row + y + lower * 0.5) * cell_height - rect.y,
                )
                red += r * a
                green += g * a
                blue += b * a
                alpha += a
        if not alpha:
            return (0, 0, 0, 0)
        return (
            round(red * tint[0] / (alpha * 255)),
            round(green * tint[1] / (alpha * 255)),
            round(blue * tint[2] / (alpha * 255)),
            round(alpha * tint[3] / (4 * 255)),
        )

    left, top, right, bottom = bounds
    for row in range(top, bottom):
        for column in range(left, right):
            yield column, row, half(column, row, 0), half(column, row, 1)


def luminance_glyph(upper, lower):
    """Use increasing ASCII ink density when the terminal cannot show color."""
    ramp = " .:-=+*#%@"
    luminance = sum(
        channel * weight
        for color in (upper, lower)
        for channel, weight in zip(color, (2126, 7152, 722))
    ) / (2 * 10000 * 255)
    return ramp[round(luminance * (len(ramp) - 1))]
