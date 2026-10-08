"""Check standalone and live Python applications in real headless Chromium.

python -m playwright install chromium
python tools/check_web.py [--runtime path/to/pyodide]
python tools/check_web.py --mode live
python tools/check_web.py --mode all [--runtime path/to/pyodide]

The default standalone check serves only the generated HTML and rejects external
requests. Live checks exercise the Hello and Dashboard examples through their
actual loopback WebHost, followed by metadata, lifetime and reconnection checks
under both SSE and fetch. Screenshots and diagnostics are saved in .build/web-smoke
and .build/web-live respectively. Live mode does not download a Pyodide runtime.
"""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]


def browser_diagnostics():
    return {"console": [], "page_errors": [], "requests": [],
            "blocked_requests": [], "success": False}


def watch_page(page, diagnostics, timeout=30_000):
    page.set_default_timeout(timeout)
    page.on("console", lambda message: diagnostics["console"].append(
        {"type": message.type, "text": message.text}))
    page.on("pageerror", lambda error: diagnostics["page_errors"].append(str(error)))


def assert_browser_clean(diagnostics):
    assert not diagnostics["blocked_requests"], diagnostics["blocked_requests"]
    assert not diagnostics["page_errors"], diagnostics["page_errors"]
    errors = [message for message in diagnostics["console"] if message["type"] == "error"]
    assert not errors, errors


def focus_text(page, x, y, selector="textarea"):
    from playwright.sync_api import expect

    page.mouse.click(x, y)
    # Live input crosses HTTP and the UI owner before editor metadata returns.
    # An acknowledged click alone does not establish that text input is ready.
    expect(page.locator(selector)).to_be_focused()


def failure_screenshot(page, path):
    try:
        page.screenshot(path=str(path), timeout=5_000)
    except Exception:
        pass  # Keep the original browser/application failure.


def check_feedback_and_resize(page, selector):
    """Exercise settled motion and retained background geometry in either host."""
    from playwright.sync_api import expect

    page.mouse.move(350, 250)
    expect(page.locator(f'{selector} rect[fill="#00ff00"]')).to_have_count(1)
    page.mouse.move(750, 450)
    expect(page.locator(f'{selector} rect[fill="#ff0000"]')).to_have_count(1)
    thumb = """selector => [...document.querySelectorAll(selector + ' rect')]
        .find(rect => +rect.getAttribute('width') === 18 &&
                      +rect.getAttribute('height') === 18)"""
    start = page.evaluate(f"selector => +({thumb})(selector).getAttribute('x')", selector)
    for expected in (start + 19, start):
        page.mouse.click(510, 360)
        page.wait_for_function(
            f"data => +({thumb})(data.selector).getAttribute('x') === data.expected",
            arg={"selector": selector, "expected": expected})
    for width, height in ((1000, 820), (800, 500)):
        page.set_viewport_size({"width": width, "height": height})
        expect(page.locator(selector)).to_have_attribute("viewBox", f"0 0 {width} {height}")
        page.wait_for_function("""data => {
            const rect = document.querySelector(data.selector + ' > rect');
            return +rect.getAttribute('width') === data.width &&
                   +rect.getAttribute('height') === data.height;
        }""", arg={"selector": selector, "width": width, "height": height})
    return ["hover_settles", "toggle_on_and_off_settle", "resize_grow_and_shrink"]


def check_standalone(runtime=None):
    from PIL import Image
    from playwright.sync_api import expect, sync_playwright
    from pysual.bundle import PYODIDE_VERSION, fetch_runtime

    destination = ROOT / ".build/web-smoke"
    destination.mkdir(parents=True, exist_ok=True)
    runtime = runtime or fetch_runtime(ROOT / ".build" / f"pyodide-{PYODIDE_VERSION}")
    fixture = destination / "fixture"
    subprocess.run([sys.executable, str(ROOT / "tests/web/build_asset_smoke.py"),
                    "--runtime", str(runtime.resolve()), "--output", str(fixture)], check=True, timeout=120)
    served = destination / "served"
    served.mkdir(exist_ok=True)
    shutil.copy2(fixture / "asset_smoke.html", served / "index.html")
    diagnostics = browser_diagnostics()

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(served)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/index.html"
    try:
        with sync_playwright() as playwright:
            # Use full Chromium's current headless mode, including JSPI support.
            browser = playwright.chromium.launch(channel="chromium")
            diagnostics["browser"] = browser.version
            context = browser.new_context(viewport={"width": 800, "height": 500}, device_scale_factor=1)
            def route_request(route):
                request_url = route.request.url
                diagnostics["requests"].append(request_url)
                if request_url == url:
                    route.continue_()
                elif urlsplit(request_url).scheme in ("blob", "data"):
                    route.continue_()
                elif request_url == url.rsplit("/", 1)[0] + "/favicon.ico":
                    route.fulfill(status=204)
                else:
                    diagnostics["blocked_requests"].append(request_url)
                    route.abort()
            context.route("**/*", route_request)
            page = context.new_page()
            watch_page(page, diagnostics, timeout=60_000)
            try:
                page.goto(url, wait_until="load")
                expect(page.locator("#app")).to_contain_text("Ready after rejected second opening", timeout=60_000)
                assert "one presentation surface" in page.evaluate("window.secondOpeningError")
                expect(page.locator("textarea")).to_have_count(1)
                diagnostics["rejected_second_open_preserves_first"] = True
                # DOM image presence alone does not prove successful PNG/SVG decoding.
                deadline = time.monotonic() + 15
                while True:
                    screenshot = page.screenshot(path=str(destination / "images.png"))
                    pixels = Image.open(io.BytesIO(screenshot)).convert("RGB")
                    samples = [pixels.getpixel((74 + 158 * index, 152)) for index in range(4)]
                    if samples == [(59, 130, 246)] * 4:
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError(f"Relative/absolute PNG/SVG pixels: {samples}")
                    page.wait_for_timeout(100)
                diagnostics["image_pixels"] = samples
                focus_text(page, 80, 250)
                page.keyboard.insert_text("Zoë")
                expect(page.locator("#app")).to_contain_text("Zoë")
                page.keyboard.press("Tab")
                expect(page.locator("#app")).to_be_focused()
                page.keyboard.press("Enter")
                expect(page.locator("#app")).to_contain_text("Hello, Zoë!")
                page.screenshot(path=str(destination / "greeting.png"))
                # Compare Python caret/selection geometry to actual shaped SVG text.
                # Canonically decomposed accents must remain in the document.
                accents = "e\u0301" * 10
                focus_text(page, 70, 358)
                page.keyboard.press("End")
                page.wait_for_function("""text => {
                    const node = [...document.querySelectorAll('#app text')]
                        .find(node => node.textContent === text);
                    if (!node) return false;
                    const end = +node.getAttribute('x') + node.getComputedTextLength();
                    return [...document.querySelectorAll('#app line')].some(line =>
                        +line.getAttribute('y1') >= 340 &&
                        +line.getAttribute('y2') <= 380 &&
                        line.getAttribute('x1') === line.getAttribute('x2') &&
                        Math.abs(+line.getAttribute('x1') - end) < 0.25);
                }""", arg=accents)
                page.keyboard.press("Shift+Home")
                page.wait_for_function("""text => {
                    const node = [...document.querySelectorAll('#app text')]
                        .find(node => node.textContent === text);
                    if (!node) return false;
                    const x = +node.getAttribute('x'), width = node.getComputedTextLength();
                    return [...document.querySelectorAll('#app rect')].some(rect =>
                        +rect.getAttribute('y') >= 340 && +rect.getAttribute('y') < 380 &&
                        Math.abs(+rect.getAttribute('x') - x) < 0.25 &&
                        Math.abs(+rect.getAttribute('width') - width) < 0.25);
                }""", arg=accents)
                diagnostics["canonical_accent_geometry"] = True
                page.screenshot(path=str(destination / "accent-selection.png"))
                diagnostics["feedback"] = check_feedback_and_resize(page, "#app")
                page.screenshot(path=str(destination / "feedback.png"))
                assert_browser_clean(diagnostics)
                diagnostics["success"] = True
            except BaseException:
                diagnostics["error"] = traceback.format_exc()
                failure_screenshot(page, destination / "failure.png")
                raise
            finally:
                context.close()
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        (destination / "result.json").write_text(json.dumps(diagnostics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Standalone Chromium: included images, Unicode input/caret/selection, keyboard activation and async Python callback passed")


def wait_for_model(page, predicate, description):
    deadline = time.monotonic() + 10
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {description}")
        # Let Playwright deliver browser events while the Python UI owner runs.
        page.wait_for_timeout(20)


def control_point(control):
    bounds = control.bounds
    return bounds.x + bounds.width / 2, bounds.y + bounds.height / 2


def check_hello(page, app):
    from playwright.sync_api import expect

    frame = page.locator("#frame")
    expect(frame).to_contain_text("Say hello")
    focus_text(page, *control_point(app.name_entry), selector="#editor")
    page.keyboard.insert_text("Zoë")
    wait_for_model(page, lambda: app.name_entry.text == "Zoë", "Unicode entry")
    page.keyboard.press("Tab")
    expect(frame).to_be_focused()
    page.keyboard.press("Enter")
    wait_for_model(page, lambda: app.message.text == "Hello, Zoë!", "async greeting")
    expect(frame).to_contain_text("Hello, Zoë!")

    focus_text(page, *control_point(app.name_entry), selector="#editor")
    page.keyboard.press("ControlOrMeta+A")
    page.evaluate("navigator.clipboard.writeText('Ada')")
    page.keyboard.press("ControlOrMeta+V")
    wait_for_model(page, lambda: app.name_entry.text == "Ada", "clipboard paste")
    page.keyboard.press("ControlOrMeta+Z")
    wait_for_model(page, lambda: app.name_entry.text == "Zoë", "undo")
    page.keyboard.press("ControlOrMeta+Shift+Z")
    wait_for_model(page, lambda: app.name_entry.text == "Ada", "redo")
    expect(frame).to_contain_text("Ada")
    return ["unicode_entry", "tab_enter_async_greeting", "clipboard_paste", "undo_redo"]


def check_dashboard(page, app):
    from playwright.sync_api import expect

    frame = page.locator("#frame")
    expect(frame).to_contain_text("Revenue by region")
    focus_text(page, *control_point(app.query), selector="#editor")
    page.keyboard.insert_text("North")
    wait_for_model(page, lambda: tuple(row.key for row in app.grid.rows) == ("0", "2"),
                   "two filtered North rows")
    expect(frame).to_contain_text("2 of 6 accounts")

    bounds, columns = app.grid.bounds, app.grid.columns
    page.mouse.click(bounds.x + sum(column.width for column in columns[:2]) + columns[2].width / 2,
                     bounds.y + app.grid.header_height + app.grid.row_height / 2)
    page.keyboard.press("F2")
    # The same DOM editor was focused for filtering; wait for the new popup,
    # not merely that stale focus state, before sending replacement text.
    expect(frame).to_contain_text("Number or blank · Enter saves")
    expect(page.locator("#editor")).to_be_focused()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.insert_text("2400")
    page.keyboard.press("Enter")
    def edited():
        return any(row.key == "0" and row.cells[2] == 2400 for row in app.grid.rows)

    wait_for_model(page, edited, "numeric revenue edit")
    expect(frame).to_contain_text("2400")

    focus_text(page, *control_point(app.query), selector="#editor")
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.press("Backspace")
    wait_for_model(page, lambda: len(app.grid.rows) == 6, "cleared filter")
    assert edited(), "Clearing the filter lost the committed edit"
    expect(frame).to_contain_text("6 of 6 accounts")
    page.mouse.click(*control_point(app.add_account))
    wait_for_model(page, lambda: len(app.grid.rows) == 7, "new account")
    expect(frame).to_contain_text("7 of 7 accounts")

    viewport = page.viewport_size
    try:
        page.set_viewport_size({"width": 440, "height": 720})
        wait_for_model(page, lambda: app.grid.bounds.width < 500, "narrow grid viewport")
        bounds = app.grid.bounds
        assert sum(column.width for column in app.grid.columns) > bounds.width
        page.mouse.move(bounds.x + 80, bounds.y + 80)
        page.mouse.wheel(-500, 0)
        wait_for_model(page, lambda: app.grid._scroll_x == 0, "grid scrolled left")
        page.mouse.wheel(500, 0)
        wait_for_model(page, lambda: app.grid._scroll_x > 0, "horizontal wheel scrolls right")
        page.mouse.wheel(-500, 0)
        wait_for_model(page, lambda: app.grid._scroll_x == 0, "horizontal wheel scrolls left")
        page.keyboard.down("Shift")
        try:
            page.mouse.wheel(0, 500)
            wait_for_model(page, lambda: app.grid._scroll_x > 0, "Shift-wheel scrolls right")
        finally:
            page.keyboard.up("Shift")
    finally:
        page.set_viewport_size(viewport)
    return ["filter", "numeric_edit", "edit_survives_filter", "add_account",
            "horizontal_wheel", "shift_wheel"]


def check_live_transport(browser, *, fallback=False):
    from playwright.sync_api import expect
    from pysual import App, Label
    from pysual.backends.web import WebHost

    context = browser.new_context(viewport={"width": 500, "height": 300})
    if fallback:
        context.add_init_script("window.EventSource = undefined")
    page = context.new_page()
    app = App(title="Stable title", reduce_motion=True)
    app.message = Label(text="Stable scene")
    host = WebHost(open_browser=False)
    result, task = {}, None
    server_errors = []

    def record_server_error(request, address):
        server_errors.append(traceback.format_exc())

    try:
        app.run(backend=host)
        host._server.handle_error = record_server_error
        # Chromium offline emulation does not close an existing SSE socket.
        # Track only this host's connections so the outage is real and isolated.
        connections = []
        available = True
        process_request = host._server.process_request

        def accept(connection, address):
            if not available:
                host._server.shutdown_request(connection)
                return
            connections.append(connection)
            process_request(connection, address)

        host._server.process_request = accept

        def disconnect():
            nonlocal available
            available = False
            context.set_offline(True)
            for connection in connections:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass  # A prior request may already have closed its socket.
            connections.clear()

        page.goto(host.url)
        expect(page.locator("#frame")).to_contain_text("Stable scene")
        page.wait_for_function("document.fonts.status === 'loaded'")
        page.wait_for_timeout(250)
        revision = host._revision
        app.title = "Metadata delivered"
        expect(page).to_have_title("Metadata delivered")
        task = app.create_task(app.open_text_file())
        expect(page.locator("dialog")).to_have_count(1)
        app.title = "One pending dialog"
        expect(page).to_have_title("One pending dialog")
        expect(page.locator("dialog")).to_have_count(1)
        assert host._revision == revision, "Metadata needed a scene repaint"
        page.get_by_role("button", name="Cancel", exact=True).click()
        wait_for_model(page, task.done, "file cancellation")
        assert task.result() is None
        result["metadata_and_service_without_repaint"] = True

        for _ in range(3):
            page.reload()
            expect(page.locator("#frame")).to_contain_text("Stable scene")
        page.wait_for_timeout(2500)
        assert app.is_open, "Reload incorrectly closed the application"
        result["three_reloads"] = True

        disconnect()
        expect(page.locator("#status")).to_contain_text("Reconnecting", timeout=5000)
        available = True
        context.set_offline(False)
        app.title = "Recovered connection"
        expect(page).to_have_title("Recovered connection", timeout=7000)
        expect(page.locator("#status")).to_be_hidden()
        page.wait_for_timeout(8500)
        expect(page.locator("#status")).to_be_hidden()
        app.message.text = "After recovery"
        expect(page.locator("#frame")).to_contain_text("After recovery")
        result["recovery_cancels_timeout"] = True
        disconnect()
        expect(page.locator("#status")).to_contain_text("Python connection ended", timeout=12000)
        result["lost_connection_status"] = True

        available = True
        context.set_offline(False)
        page.reload()
        expect(page.locator("#frame")).to_contain_text("After recovery")
        started = time.monotonic()
        page.close()
        app.wait(timeout=5)
        assert not app.is_open, "Tab close did not close the application"
        result["tab_close_seconds"] = time.monotonic() - started
        app.run(backend=host)
        host._server.handle_error = record_server_error
        page = context.new_page()
        page.goto(host.url)
        expect(page.locator("#frame")).to_contain_text("After recovery")
        expect(page.locator("#frame")).to_have_attribute("viewBox", "0 0 500 300")
        page.wait_for_function("document.fonts.status === 'loaded'")
        started = time.monotonic()
        page.goto("about:blank")
        app.wait(timeout=5)
        assert not app.is_open, "Navigation away did not close the application"
        result["navigate_away_seconds"] = time.monotonic() - started
        assert not server_errors, server_errors
        result["server_errors"] = server_errors
        return result
    finally:
        if task is not None:
            task.cancel()
        app.close()
        app.wait(timeout=5)
        app.destroy()
        context.close()


def check_live():
    from playwright.sync_api import sync_playwright
    from pysual import App, Button, Style, Theme, Toggle, Tokens
    from pysual.backends.web import WebHost
    from examples.hello import Hello
    from examples.dashboard import Dashboard

    class FeedbackSmoke(App):
        def build(self):
            self.reduce_motion = False
            self.layout = "absolute"
            self.theme = Theme(tokens=Tokens(background="#f1f4fa")).styled(
                Button, Style(fill="#ff0000")
            ).styled(Button, Style(fill="#00ff00"), state="hover")
            self.action_button = Button(text="Feedback", left=322, top=230, width=150, height=40)
            self.enabled_switch = Toggle(text="Enabled", left=500, top=340, width=140, height=40)

    destination = ROOT / ".build/web-live"
    destination.mkdir(parents=True, exist_ok=True)
    diagnostics = {"apps": {}, "success": False}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chromium")
            diagnostics["browser"] = browser.version
            try:
                scenarios = (("hello", Hello, check_hello),
                             ("dashboard", Dashboard, check_dashboard),
                             ("feedback", FeedbackSmoke,
                              lambda page, app: check_feedback_and_resize(page, "#frame")))
                for name, app_type, scenario in scenarios:
                    result = diagnostics["apps"][name] = browser_diagnostics()
                    context = browser.new_context(viewport={"width": 960, "height": 760},
                        device_scale_factor=1, permissions=["clipboard-read", "clipboard-write"])
                    page = context.new_page()
                    watch_page(page, result)
                    page.on("request", lambda request, result=result: result["requests"].append(request.url))
                    app, host = app_type(reduce_motion=True), WebHost(open_browser=False)
                    try:
                        app.run(backend=host)
                        page.goto(host.url, wait_until="load")
                        result["scenarios"] = scenario(page, app)
                        page.screenshot(path=str(destination / f"{name}.png"))
                        result["resource_errors"] = list(app.resource_errors)
                        assert not result["resource_errors"], result["resource_errors"]
                        assert_browser_clean(result)
                        result["success"] = True
                    except BaseException:
                        result["error"] = traceback.format_exc()
                        result["resource_errors"] = list(app.resource_errors)
                        failure_screenshot(page, destination / f"{name}-failure.png")
                        raise
                    finally:
                        try:
                            app.close()
                            app.wait(timeout=10)
                        finally:
                            app.destroy()
                            context.close()
                diagnostics["transports"] = {
                    name: check_live_transport(browser, fallback=fallback)
                    for name, fallback in (("sse", False), ("fetch", True))
                }
            finally:
                browser.close()
        diagnostics["success"] = True
    except BaseException:
        diagnostics["error"] = traceback.format_exc()
        raise
    finally:
        (destination / "result.json").write_text(
            json.dumps(diagnostics, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("Live Chromium: Hello/Dashboard, metadata/services, reload/close and connection recovery passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, help="Use an already downloaded Pyodide runtime")
    parser.add_argument("--mode", choices=("standalone", "live", "all"), default="standalone")
    options = parser.parse_args()
    if options.mode in ("standalone", "all"):
        check_standalone(options.runtime)
    if options.mode in ("live", "all"):
        check_live()


if __name__ == "__main__":
    main()
