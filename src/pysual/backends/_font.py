"""Text metrics from the two bundled DejaVu TrueType fonts.

Reads the packaged fonts' Unicode mappings, advances and kerning for SVG layout.
Browsers render the glyphs; this module only measures text and caches metrics.
"""

from bisect import bisect_right
from functools import lru_cache
from importlib import resources
from math import ceil, isfinite
from struct import unpack_from
from unicodedata import normalize


@lru_cache(maxsize=2)
def font_bytes(mono=False):
    return (
        resources.files("pysual")
        .joinpath("assets", "DejaVuSansMono.ttf" if mono else "DejaVuSans.ttf")
        .read_bytes()
    )


class TrueTypeFont:
    """Read-only TrueType data. Input is the packaged, trusted font resource."""

    def __init__(self, data):
        self.data = data
        self.tables = {}
        for index in range(self.u16(4)):
            tag, _, offset, length = unpack_from(">4sIII", data, 12 + index * 16)
            if offset + length > len(data):
                raise ValueError("Truncated TrueType table")
            self.tables[tag] = offset
        head, hhea = self.tables[b"head"], self.tables[b"hhea"]
        self.units = self.u16(head + 18)
        self.ascender, self.descender, self.line_gap = unpack_from(
            ">hhh", data, hhea + 4
        )
        self.glyph_count = self.u16(self.tables[b"maxp"] + 4)
        self.metrics_count = self.u16(hhea + 34)
        self.advances = tuple(
            self.u16(self.tables[b"hmtx"] + min(i, self.metrics_count - 1) * 4)
            for i in range(self.glyph_count)
        )
        self._read_cmap()
        self.kerning = {}
        kern = self.tables.get(b"kern")
        if kern is not None and self.u16(kern) == 0:
            offset = kern + 4
            for _ in range(self.u16(kern + 2)):
                _, length, coverage = unpack_from(">HHH", data, offset)
                if length < 6:
                    break
                # Horizontal, ordinary pair kerning (not minimum/cross-stream).
                if coverage & 0xFF07 == 1:
                    count = self.u16(offset + 6)
                    for i in range(count):
                        left, right, value = unpack_from(
                            ">HHh", data, offset + 14 + i * 6
                        )
                        key = (left, right)
                        self.kerning[key] = (
                            value if coverage & 8 else self.kerning.get(key, 0) + value
                        )
                offset += length

    def u16(self, offset):
        return unpack_from(">H", self.data, offset)[0]

    def _read_cmap(self):
        cmap = self.tables[b"cmap"]
        choices = []
        for index in range(self.u16(cmap + 2)):
            platform, encoding, relative = unpack_from(
                ">HHI", self.data, cmap + 4 + index * 8
            )
            offset = cmap + relative
            kind = self.u16(offset)
            if kind in (4, 12) and (
                platform == 0 or (platform == 3 and encoding in (1, 10))
            ):
                choices.append((kind == 12, platform == 0, offset))
        if not choices:
            raise ValueError("Font has no supported Unicode character map")
        self._cmap = max(choices)[2]
        self._cmap_format = self.u16(self._cmap)
        if self._cmap_format == 12:
            count = unpack_from(">I", self.data, self._cmap + 12)[0]
            self._groups = tuple(
                unpack_from(">III", self.data, self._cmap + 16 + i * 12)
                for i in range(count)
            )
            self._starts = tuple(group[0] for group in self._groups)
        else:
            count = self.u16(self._cmap + 6) // 2
            offset = self._cmap + 14
            ends = unpack_from(f">{count}H", self.data, offset)
            starts = unpack_from(f">{count}H", self.data, offset + count * 2 + 2)
            deltas = unpack_from(f">{count}h", self.data, offset + count * 4 + 2)
            ranges = offset + count * 6 + 2
            self._segments = tuple(
                (
                    starts[i],
                    ends[i],
                    deltas[i],
                    self.u16(ranges + i * 2),
                    ranges + i * 2,
                )
                for i in range(count)
            )
            self._starts = starts

    def glyph_id(self, character):
        code = ord(character)
        index = bisect_right(self._starts, code) - 1
        if index < 0:
            return 0
        if self._cmap_format == 12:
            start, end, first = self._groups[index]
            glyph = first + code - start if code <= end else 0
        else:
            start, end, delta, relative, address = self._segments[index]
            if code > end:
                return 0
            if relative:
                glyph = self.u16(address + relative + (code - start) * 2)
                glyph = (glyph + delta) & 0xFFFF if glyph else 0
            else:
                glyph = (code + delta) & 0xFFFF
        return glyph if glyph < self.glyph_count else 0

    def width(self, text, size):
        previous, total = 0, 0
        for character in text:
            glyph = self.glyph_id(character)
            total += self.kerning.get((previous, glyph), 0) + self.advances[glyph]
            previous = glyph
        return total * size / self.units


@lru_cache(maxsize=2)
def font(mono=False):
    return TrueTypeFont(font_bytes(mono))


def ascent(size, mono=False):
    face = font(mono)
    return face.ascender * size / face.units


def measure(text, size, mono=False):
    if not isfinite(size) or size <= 0 or size > 2048:
        raise ValueError("Font size must be finite and between 0 and 2048 pixels")
    return (
        _measure_cached(text, size, mono)
        if len(text) <= 4096
        else _measure(text, size, mono)
    )


def _measure(text, size, mono):
    face = font(mono)
    # Browsers compose canonical accents before positioning their glyphs.
    # Normalize only this measuring copy; document text and indices stay intact.
    lines = normalize("NFC", text).split("\n")
    height = ceil((face.ascender - face.descender) * size / face.units)
    return max((face.width(line, size) for line in lines), default=0), height * len(
        lines
    )


_measure_cached = lru_cache(maxsize=2048)(_measure)
