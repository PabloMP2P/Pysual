"""Reusable VT session, input and OS services, independent of drawing targets."""

import asyncio
import atexit
import base64
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any

from ..host import MAX_TEXT_BYTES, CapabilityError, Input, Viewport
from ._native import NativeServices
from ._term_input import TerminalInput
from ._term_io import TerminalIO


@dataclass
class TerminalFeatures:
    protocol: str = "halfblock"
    columns: int = 100
    rows: int = 36
    pixel_width: int = 0
    pixel_height: int = 0
    cell_width: float = 8
    cell_height: float = 16
    pixel_mouse: bool = False
    kitty_keyboard: bool = False
    dimensions_reported: bool = False


_PROBE = (
    b"\x1b_Gi=31,s=1,v=1,a=q,t=d,f=24;AAAA\x1b\\"
    b"\x1b[c\x1b[14t\x1b[16t\x1b[18t\x1b[?1016$p\x1b[?80$p\x1b[?u"
)
_ENTER = (
    b"\x1b[22;0t\x1b[?1049h\x1b[?25l\x1b[?7l"
    b"\x1b[?1003h\x1b[?1006h\x1b[?1004h\x1b[?2004h\x1b[2J\x1b[H"
)
_LEAVE = (
    b"\x1b[?2026l\x1b[?1016l\x1b[?1003l\x1b[?1002l\x1b[?1000l"
    b"\x1b[?1006l\x1b[?1004l\x1b[?2004l\x1b[0m\x1b[?7h"
    b"\x1b[?25h\x1b[?1049l\x1b[23;0t"
)

# Sessions open on the UI owner thread, but only the main thread may install
# signal handlers. The window gateways install one process-wide handler from
# the caller's thread; it restores every open session before the process ends.
_open_sessions: "weakref.WeakSet[TerminalSession]" = weakref.WeakSet()
_restoration_signals: dict[int, Any] = {}
_RESTORATION_NAMES = ("SIGTERM", "SIGHUP")


def install_signal_restoration() -> bool:
    """Restore open terminals on SIGTERM/SIGHUP; returns whether handlers are owned.

    A no-op outside the main thread, where Python forbids signal handlers, and
    idempotent on the main thread. Keyboard interrupts already unwind through
    the caller's wait and the session's atexit hook.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    if _restoration_signals:
        return True
    for name in _RESTORATION_NAMES:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            _restoration_signals[signum] = signal.signal(signum, _restore_terminals)
        except (ValueError, OSError):
            continue
    return bool(_restoration_signals)


def _restore_terminals(signum, frame):
    for session in tuple(_open_sessions):
        try:
            session.close()
        except Exception:
            pass  # Console restoration is best effort while the process ends.
    previous = _restoration_signals.get(signum)
    if callable(previous):
        previous(signum, frame)
    elif previous != signal.SIG_IGN:
        raise SystemExit(128 + int(signum))


def _reset_signal_restoration() -> None:
    """Return the previous handlers; used by tests on the main thread."""
    if threading.current_thread() is not threading.main_thread():
        return
    for signum, previous in _restoration_signals.items():
        if signal.getsignal(signum) is _restore_terminals:
            signal.signal(signum, previous)
    _restoration_signals.clear()


class TerminalSession(NativeServices):
    """Mode ownership and services shared by pixel and native text hosts."""

    capabilities = frozenset({"clipboard", "session_storage", "open_url", "text_files"})
    PROBE = _PROBE
    GEOMETRY_QUERY = b"\x1b[14t\x1b[16t\x1b[18t"

    def __init__(self, *, io=None, protocol=None, probe_timeout=0.15, environ=None):
        if protocol not in (None, "kitty", "sixel", "iterm", "halfblock", "text"):
            raise ValueError("Invalid terminal session protocol")
        if not 0 <= probe_timeout <= 2:
            raise ValueError("probe_timeout must be between zero and two seconds")
        self.io = TerminalIO() if io is None else io
        self.features = TerminalFeatures()
        self.size = (800.0, 576.0)
        self.scale = 1.0
        self.resource_revision = 0
        self._forced_protocol = protocol
        self._probe_timeout = probe_timeout
        self._environ = dict(os.environ if environ is None else environ)
        self._parser = TerminalInput()
        self._pending = []
        self._resource_events: deque[Input] = deque(maxlen=32)
        self._image_errors: OrderedDict[tuple[int, str], None] = OrderedDict()
        self._active = False
        self._title = ""
        self._previous_frame = None
        self._last_dimensions = None
        self._last_size_check = 0.0
        self._last_size_query = 0.0
        self._session_lock = asyncio.Lock()
        self._clipboard_lock = asyncio.Lock()
        self._clipboard_reply = None
        self._session_generation = 0
        self._explicit_scale = None
        self._sixel_mode = False
        self._sixel_restore = False
        # Own one image slot, never issue a terminal-wide image deletion.
        self._image_id = (os.getpid() % 1000000) * 2 + 100

    @property
    def viewport(self):
        return Viewport(*self.size, self.scale)

    @property
    def render_scale(self):
        return self.scale

    @property
    def protocol(self):
        return self.features.protocol

    def _responses(self):
        f = self.features
        responses, self._parser.replies = self._parser.replies, []
        for response in responses:
            if re.fullmatch(rb"\x1b_Gi=31;OK\x1b\\", response):
                f.protocol = "kitty"
            elif match := re.fullmatch(rb"\x1b\[\?([\d;]+)c", response):
                values = [int(n) for n in match[1].split(b";")]
                if values[0] >= 60 and 4 in values[1:] and f.protocol != "kitty":
                    f.protocol = "sixel"
            elif match := re.fullmatch(rb"\x1b\[(4|6|8);(\d+);(\d+)t", response):
                kind, height, width = map(int, match.groups())
                if not (0 < width <= 8192 and 0 < height <= 8192):
                    continue
                if kind == 4:
                    f.pixel_width, f.pixel_height = width, height
                    f.dimensions_reported = True
                elif kind == 6:
                    f.cell_width, f.cell_height = width, height
                    f.pixel_width, f.pixel_height = f.columns * width, f.rows * height
                    f.dimensions_reported = True
                elif kind == 8:
                    f.columns, f.rows = width, height
            elif match := re.fullmatch(rb"\x1b\[\?1016;([0-4])\$y", response):
                f.pixel_mouse = int(match[1]) in (1, 2)
            elif match := re.fullmatch(rb"\x1b\[\?80;([0-4])\$y", response):
                self._sixel_restore = int(match[1]) in (1, 3)
            elif re.fullmatch(rb"\x1b\[\?\d+u", response):
                f.kitty_keyboard = True
            elif response.startswith(b"\x1b]52;"):
                future = self._clipboard_reply
                if future is not None and not future.done():
                    try:
                        end = -1 if response.endswith(b"\x07") else -2
                        encoded = response[5:end].split(b";", 1)[1]
                        value = base64.b64decode(encoded, validate=True)
                        if len(value) > MAX_TEXT_BYTES:
                            raise ValueError("Clipboard text exceeds 8 MiB")
                        future.set_result(value.decode("utf-8"))
                    except (ValueError, UnicodeError, IndexError) as exc:
                        future.set_exception(
                            CapabilityError(
                                f"Invalid terminal clipboard response: {exc}"
                            )
                        )
        if self._forced_protocol is not None:
            f.protocol = self._forced_protocol

    def _reset_open_state(self):
        # A reusable Host can enter a different terminal on its next run.
        # Capabilities, partial input and owned modes belong to one opening;
        # constructor options and the monotonic resource revision persist.
        self._session_generation += 1
        self.features = TerminalFeatures()
        self._parser = TerminalInput()
        self._pending.clear()
        self._resource_events.clear()
        self._image_errors.clear()
        self._title = ""
        self._previous_frame = None
        self._last_dimensions = None
        self._last_size_check = 0.0
        self._last_size_query = 0.0
        self._sixel_mode = False
        self._sixel_restore = False
        self._clipboard_reply = None
        self._clipboard_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()

    def open(self, title, width, height, resizable, scale):
        if self._active:
            raise RuntimeError("This terminal host is already open")
        if scale is not None:
            Viewport(width, height, scale)  # shared validation
        self._reset_open_state()
        self._explicit_scale = scale
        try:
            self.io.enter()
            self._active = True
            atexit.register(self.close)
            _open_sessions.add(self)
            self.io.write(_ENTER)
            self._read_dimensions()
            self.io.write(self.PROBE)
            deadline = time.monotonic() + self._probe_timeout
            while time.monotonic() < deadline:
                data = self.io.read(max(0.0, deadline - time.monotonic()))
                if data:
                    self._pending.extend(self._parser.feed(data))
                    self._responses()
                else:
                    break
            # iTerm's inline-file capability has no portable query. Only use
            # its identifying environment when no confirmed protocol exists.
            if (
                self.protocol == "halfblock"
                and not self._environ.get("TMUX")
                and not self._environ.get("STY")
                and self._environ.get("TERM_PROGRAM") in ("iTerm.app", "WezTerm")
            ):
                self.features.protocol = "iterm"
            if self._forced_protocol is not None:
                self.features.protocol = self._forced_protocol
            self._geometry()
            if self.features.pixel_mouse:
                self.io.write(b"\x1b[?1016h")
            if self.features.kitty_keyboard:
                self.io.write(b"\x1b[>3u")
            self.set_title(title)
        except BaseException:
            self.close()
            raise

    def _read_dimensions(self):
        facts = self.io.dimensions()
        if facts == self._last_dimensions:
            return False
        self._last_dimensions = facts
        columns, rows, width, height = facts
        f = self.features
        f.columns, f.rows = max(1, columns), max(1, rows)
        # Preserve a queried cell size across resize. Old window pixels must
        # not win over the new dimensions when ioctl reports only cells.
        f.pixel_width = width or round(f.columns * f.cell_width)
        f.pixel_height = height or round(f.rows * f.cell_height)
        return True

    def poll(self):
        if not self._active:
            return []
        result, self._pending = self._pending, []
        result.extend(self._resource_events)
        self._resource_events.clear()
        previous_features = (
            self.features.pixel_mouse,
            self.features.kitty_keyboard,
            self.protocol,
        )
        try:
            result.extend(self._parser.feed(self.io.read(0)))
            self._responses()
            now = time.monotonic()
            if now - self._last_size_check >= 0.1:
                changed = self._read_dimensions()
                self._last_size_check = now
                # Requery on cell resize and periodically for DPI/font changes
                # which preserve the number of rows and columns.
                if changed or now - self._last_size_query >= 2:
                    self.io.write(self.GEOMETRY_QUERY)
                    self._last_size_query = now
            if self._geometry():
                result.append(Input("viewport", viewport=self.viewport))
            old_mouse, old_keyboard, old_protocol = previous_features
            if self.features.pixel_mouse and not old_mouse:
                self.io.write(b"\x1b[?1016h")
            if self.features.kitty_keyboard and not old_keyboard:
                self.io.write(b"\x1b[>3u")
            if old_protocol != self.protocol:
                self._previous_frame = None
                result.append(Input("repaint"))
        except (EOFError, OSError):
            result.append(Input("close"))
        return result

    def set_title(self, title):
        title = "".join(
            ch for ch in str(title) if ord(ch) >= 32 and not 127 <= ord(ch) <= 159
        )[:256]
        if title != self._title:
            self._title = title
            if self._active:
                self.io.write(b"\x1b]2;" + title.encode("utf-8") + b"\x07")

    def set_size(self, width, height):
        # A terminal pane owns its size. Runtime synchronizes the application's
        # request back to this actual viewport, like a constrained native host.
        self._read_dimensions()
        self._geometry()

    def close(self):
        if not self._active:
            return
        self._active = False
        _open_sessions.discard(self)
        self._session_generation += 1
        try:
            tail = b""
            if self.features.kitty_keyboard:
                tail += b"\x1b[<u"
            tail += self._presentation_cleanup()
            self.io.write(tail + _LEAVE)
        except (OSError, EOFError):
            # A disconnected output cannot be repaired, but input flags still
            # have to be restored, including when output failed mid-frame.
            pass
        finally:
            try:
                self.io.exit()
            finally:
                atexit.unregister(self.close)
                self._previous_frame = None
                self._image_errors.clear()
                self._resource_events.clear()
                self._close_target()
                future = self._clipboard_reply
                if future is not None and not future.done():
                    future.set_exception(
                        CapabilityError("Terminal closed during clipboard access")
                    )

    def _clipboard_command(self, write=False):
        if self._environ.get("SSH_CONNECTION") or self._environ.get("SSH_TTY"):
            return None
        if sys.platform == "win32":
            executable = shutil.which("powershell.exe")
            if executable:
                script = (
                    "[Console]::InputEncoding=[System.Text.UTF8Encoding]::new($false);"
                    "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
                ) + (
                    "Set-Clipboard -Value ([Console]::In.ReadToEnd())"
                    if write
                    else "[Console]::Write((Get-Clipboard -Raw))"
                )
                return [executable, "-NoProfile", "-NonInteractive", "-Command", script]
        elif sys.platform == "darwin":
            executable = shutil.which("pbcopy" if write else "pbpaste")
            if executable:
                return [executable]
        elif self._environ.get("WAYLAND_DISPLAY"):
            executable = shutil.which("wl-copy" if write else "wl-paste")
            if executable:
                return (
                    [executable, "--type", "text/plain;charset=utf-8"]
                    if write
                    else [executable, "--no-newline"]
                )
        elif self._environ.get("DISPLAY"):
            executable = shutil.which("xclip")
            if executable:
                return [
                    executable,
                    "-selection",
                    "clipboard",
                    "-in" if write else "-out",
                ]
            executable = shutil.which("xsel")
            if executable:
                return [executable, "--clipboard", "--input" if write else "--output"]
        return None

    async def _clipboard_process(self, command, data=None):
        options: dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if sys.platform == "win32"
            else {}
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **options,
        )
        try:
            out, _ = await asyncio.wait_for(process.communicate(data), 3)
        except BaseException:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            raise
        if process.returncode:
            raise CapabilityError("The system clipboard is unavailable")
        if len(out) > MAX_TEXT_BYTES:
            raise CapabilityError("Clipboard text exceeds 8 MiB")
        return out.decode("utf-8")

    async def clipboard_read(self):
        generation = self._session_generation
        command = self._clipboard_command()
        if command:
            try:
                return await self._clipboard_process(command)
            except (TimeoutError, CapabilityError, OSError, UnicodeError):
                pass
        if not self._active or generation != self._session_generation:
            raise CapabilityError(
                "The terminal clipboard is unavailable before opening"
            )
        async with self._clipboard_lock:
            if not self._active or generation != self._session_generation:
                raise CapabilityError("Terminal closed during clipboard access")
            reply = asyncio.get_running_loop().create_future()
            self._clipboard_reply = reply
            try:
                self.io.write(b"\x1b]52;c;?\x07")
                return await asyncio.wait_for(reply, 0.75)
            except TimeoutError as exc:
                raise CapabilityError(
                    "The terminal denied clipboard reading; use its Paste command"
                ) from exc
            finally:
                if self._clipboard_reply is reply:
                    self._clipboard_reply = None

    async def clipboard_write(self, text):
        generation = self._session_generation
        data = text.encode("utf-8")
        if len(data) > MAX_TEXT_BYTES:
            raise CapabilityError("Clipboard text exceeds 8 MiB")
        command = self._clipboard_command(True)
        if command:
            try:
                await self._clipboard_process(command, data)
                return
            except (TimeoutError, CapabilityError, OSError, UnicodeError):
                pass
        if not self._active or generation != self._session_generation:
            raise CapabilityError(
                "The terminal clipboard is unavailable before opening"
            )
        self.io.write(b"\x1b]52;c;" + base64.b64encode(data) + b"\x07")

    def _geometry(self) -> bool:
        raise NotImplementedError

    def _presentation_cleanup(self) -> bytes:
        return b""

    def _close_target(self) -> None:
        pass
