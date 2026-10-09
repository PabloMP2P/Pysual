"""Capture, check or deliberately update a small native/Chromium visual baseline.

python tools/check_visual.py --mode capture  # Review .build/visual first.
python tools/check_visual.py --mode update  # Accept the reviewed environment.
python tools/check_visual.py                # Compare; save useful failure diffs.
"""
import argparse
import hashlib
from importlib.metadata import version
import io
import json
from pathlib import Path
import platform
import shutil
import sys
import time

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pysual import (
    App, Button, CalendarEditor, CheckBox, ChoiceCard, Label, ListView,
    MenuBar, MenuGroup, MenuItem, TextBox, Toggle, get_theme,
)

THEMES = ("modern", "modern_dark", "macos")
SIZE = (800, 560)
SCALE = 1.5
# Avoid hardware-driver and display subpixel settings in reviewed web pixels.
WEB_ARGS = ("--use-angle=swiftshader", "--disable-lcd-text", "--force-color-profile=srgb")


class VisualFixture(App):
    def build(self):
        self.layout = "absolute"
        self.menu_control = MenuBar(left=16, top=12, width=768, height=32, groups=(
            MenuGroup("File", (MenuItem("save", "Save"),)),
            MenuGroup("Edit", (MenuItem("copy", "Copy"),)),
            MenuGroup("View", (MenuItem("details", "Details"),)),
        ))
        self.save_action = Button(text="Save changes", left=16, top=64, width=160, height=38)
        self.disabled_action = Button(text="Unavailable", enabled=False,
                                      left=192, top=64, width=160, height=38)
        self.checked_option = CheckBox(text="Snap to grid", checked=True,
                                       left=16, top=118, width=180, height=32)
        self.enabled_option = Toggle(text="Preview", checked=True,
                                     left=208, top=118, width=144, height=32)
        self.text_entry = TextBox(text="Café — project notes", left=16, top=166,
                                  width=336, height=38)
        self.selected_rows = ListView(items=("Components", "Typography", "Colors", "Layouts"),
                                      selected_index=1, left=16, top=220, width=336, height=138)
        self.plan_control = ChoiceCard(text="Small plan", description="A selected card with an icon.",
                                      icon="check", checked=True, left=16, top=376,
                                      width=336, height=124)
        self.calendar_title = Label(text="Review date", left=420, top=64, width=348, height=28)
        self.calendar_control = CalendarEditor("2026-10-15", left=420, top=102,
                                              width=348, height=398)


def fixture(theme, *, scale=SCALE):
    return VisualFixture(title="Pysual visual check", width=SIZE[0], height=SIZE[1],
                         theme=get_theme(theme), reduce_motion=True, ui_scale=scale)


def close(app):
    try:
        app.close()
        app.wait(timeout=10)
    finally:
        app.destroy()


def capture_native(theme, path, *, scale=SCALE):
    from pysual.backends.native import NativeHost

    app, host = fixture(theme, scale=scale), NativeHost(hidden=True, vsync=False)
    try:
        app.run(backend=host)
        bitmap = path.with_suffix(".bmp")
        host.capture(bitmap)
        with Image.open(bitmap) as image:
            image.convert("RGB").save(path)
        bitmap.unlink()
        assert not app.resource_errors, app.resource_errors
        stats = host.native_stats()
        return {"renderer": stats["renderer"], "scale": stats["scale"]}
    finally:
        close(app)


def capture_web(browser, theme, path):
    from playwright.sync_api import expect
    from pysual.backends.web import WebHost

    context = browser.new_context(viewport={"width": SIZE[0], "height": SIZE[1]},
                                  device_scale_factor=SCALE)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # Browser viewport owns logical scaling; screenshot device pixels use SCALE.
    app, host = fixture(theme), WebHost(open_browser=False)
    app.ui_scale = 1
    try:
        app.run(backend=host)
        page.goto(host.url)
        frame = page.locator("#frame")
        expect(frame).to_contain_text("October 2026")
        fonts = page.evaluate("""async () => {
            await document.fonts.load('16px "Pysual Sans"');
            await document.fonts.ready;
            return [...document.fonts].map(font => ({family: font.family, status: font.status}));
        }""")
        assert any(font["family"].strip("'\"") == "Pysual Sans" and font["status"] == "loaded"
                   for font in fonts), fonts
        previous = None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = frame.screenshot()
            if current == previous:
                with Image.open(io.BytesIO(current)) as image:
                    image.convert("RGB").save(path)
                break
            previous = current
            page.wait_for_timeout(100)
        else:
            raise RuntimeError("Visual fixture did not settle")
        assert not errors, errors
        assert not app.resource_errors, app.resource_errors
        return {"browser": browser.version, "scale": SCALE, "font_faces": fonts}
    finally:
        try:
            close(app)
        finally:
            context.close()


def web_environment(browser):
    session = browser.new_browser_cdp_session()
    try:
        gpu = session.send("SystemInfo.getInfo")["gpu"]
    finally:
        session.detach()
    attributes = gpu["auxAttributes"]
    renderer = attributes.get("glRenderer", "")
    if "SwiftShader" not in renderer:
        raise RuntimeError(f"Visual baseline requires SwiftShader; got {renderer!r}")
    return {"os_version": platform.version(), "playwright": version("playwright"),
            "channel": "chromium", "headless": True, "launch_args": list(WEB_ARGS),
            "renderer": renderer, "skia_backend": attributes.get("skiaBackendType"),
            "rasterization": gpu["featureStatus"].get("rasterization")}


def compare(actual, expected, difference, tolerance):
    with Image.open(actual) as source, Image.open(expected) as reference:
        source, reference = source.convert("RGB"), reference.convert("RGB")
        if source.size != reference.size:
            return {"error": "Image dimensions differ", "actual": source.size, "expected": reference.size}
        delta = ImageChops.difference(source, reference)
        red, green, blue = delta.split()
        largest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
        mask = largest.point(lambda value: 255 if value > tolerance else 0)
        changed = mask.histogram()[255]
        if changed:
            delta.point(lambda value: min(value * 4, 255)).save(difference)
        return {"changed_pixels": changed, "bounds": mask.getbbox(), "tolerance": tolerance}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=("native", "web", "all"), default="all")
    parser.add_argument("--mode", choices=("capture", "check", "update"), default="check")
    parser.add_argument("--native-scale", type=float, choices=(1, 1.5), default=SCALE,
                        help="Native capture scale; use 1 on small desktops (web stays at 1.5)")
    parser.add_argument("--output", type=Path, default=ROOT / ".build/visual")
    parser.add_argument("--baselines", type=Path,
                        default=ROOT / "tests/visual_baselines" / platform.system().lower())
    parser.add_argument("--tolerance", type=int, choices=range(256), default=8, metavar="0..255")
    options = parser.parse_args()
    if options.output.resolve() == options.baselines.resolve():
        parser.error("Capture output must differ from the baseline directory")
    fonts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in (ROOT / "src/pysual/assets").glob("*.ttf")}
    result = {"mode": options.mode, "success": False, "backends": {}}
    options.output.mkdir(parents=True, exist_ok=True)
    browser = playwright = None
    try:
        if options.backend in ("web", "all"):
            from playwright.sync_api import sync_playwright
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(channel="chromium", headless=True, args=list(WEB_ARGS))
        for backend in (("native", "web") if options.backend == "all" else (options.backend,)):
            scale = options.native_scale if backend == "native" else SCALE
            output, baseline = options.output / backend, options.baselines / backend
            output.mkdir(parents=True, exist_ok=True)
            records = result["backends"][backend] = {}
            environment = {"platform": platform.system(), "size": list(SIZE), "fonts": fonts}
            if backend == "web":
                environment.update(web_environment(browser))
            for theme in THEMES:
                for suffix in ("-diff.png", "-expected.png"):
                    (output / f"{theme}{suffix}").unlink(missing_ok=True)
                path = output / f"{theme}.png"
                environment.update(capture_native(theme, path, scale=scale) if backend == "native"
                                   else capture_web(browser, theme, path))
                with Image.open(path) as image:
                    expected = tuple(int(value * scale) for value in SIZE)
                    assert image.size == expected, (image.size, expected)
            (output / "environment.json").write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")
            if options.mode == "update":
                baseline.mkdir(parents=True, exist_ok=True)
                for name in ["environment.json", *(f"{theme}.png" for theme in THEMES)]:
                    shutil.copy2(output / name, baseline / name)
            elif options.mode == "check":
                manifest = baseline / "environment.json"
                if not manifest.is_file() or json.loads(manifest.read_text(encoding="utf-8")) != environment:
                    records["error"] = "Baseline missing or environment changed; review captures before --mode update"
                    continue
                for theme in THEMES:
                    expected = baseline / f"{theme}.png"
                    records[theme] = compare(output / expected.name, expected,
                                             output / f"{theme}-diff.png", options.tolerance)
                    if records[theme].get("changed_pixels") or records[theme].get("error"):
                        shutil.copy2(expected, output / f"{theme}-expected.png")
            records["success"] = not records.get("error") and all(
                not entry.get("changed_pixels") and not entry.get("error")
                for entry in records.values() if isinstance(entry, dict))
        result["success"] = all(item.get("success") for item in result["backends"].values())
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        (options.output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
