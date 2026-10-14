"""Forwarded terminal methods retain precise argument and result types."""

from pysual import Rect, terminal

target = terminal(renderer="python", hidden=True)
target.begin(123)  # error: reportArgumentType
target.rect(Rect(0, 0, 16, 16), 123)  # error: reportArgumentType
target.text("Hello", 0, 0, "#ffffff", "large")  # error: reportArgumentType
target.image("image.png", Rect(0, 0, 16, 16), fit="invalid")  # error: reportArgumentType
target.present("extra")  # error: reportCallIssue
target.poll("extra")  # error: reportCallIssue
measurement: str = target.measure("Hello", 16)  # error: reportAssignmentType
target.surface_create("not a rectangle")  # error: reportArgumentType


async def invalid_service() -> None:
    await target.clipboard_write(123)  # error: reportArgumentType
    await target.write_session("document", 123)  # error: reportArgumentType
