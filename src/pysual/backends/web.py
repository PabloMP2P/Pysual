"""The SVG web backend for native Python and self-contained browser apps."""

import sys
from typing import TypedDict, Unpack

from ..host import Host


class _WebOptions(TypedDict, total=False):
    open_browser: bool
    host: str
    port: int


def WebHost(**options: Unpack[_WebOptions]) -> Host:
    """Create an SVG host for the current Python runtime.

    Native Python serves a loopback browser tab. In Pyodide the same SVG scene
    is delivered directly to the embedded browser bridge. Native delivery
    accepts ``open_browser=False`` and ``port=...`` for application integration.
    """
    if sys.platform == "emscripten":
        if options:
            raise TypeError("Browser delivery does not accept native server options")
        from ._web_pyodide import BundledSVGHost
        return BundledSVGHost()
    from ._web_native import LiveSVGHost
    return LiveSVGHost(**options)


def create_host() -> Host:
    return WebHost()
