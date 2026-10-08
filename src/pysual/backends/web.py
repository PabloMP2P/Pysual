"""The SVG web backend for native Python and self-contained browser apps."""

import sys

from ..host import Host


def WebHost(**options) -> Host:
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
