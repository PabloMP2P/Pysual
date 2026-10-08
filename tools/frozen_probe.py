"""Self-closing application used to verify distributed executables."""
from pathlib import Path
import hashlib
import json
import os
import sys
import struct
import traceback
import zlib


def sample_image():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff\x00\x00\xff\xff")) + chunk(b"IEND", b""))


def bmp_pixel(path, x, y):
    data = path.read_bytes()
    offset = struct.unpack_from("<I", data, 10)[0]
    width, height = struct.unpack_from("<ii", data, 18)
    depth = struct.unpack_from("<H", data, 28)[0]
    assert data[:2] == b"BM" and depth in (24, 32)
    row = abs(height) - 1 - y if height > 0 else y
    position = offset + row * ((width * depth + 31) // 32 * 4) + x * (depth // 8)
    return tuple(reversed(data[position:position + 3]))


def main():
    import pysual
    from pysual import App, Image, Label, configure, image_source
    from pysual.backends import resolve_backend

    expected = os.environ["PYSUAL_PROBE_BACKEND"]
    assert resolve_backend("web") == expected, "Frozen target must override a named application backend"
    assets = Path(__file__).resolve().parent
    assert (assets / "assets/nested/message.txt").read_text(encoding="utf-8") == "Included directory: Zoë\n"
    assert (assets / "settings.txt").read_text(encoding="utf-8") == "Included file\n"
    notice = Path(pysual.__file__).parent / "licenses/CPython-LICENSE.txt"
    notice_sha256 = hashlib.sha256(notice.read_bytes()).hexdigest()
    assert notice_sha256 == os.environ["PYSUAL_PROBE_PYTHON_LICENSE_SHA256"]
    native_notices = json.loads(os.environ["PYSUAL_PROBE_NATIVE_NOTICES"])
    for name, expected_hash in native_notices.items():
        data = (Path(pysual.__file__).parent / "bin" / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected_hash, name
    os.environ["PYSUAL_HIDDEN"] = "1"
    os.environ["PYSUAL_VSYNC"] = "0"
    opened = []
    if expected == "terminal":
        from pysual.backends import terminal
        host_type = terminal.TerminalHost
        def headless():
            host = host_type(hidden=True)
            opened.append(host)
            return host
        terminal.TerminalHost = headless
        # Application configuration must not replace the renderer embedded by
        # the builder, especially in a package intentionally excluding C.
        configure(terminal_renderer="c" if os.environ["PYSUAL_PROBE_RENDERER"] == "python" else "python")
    app = App(title="Distribution check", width=320, height=160, layout="absolute", padding=0, ui_scale=1)
    app.message = Label(text="Starting")
    if expected == "window":
        app.image_probe = Image(source=image_source(sample_image()), left=0, top=50, width=100, height=40)
    app.run(backend="web")
    try:
        app.message.text = "Updated by frozen Python"
        assert app.message.text == "Updated by frozen Python"
        if expected == "terminal":
            renderer = os.environ["PYSUAL_PROBE_RENDERER"]
            assert opened[0].renderer == renderer
            if renderer == "python":
                helper = "pysual-host.exe" if sys.platform == "win32" else "pysual-host"
                assert not (Path(pysual.__file__).parent / "bin" / helper).exists()
        else:
            app._runtime.host.capture(Path("frame.bmp").resolve())
            red, blue = bmp_pixel(Path("frame.bmp"), 10, 60), bmp_pixel(Path("frame.bmp"), 90, 60)
            assert red[0] > 240 and red[2] < 16, red
            assert blue[2] > 240 and blue[0] < 16, blue
    finally:
        app.close()
        app.wait()
    return {"backend": expected, "included_assets": True, "python_license_sha256": notice_sha256,
            "native_notices_verified": native_notices, "success": True}


if __name__ == "__main__":
    try:
        result = main()
    except BaseException:
        Path("result.json").write_text(json.dumps({"success": False, "error": traceback.format_exc()}), encoding="utf-8")
        raise SystemExit(1)
    Path("result.json").write_text(json.dumps(result), encoding="utf-8")
