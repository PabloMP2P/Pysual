"""Independent PNG fixtures cover portable decoding and resource limits."""

import base64
import random
import struct
import unittest
import zlib

from pysual._png import decode_png, encode_png, png_source_pixels


def chunk(kind, payload):
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def png(width, height, depth, color, raw, *, extra=b"", interlace=0):
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, depth, color, 0, 0, interlace),
        )
        + extra
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class PNGCodecTests(unittest.TestCase):
    def test_all_filters_on_first_pixels_and_adam7_passes(self):
        rng = random.Random(7)
        passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                  (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
        for width, height in ((1, 1), (17, 9)):
            for color, channels in ((2, 3), (6, 4)):
                pixels = rng.randbytes(width * height * channels)
                expected = pixels if channels == 4 else b"".join(
                    pixels[i:i + 3] + b"\xff" for i in range(0, len(pixels), 3))
                for interlace in (0, 1):
                    for method in range(5):
                        raw = bytearray()
                        for x0, y0, dx, dy in passes if interlace else ((0, 0, 1, 1),):
                            if x0 >= width or y0 >= height:
                                continue
                            previous = None
                            for y in range(y0, height, dy):
                                row = b"".join(pixels[(y * width + x) * channels:
                                                      (y * width + x + 1) * channels]
                                               for x in range(x0, width, dx))
                                if previous is None:
                                    previous = bytes(len(row))
                                raw.append(method)
                                for i, value in enumerate(row):
                                    a = row[i - channels] if i >= channels else 0
                                    b = previous[i]
                                    c = previous[i - channels] if i >= channels else 0
                                    prediction = a + b - c
                                    predictor = (min((a, b, c), key=lambda v: abs(prediction - v))
                                                 if method == 4 else (0, a, b, (a + b) // 2)[method])
                                    raw.append((value - predictor) & 255)
                                previous = row
                        with self.subTest(size=(width, height), color=color,
                                          interlace=interlace, method=method):
                            self.assertEqual(decode_png(png(width, height, 8, color, raw,
                                                            interlace=interlace)),
                                             (width, height, expected))

    def test_rgba_roundtrip_and_portable_source(self):
        pixels = bytes(range(256)) * 4
        encoded = encode_png(16, 16, pixels)
        self.assertEqual(decode_png(encoded), (16, 16, pixels))
        self.assertEqual(
            png_source_pixels(
                "data:image/png;base64," + base64.b64encode(encoded).decode()
            ),
            (16, 16, pixels),
        )

    def test_all_scanline_filters_reconstruct_same_rgb(self):
        first = bytes([12, 24, 36, 44, 66, 88])
        second = bytes([16, 36, 48, 62, 77, 91])
        for method in range(5):
            filtered = bytearray()
            for i, value in enumerate(second):
                a = second[i - 3] if i >= 3 else 0
                b = first[i]
                c = first[i - 3] if i >= 3 else 0
                if method == 4:
                    p = a + b - c
                    distances = [abs(p - v) for v in (a, b, c)]
                    predictor = (a, b, c)[distances.index(min(distances))]
                else:
                    predictor = (0, a, b, (a + b) // 2)[method]
                filtered.append((value - predictor) % 256)
            with self.subTest(method=method):
                raw = b"\0" + first + bytes([method]) + filtered
                expected = b"".join(
                    row[i : i + 3] + b"\xff" for row in (first, second) for i in (0, 3)
                )
                encoded = png(2, 2, 8, 2, raw)
                self.assertEqual(decode_png(encoded)[2], expected)
                self.assertEqual(
                    png_source_pixels(
                        "data:image/png;base64," + base64.b64encode(encoded).decode()
                    )[2],
                    expected,
                )

    def test_paeth_equal_neighbors_and_ties_preserve_pixels(self):
        # Exercise every byte value in all equality shortcuts. The encoder
        # uses the general distance rule, independently of decoder shortcuts.
        neighbors = [(0, 3, 2), (1, 4, 2), (255, 0, 128), (0, 255, 127)]
        for value in range(256):
            other = (value * 73 + 17) % 256
            neighbors.extend(((value, other, value), (other, value, value),
                              (value, value, other), (value, value, value)))
        for a, b, c in neighbors:
            target = (a + b + c + 101) % 256
            first, second = bytes((c, b)), bytes((a, target))
            filtered = bytearray()
            for i, value in enumerate(second):
                candidates = (second[i - 1] if i else 0, first[i],
                              first[i - 1] if i else 0)
                prediction = candidates[0] + candidates[1] - candidates[2]
                predictor = min(candidates, key=lambda v: abs(prediction - v))
                filtered.append((value - predictor) % 256)
            expected = b"".join(bytes((v, v, v, 255)) for v in first + second)
            with self.subTest(a=a, b=b, c=c):
                self.assertEqual(
                    decode_png(png(2, 2, 8, 0, b"\0" + first + b"\4" + filtered))[2],
                    expected,
                )

    def test_paeth_matches_unfiltered_packed_and_multibyte_formats(self):
        rng = random.Random(31)
        width, height = 17, 5
        for color, channels, depths in (
            (0, 1, (1, 2, 4, 8, 16)), (2, 3, (8, 16)),
            (3, 1, (1, 2, 4, 8)), (4, 2, (8, 16)), (6, 4, (8, 16)),
        ):
            for depth in depths:
                stride = (width * channels * depth + 7) // 8
                bpp = max(1, (channels * depth + 7) // 8)
                rows = [rng.randbytes(stride) for _ in range(height)]
                extra = chunk(b"PLTE", rng.randbytes(3 * (1 << depth))) if color == 3 else b""
                unfiltered = b"".join(b"\0" + row for row in rows)
                expected = decode_png(png(width, height, depth, color, unfiltered, extra=extra))
                previous = bytes(stride)
                filtered = bytearray()
                for row in rows:
                    filtered.append(4)
                    for i, value in enumerate(row):
                        neighbors = (row[i - bpp] if i >= bpp else 0, previous[i],
                                     previous[i - bpp] if i >= bpp else 0)
                        prediction = neighbors[0] + neighbors[1] - neighbors[2]
                        predictor = min(neighbors, key=lambda v: abs(prediction - v))
                        filtered.append((value - predictor) & 255)
                    previous = row
                with self.subTest(color=color, depth=depth):
                    self.assertEqual(
                        decode_png(png(width, height, depth, color, filtered, extra=extra)),
                        expected,
                    )

    def test_unchanged_filtered_rows_and_channels_across_formats_and_adam7(self):
        rng = random.Random(531)
        passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                  (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
        for color, channels, depths in (
            (0, 1, (1, 2, 4, 8, 16)), (2, 3, (8, 16)),
            (3, 1, (1, 2, 4, 8)), (4, 2, (8, 16)), (6, 4, (8, 16)),
        ):
            for depth in depths:
                for width, height in ((1, 5), (17, 19)):
                    for interlace in (0, 1):
                        for method in (2, 4):
                            filtered, unfiltered = bytearray(), bytearray()
                            bpp = max(1, (channels * depth + 7) // 8)
                            for x0, y0, dx, dy in passes if interlace else ((0, 0, 1, 1),):
                                if x0 >= width or y0 >= height:
                                    continue
                                pw = (width - x0 + dx - 1) // dx
                                stride = (pw * channels * depth + 7) // 8
                                previous = bytes(stride)
                                for index, _ in enumerate(range(y0, height, dy)):
                                    # Repeated rows exercise Up; partly repeated byte
                                    # channels exercise Paeth alongside its normal path.
                                    row = bytearray(previous if index % 3 == 1 else rng.randbytes(stride))
                                    if method == 4 and index % 3 == 1 and bpp > 1:
                                        row[1::bpp] = rng.randbytes(len(row[1::bpp]))
                                    unfiltered.extend(b"\0" + row)
                                    filtered.append(method)
                                    for i, value in enumerate(row):
                                        a = row[i - bpp] if i >= bpp else 0
                                        b = previous[i]
                                        c = previous[i - bpp] if i >= bpp else 0
                                        prediction = a + b - c
                                        predictor = (b if method == 2 else
                                                     min((a, b, c), key=lambda v: abs(prediction - v)))
                                        filtered.append((value - predictor) & 255)
                                    previous = row
                            extra = chunk(b"PLTE", rng.randbytes(3 * (1 << depth))) if color == 3 else b""
                            with self.subTest(color=color, depth=depth, width=width,
                                              interlace=interlace, method=method):
                                expected = decode_png(png(width, height, depth, color, unfiltered,
                                                          extra=extra, interlace=interlace))
                                self.assertEqual(decode_png(png(width, height, depth, color, filtered,
                                                                extra=extra, interlace=interlace)), expected)

    def test_changing_flat_paeth_channels_across_formats_and_adam7(self):
        passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
                  (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))
        for color, channels, depths in (
            (0, 1, (1, 2, 4, 8, 16)), (2, 3, (8, 16)),
            (3, 1, (1, 2, 4, 8)), (4, 2, (8, 16)), (6, 4, (8, 16)),
        ):
            for depth in depths:
                for width, height in ((1, 5), (17, 19)):
                    for interlace in (0, 1):
                        filtered, plain = bytearray(), bytearray()
                        bpp = max(1, (channels * depth + 7) // 8)
                        for x0, y0, dx, dy in passes if interlace else ((0, 0, 1, 1),):
                            if x0 >= width or y0 >= height:
                                continue
                            pw = (width - x0 + dx - 1) // dx
                            stride = (pw * channels * depth + 7) // 8
                            previous = bytes(stride)
                            for index, _ in enumerate(range(y0, height, dy)):
                                row = bytes((index * 37 + (i % bpp) * 51) % 256
                                            for i in range(stride))
                                plain.extend(b"\0" + row)
                                filtered.append(4)
                                for i, value in enumerate(row):
                                    a = row[i - bpp] if i >= bpp else 0
                                    b = previous[i]
                                    c = previous[i - bpp] if i >= bpp else 0
                                    prediction = a + b - c
                                    predictor = min((a, b, c), key=lambda v: abs(prediction - v))
                                    filtered.append((value - predictor) & 255)
                                previous = row
                        extra = (chunk(b"PLTE", bytes(v for i in range(1 << depth)
                                                      for v in (i, i, i)))
                                 if color == 3 else b"")
                        with self.subTest(color=color, depth=depth, width=width,
                                          interlace=interlace):
                            expected = decode_png(png(width, height, depth, color, plain,
                                                      extra=extra, interlace=interlace))
                            self.assertEqual(decode_png(png(width, height, depth, color, filtered,
                                                            extra=extra, interlace=interlace)), expected)

    def test_rgb_color_key_and_16_bit_samples(self):
        for depth, samples in (
            (8, ((12, 34, 56), (13, 34, 56))),
            (16, ((0x1234, 0x5678, 0x9ABC), (0x1235, 0x5678, 0x9ABC))),
        ):
            raw = b"\0" + b"".join(
                value.to_bytes(depth // 8, "big") for pixel in samples for value in pixel
            )
            for keyed in (False, True):
                with self.subTest(depth=depth, keyed=keyed):
                    extra = (
                        chunk(b"tRNS", struct.pack(">HHH", *samples[0])) if keyed else b""
                    )
                    expected = b"".join(
                        bytes(value >> (depth - 8) for value in pixel)
                        + bytes([0 if keyed and index == 0 else 255])
                        for index, pixel in enumerate(samples)
                    )
                    self.assertEqual(
                        decode_png(png(2, 1, depth, 2, raw, extra=extra))[2], expected
                    )

    def test_packed_palette_and_transparency(self):
        extra = chunk(b"PLTE", bytes([255, 0, 0, 0, 255, 0, 0, 0, 255])) + chunk(
            b"tRNS", b"\xff\x80\0"
        )
        pixels = decode_png(png(3, 1, 2, 3, b"\0\x18", extra=extra))[2]
        self.assertEqual(pixels, bytes([255, 0, 0, 255, 0, 255, 0, 128, 0, 0, 255, 0]))

    def test_gray_low_depth_and_16_bit_transparency(self):
        pixels = decode_png(png(4, 1, 2, 0, b"\0\x1b"))[2]
        self.assertEqual(
            pixels,
            bytes(
                [0, 0, 0, 255, 85, 85, 85, 255, 170, 170, 170, 255, 255, 255, 255, 255]
            ),
        )
        extra = chunk(b"tRNS", b"\x12\x34")
        self.assertEqual(
            decode_png(png(2, 1, 16, 0, b"\0\x12\x34\xab\xcd", extra=extra))[2],
            bytes([18, 18, 18, 0, 171, 171, 171, 255]),
        )
        self.assertEqual(
            decode_png(png(1, 1, 8, 4, b"\0\x80\x40"))[2], bytes([128, 128, 128, 64])
        )

    def test_adam7_reconstructs_non_square_image(self):
        width, height = 11, 9
        pixels = bytes(
            v
            for y in range(height)
            for x in range(width)
            for v in (x * 20, y * 20, (x + y) * 10, 255)
        )
        passes = (
            (0, 0, 8, 8),
            (4, 0, 8, 8),
            (0, 4, 4, 8),
            (2, 0, 4, 4),
            (0, 2, 2, 4),
            (1, 0, 2, 2),
            (0, 1, 1, 2),
        )
        for color, channels in ((2, 3), (6, 4)):
            with self.subTest(color=color):
                raw = bytearray()
                for x0, y0, dx, dy in passes:
                    for y in range(y0, height, dy):
                        raw.append(0)
                        for x in range(x0, width, dx):
                            offset = (y * width + x) * 4
                            raw.extend(pixels[offset : offset + channels])
                self.assertEqual(
                    decode_png(png(width, height, 8, color, raw, interlace=1))[2], pixels
                )

    def test_corrupt_truncated_and_bomb_images_fail(self):
        valid = encode_png(1, 1, b"\0\0\0\xff")
        bad_crc = bytearray(valid)
        bad_crc[29] ^= 1
        cases = (
            bytes(bad_crc),
            valid[:-1],
            valid + b"trailing",
            png(1, 1, 8, 6, b"\0" * 100_000),
            png(1, 1, 8, 6, b"\x05\0\0\0\xff"),
            png(100_000, 100_000, 8, 6, b""),
        )
        for data in cases:
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                decode_png(data)
        with self.assertRaises(ValueError):
            png_source_pixels("https://example.com/image.png")
        with self.assertRaises(ValueError):
            png_source_pixels("data:image/png;base64,!!")


if __name__ == "__main__":
    unittest.main()
