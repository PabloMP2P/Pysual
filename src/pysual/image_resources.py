"""Portable PNG sources, independent of native paths and browser URL resolution."""

import base64
import struct
import zlib
from importlib import resources

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_DECODED_IMAGE_BYTES = 32 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def image_source(data: bytes) -> str:
    """Return a portable PNG data URI for ``Image.source`` or ``Painter.image``.

    Encoded input is limited to 8 MiB and its PNG header must describe at most
    32 MiB of RGBA pixels. Hosts still decode the image and own their bounded
    decoded-resource caches; this helper retains no resources or global cache.
    """
    if not isinstance(data, bytes):
        raise TypeError("Image data must be PNG bytes")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("PNG image input must be at most 8 MiB")
    if (
        len(data) < 33
        or data[:8] != _PNG_SIGNATURE
        or data[8:16] != b"\x00\x00\x00\rIHDR"
        or zlib.crc32(data[12:29]) != int.from_bytes(data[29:33], "big")
    ):
        raise ValueError("Image data must contain a valid PNG header")
    width, height = struct.unpack(">II", data[16:24])
    if not width or not height or width * height * 4 > MAX_DECODED_IMAGE_BYTES:
        raise ValueError("PNG image dimensions must fit within 32 MiB of RGBA pixels")
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def package_image(package: str, resource: str) -> str:
    """Read a package-relative PNG and return the same portable image source.

    ``resource`` is a forward-slash relative path, such as ``assets/logo.png``.
    The package must be installed or included in the application build. Reading
    uses importlib.resources, including zip imports; no temporary file, native
    absolute path or browser URL is exposed.
    """
    if not isinstance(package, str) or not package:
        raise TypeError("Image package must be a nonempty importable package name")
    if not isinstance(resource, str):
        raise TypeError("Image resource must be a package-relative path")
    parts = resource.split("/")
    if any(mark in resource for mark in ("\\", ":")) or any(
        part in ("", ".", "..") for part in parts
    ):
        raise ValueError("Image resource must be a forward-slash package-relative path")
    item = resources.files(package).joinpath(*parts)
    with item.open("rb") as stream:
        data = stream.read(MAX_IMAGE_BYTES + 1)
    return image_source(data)
