"""The dynamic terminal wrapper exposes the typed public Host contract."""

from typing import assert_type

from pysual import Rect, Style, terminal
from pysual.backends.terminal import TerminalHost
from pysual.host import Input, TextFile, Viewport


def draw(target: TerminalHost) -> None:
    target.open("Typed terminal", 160, 64, False, 1)
    assert_type(target.size, tuple[float, float])
    assert_type(target.viewport, Viewport)
    target.set_title("Changed title")
    target.set_size(160, 64)
    target.begin("#000000")
    target.clip(Rect(0, 0, 160, 64))
    target.rect(Rect(0, 0, 16, 16), "#ffffff", border_width=0)
    target.text("Hello", 0, 16, "#ffffff", 16, mono=True)
    assert_type(target.measure("Hello", 16), tuple[float, float])
    target.line(0, 32, 80, 32, "#ffffff", width=1)
    target.image("example.png", Rect(80, 0, 32, 32), fit="contain")
    if "render_surfaces" in target.capabilities:
        surface = target.surface_create(Rect(0, 0, 16, 16))
        target.surface_begin(surface)
        target.surface_end()
        target.surface_blit(surface)
        target.surface_release(surface)
    target.text_input(None)
    target.present()
    assert_type(target.poll(), list[Input])
    target.close()


async def services(target: TerminalHost) -> None:
    assert_type(await target.clipboard_read(), str)
    await target.clipboard_write("Copied text")
    assert_type(await target.open_text_file(), TextFile | None)
    assert_type(await target.save_text_file("Text", "file.txt", None), TextFile | None)
    assert_type(await target.read_session("document"), str | None)
    await target.write_session("document", None)
    await target.open_url("https://example.com")


def optional_operations(target: TerminalHost) -> None:
    target.marker(
        Rect(0, 0, 16, 16), Style(foreground="#ffffff"), shape="square", checked=True
    )
    target.reload_image("example.png")
    if hasattr(target, "lines"):
        target.lines([(0, 0), (8, 16)], "#ffffff")
    if hasattr(target, "measure_many"):
        target.measure_many(["a", "b"], 16)


assert_type(terminal(renderer="python", hidden=True), TerminalHost)
