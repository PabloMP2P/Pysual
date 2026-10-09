"""Run only from check_wheel.py's clean installed environment."""
from importlib import metadata, resources
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import time


def main(kind):
    import pysual
    from pysual import (
        AlertBanner, App, Badge, Breadcrumb, ChoiceCard, CircularProgress,
        Container, Disclosure, Image, Label, Meter, Pagination,
        RangeSlider, SegmentedControl, image_source, registered_controls,
    )
    from pysual._png import encode_png
    from pysual.backends._native_client import native_executable
    from pysual.backends.native import NativeHost
    from pysual.backends.terminal import TerminalHost

    package = Path(pysual.__file__).resolve().parent
    assert package.is_relative_to(Path(sys.prefix).resolve()), package
    assert not any("editable" in path.name for path in Path(sys.prefix).rglob("*pysual*.pth"))
    distribution = metadata.distribution("pysual")
    assert distribution.version
    root = resources.files("pysual")
    assert root.joinpath("py.typed").is_file()
    assert root.joinpath("_factory_types.pyi").is_file()
    assert any(path.name.endswith(".ttf") for path in root.joinpath("assets").iterdir())
    for name in ("host.js", "input.js", "services.js", "standalone.js", "svg.js", "live.html"):
        assert root.joinpath("web", name).read_bytes(), name
    from pysual.bundle import PYODIDE_VERSION

    runtime_notices = json.loads(root.joinpath("web", "notices", "manifest.json").read_text())
    assert runtime_notices["pyodide_version"] == PYODIDE_VERSION
    for name, entry in runtime_notices["files"].items():
        data = root.joinpath("web", "notices", name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"], name
    assert "Makoto Matsumoto" in root.joinpath("web", "notices", "CPython-LICENSE.txt").read_text()
    print(json.dumps({"runtime_notices_verified": sorted(runtime_notices["files"])}), flush=True)
    helper = native_executable()
    assert helper.is_file() == (kind == "native"), helper
    if kind == "native":
        assert helper.resolve().is_relative_to(package), helper

    notices = json.loads(os.environ["PYSUAL_PROBE_NATIVE_NOTICES"])
    for name, expected_hash in notices.items():
        assert hashlib.sha256(root.joinpath("bin", name).read_bytes()).hexdigest() == expected_hash, name
    print(json.dumps({"native_notices_verified": notices}), flush=True)

    # The installed catalog must include the new focused family modules and
    # their public exports without adding reserved factory names to containers.
    panel = Container()
    try:
        interval = panel.create(RangeSlider, value=(20, 80))
        interval.set_range(30, 70)
        assert interval.value == (30, 70)
        ring = panel.create(CircularProgress, value=50)
        ring.set_range(40)
        assert ring.value == 40
        segments = panel.create(SegmentedControl, items=("All", "Done"), selected_index=1)
        assert segments.selected_item == "Done"
        panel.create(Breadcrumb, items=("Home", "Reviews"))
        pages = panel.create(Pagination, page=3, page_count=5)
        pages.update(page=1, page_count=1)
        panel.create(Badge, text="Ready", tone="success")
        panel.create(AlertBanner, text="Ready to review", action_text="Open")
        panel.create(ChoiceCard, text="Focused", checked=True)
        details = panel.create(Disclosure, title="Details", expanded=True)
        Label(parent=details.content, text="Installed content")
        meter = panel.create(Meter, value=50, warning_at=75, danger_at=90)
        meter.set_range(0, 80)
        assert (meter.value, meter.warning_at, meter.danger_at) == (50, 75, 80)
        assert len(registered_controls()) == 43
        assert len(registered_controls(factories_only=True)) == 30
        children = panel.children
    finally:
        panel.destroy()
    assert all(child._disposed for child in children)

    renderers = ["auto"] if kind == "pure" else ["python", "c"]
    for renderer in renderers:
        host = TerminalHost(renderer=renderer, hidden=True)
        app = App(width=320, height=160, layout="absolute", padding=0, ui_scale=1)
        app.message = Label(text="Installed package", left=0, top=0, width=300, height=32)
        app.run(backend=host)
        try:
            app.message.text = "Hello, Zoë!"
            assert host.renderer == ("python" if renderer == "auto" else renderer)
            deadline = time.monotonic() + 5
            while True:
                rows = "\n".join(host.snapshot()["rows_text"])
                if "Hello, Zoë!" in rows:
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f"Updated terminal text was not presented: {rows!r}")
                time.sleep(0.01)
        finally:
            app.close()
            app.wait()

    if kind == "native":
        host = NativeHost(hidden=True, vsync=False)
        app = App(width=160, height=96, layout="absolute", padding=0, ui_scale=1)
        app.message = Label(text="Installed font: Zoë", left=0, top=0, width=160, height=32)
        png = encode_png(2, 1, bytes((255, 0, 0, 255, 0, 0, 255, 255)))
        app.image_probe = Image(source=image_source(png), left=0, top=40, width=100, height=40)
        app.run(backend=host)
        try:
            app.message.text = "Updated: Zoë"
            host.capture(Path("frame.bmp").resolve())
            data = Path("frame.bmp").read_bytes()
            offset = struct.unpack_from("<I", data, 10)[0]
            width, height = struct.unpack_from("<ii", data, 18)
            depth = struct.unpack_from("<H", data, 28)[0]
            assert (width, abs(height)) == (160, 96) and depth in (24, 32)
            stride = (width * depth + 31) // 32 * 4
            def pixel(x, y):
                row = abs(height) - y - 1 if height > 0 else y
                position = offset + row * stride + x * (depth // 8)
                return tuple(reversed(data[position:position + 3]))
            assert pixel(10, 50) == (255, 0, 0), pixel(10, 50)
            assert pixel(90, 50) == (0, 0, 255), pixel(90, 50)
        finally:
            app.close()
            app.wait()
    print(json.dumps({"kind": kind, "package": str(package), "version": distribution.version,
                      "terminal_renderers": renderers, "success": True}), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
