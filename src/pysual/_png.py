"""Bounded, dependency-free PNG transport for masks and pixel hosts.

Decoding supports PNG's five color types, all legal depths and Adam7. Output
is always straight-alpha RGBA8. Callers own their decoded-resource caches.
"""

import base64
import struct
import zlib

MAX_ENCODED_BYTES = 8 * 1024 * 1024
MAX_RGBA_BYTES = 32 * 1024 * 1024
_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _dimensions(width, height):
    if (
        type(width) is not int
        or type(height) is not int
        or width <= 0
        or height <= 0
        or width * height * 4 > MAX_RGBA_BYTES
    ):
        raise ValueError("PNG dimensions must fit within 32 MiB of RGBA pixels")


def _chunk(kind, data):
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data))
    )


def rgba_png_data_uri(width, height, scanlines):
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(scanlines, 3))
        + _chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def encode_png(width: int, height: int, rgba: bytes | bytearray) -> bytes:
    """Encode tightly packed RGBA8 pixels without external image libraries."""
    _dimensions(width, height)
    if len(rgba) != width * height * 4:
        raise ValueError("RGBA byte count does not match PNG dimensions")
    stride = width * 4
    compressor = zlib.compressobj(3)
    compressed = bytearray()
    for y in range(0, len(rgba), stride):
        compressed.extend(compressor.compress(b"\0" + rgba[y : y + stride]))
    compressed.extend(compressor.flush())
    return (
        _SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", compressed)
        + _chunk(b"IEND", b"")
    )


def png_source_pixels(source: str) -> tuple[int, int, bytes]:
    """Decode the portable PNG data URI accepted by the public image API."""
    prefix = "data:image/png;base64,"
    if not isinstance(source, str) or not source.startswith(prefix):
        raise ValueError("Images require a PNG data URI from image_source()")
    if len(source) > len(prefix) + ((MAX_ENCODED_BYTES + 2) // 3) * 4:
        raise ValueError("PNG image input must be at most 8 MiB")
    try:
        data = base64.b64decode(source[len(prefix) :], validate=True)
    except ValueError as exc:
        raise ValueError("Invalid PNG base64 data") from exc
    return decode_png(data)


def decode_png(data: bytes) -> tuple[int, int, bytes]:
    """Decode a PNG with strict lengths, CRCs and bounded decompression.

    Ancillary color-management chunks are ignored, as in the native hosts;
    animated PNGs display their default image. Unknown critical chunks fail.
    """
    if not isinstance(data, bytes) or len(data) > MAX_ENCODED_BYTES:
        raise ValueError("PNG image input must be bytes and at most 8 MiB")
    if data[:8] != _SIGNATURE:
        raise ValueError("Invalid PNG signature")
    position = 8
    header = None
    palette = None
    transparent = None
    compressed = bytearray()
    ended = False
    idat_ended = False
    seen_idat = False
    while position < len(data):
        if position + 12 > len(data):
            raise ValueError("Truncated PNG chunk")
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        end = position + 12 + length
        if end > len(data):
            raise ValueError("Truncated PNG chunk data")
        payload = data[position + 8 : end - 4]
        if zlib.crc32(kind + payload) != int.from_bytes(data[end - 4 : end], "big"):
            raise ValueError("Invalid PNG chunk CRC")
        if header is None and kind != b"IHDR":
            raise ValueError("PNG must begin with IHDR")
        if kind == b"IHDR":
            if header is not None or length != 13:
                raise ValueError("Invalid PNG header")
            header = struct.unpack(">IIBBBBB", payload)
            width, height, depth, color, compression, filtering, interlace = header
            _dimensions(width, height)
            legal = {
                0: (1, 2, 4, 8, 16),
                2: (8, 16),
                3: (1, 2, 4, 8),
                4: (8, 16),
                6: (8, 16),
            }
            if (
                depth not in legal.get(color, ())
                or compression
                or filtering
                or interlace not in (0, 1)
            ):
                raise ValueError("Unsupported PNG header")
        elif kind == b"PLTE":
            if (
                palette is not None
                or seen_idat
                or not length
                or length % 3
                or length > 768
            ):
                raise ValueError("Invalid PNG palette")
            palette = payload
        elif kind == b"tRNS":
            if transparent is not None or seen_idat:
                raise ValueError("Invalid PNG transparency order")
            transparent = payload
        elif kind == b"IDAT":
            if idat_ended:
                raise ValueError("PNG IDAT chunks must be consecutive")
            seen_idat = True
            compressed.extend(payload)
        elif kind == b"IEND":
            if length or not seen_idat or end != len(data):
                raise ValueError("Invalid PNG end")
            ended = True
            break
        elif not kind[0] & 32:
            raise ValueError("Unknown critical PNG chunk")
        if seen_idat and kind != b"IDAT":
            idat_ended = True
        position = end
    if not ended or header is None:
        raise ValueError("Incomplete PNG")
    width, height, depth, color, _, _, interlace = header
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    if color == 3 and (palette is None or len(palette) // 3 > 1 << depth):
        raise ValueError("Missing or oversized PNG palette")
    if transparent is not None and (
        color == 0
        and len(transparent) != 2
        or color == 2
        and len(transparent) != 6
        or color == 3
        and (palette is None or len(transparent) > len(palette) // 3)
        or color in (4, 6)
    ):
        raise ValueError("Invalid PNG transparency")
    passes = (
        ((0, 0, 1, 1),)
        if not interlace
        else (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
    )
    layouts = []
    for x, y, dx, dy in passes:
        pw = max(0, (width - x + dx - 1) // dx)
        ph = max(0, (height - y + dy - 1) // dy)
        if pw and ph:
            layouts.append((x, y, dx, dy, pw, ph, (pw * channels * depth + 7) // 8))
    expected = sum((row_bytes + 1) * ph for _, _, _, _, _, ph, row_bytes in layouts)
    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(compressed, expected + 1)
    except zlib.error as exc:
        raise ValueError("Invalid PNG compression") from exc
    if (
        len(raw) != expected
        or not inflater.eof
        or inflater.unused_data
        or inflater.unconsumed_tail
    ):
        raise ValueError("PNG decompressed size does not match dimensions")
    out = bytearray(width * height * 4)
    position = 0
    bpp = max(1, (channels * depth + 7) // 8)
    transparent_samples = (
        tuple(
            int.from_bytes(transparent[i : i + 2], "big")
            for i in range(0, len(transparent), 2)
        )
        if transparent and color in (0, 2)
        else ()
    )
    for x0, y0, dx, dy, pw, ph, stride in layouts:
        previous = bytearray(stride)
        for py in range(ph):
            method = raw[position]
            row = bytearray(raw[position + 1 : position + 1 + stride])
            position += stride + 1
            if method > 4:
                raise ValueError("Invalid PNG row filter")
            if method == 1:
                for i in range(bpp, stride):
                    row[i] = (row[i] + row[i - bpp]) & 255
            elif method == 2:
                if not any(row):
                    row = previous[:]
                else:
                    row = bytearray((value + above) & 255
                                    for value, above in zip(row, previous))
            elif method == 3:
                for i in range(stride):
                    a = row[i - bpp] if i >= bpp else 0
                    row[i] = (row[i] + ((a + previous[i]) // 2)) & 255
            elif method == 4:
                for i in range(min(bpp, stride)):
                    row[i] = (row[i] + previous[i]) & 255
                # Each byte position within a pixel has independent left and
                # upper-left history; keep those neighbors in local variables.
                for channel in range(min(bpp, stride)):
                    # With an unchanged first byte and zero residuals, a == c
                    # makes Paeth select b throughout this byte channel.
                    if row[channel] == previous[channel] and not any(row[channel + bpp::bpp]):
                        row[channel::bpp] = previous[channel::bpp]
                        continue
                    # A constant previous channel makes b == c, so zero
                    # residuals repeat this row's reconstructed first byte.
                    if (channel + bpp < stride and row[channel + bpp] == 0
                            and previous[channel + bpp] == previous[channel]):
                        prior_channel = previous[channel::bpp]
                        if (prior_channel.count(previous[channel]) == len(prior_channel)
                                and not any(row[channel + bpp::bpp])):
                            row[channel::bpp] = bytes([row[channel]]) * len(prior_channel)
                            continue
                    a, c = row[channel], previous[channel]
                    for i in range(channel + bpp, stride, bpp):
                        b = previous[i]
                        if a == c:
                            predictor = b
                        elif b == c or a == b:
                            predictor = a
                        else:
                            p = a + b - c
                            pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                            predictor = a if pa <= pb and pa <= pc else b if pb <= pc else c
                        a = (row[i] + predictor) & 255
                        row[i] = a
                        c = b
            previous = row
            if color == 6 and depth == 8 and dx == 1:
                offset = ((y0 + py * dy) * width + x0) * 4
                out[offset : offset + pw * 4] = row
                continue
            if color == 2 and depth == 8 and dx == 1 and transparent is None:
                offset = ((y0 + py * dy) * width + x0) * 4
                expanded = bytearray(pw * 4)
                expanded[0::4] = row[0::3]
                expanded[1::4] = row[1::3]
                expanded[2::4] = row[2::3]
                expanded[3::4] = b"\xff" * pw
                out[offset : offset + pw * 4] = expanded
                continue
            for px in range(pw):
                at = px * channels
                if depth == 8:
                    values = list(row[at : at + channels])
                elif depth == 16:
                    values = [
                        int.from_bytes(row[i : i + 2], "big")
                        for i in range(at * 2, (at + channels) * 2, 2)
                    ]
                else:
                    values = [
                        (row[at * depth // 8] >> (8 - depth - at * depth % 8))
                        & ((1 << depth) - 1)
                    ]
                if color == 3:
                    index = values[0]
                    assert palette is not None
                    if index * 3 + 3 > len(palette):
                        raise ValueError("PNG pixel refers outside palette")
                    rgba = (
                        *palette[index * 3 : index * 3 + 3],
                        transparent[index]
                        if transparent and index < len(transparent)
                        else 255,
                    )
                else:
                    scaled = [
                        v >> 8 if depth == 16 else v * 255 // ((1 << depth) - 1)
                        for v in values
                    ]
                    if color in (0, 4):
                        rgba = (
                            scaled[0],
                            scaled[0],
                            scaled[0],
                            scaled[1]
                            if color == 4
                            else 0
                            if tuple(values) == transparent_samples
                            else 255,
                        )
                    else:
                        rgba = (
                            *scaled[:3],
                            scaled[3]
                            if color == 6
                            else 0
                            if tuple(values) == transparent_samples
                            else 255,
                        )
                offset = ((y0 + py * dy) * width + x0 + px * dx) * 4
                out[offset : offset + 4] = bytes(rgba)
    return width, height, bytes(out)
