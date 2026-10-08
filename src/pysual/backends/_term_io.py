"""Owned terminal modes and nonblocking input using only Python's stdlib."""

import os
import queue
import select
import shutil
import struct
import sys
import threading
import time
from typing import Any

from ..host import CapabilityError


class TerminalIO:
    """A byte transport; tests may inject any object with these five methods.

    Windows uses ReadConsoleW in a cancellable worker because select() cannot
    wait on console handles. VT input is read through the console stream, not
    CRT getwch(), which bypasses VT keyboard/mouse translation on Windows.
    """

    def __init__(self, stdin=None, stdout=None):
        self.stdin = sys.stdin if stdin is None else stdin
        self.stdout = sys.stdout if stdout is None else stdout
        self._active = False
        self._saved: Any = None
        self._thread = None
        self._stop = threading.Event()
        self._input = queue.Queue(maxsize=32)

    def _enqueue(self, value):
        # ReadConsoleW can deliver large pastes while the app paints. Apply
        # bounded backpressure without stranding cleanup on a blocked put().
        while not self._stop.is_set():
            try:
                self._input.put(value, timeout=0.05)
                return
            except queue.Full:
                continue

    def enter(self):
        if self._active:
            return
        if self._thread is not None:
            if self._thread.is_alive():
                raise CapabilityError(
                    "A previous terminal input reader is still running"
                )
            self._thread = None
        if not self.stdin.isatty() or not self.stdout.isatty():
            raise CapabilityError(
                "The terminal backend needs an interactive terminal on stdin and stdout"
            )
        self._in_fd, self._out_fd = self.stdin.fileno(), self.stdout.fileno()
        self.stdout.flush()
        # Discard only bytes already consumed by the previous application.
        # The OS input queue is preserved, including typing during this open.
        self._input = queue.Queue(maxsize=32)
        if sys.platform == "win32":
            self._enter_windows()
        else:
            import termios
            import tty

            self._saved = termios.tcgetattr(self._in_fd)
            # TCSANOW does not discard input typed while the application opens.
            tty.setraw(self._in_fd, termios.TCSANOW)
            self._active = True

    def _enter_windows(self):
        if sys.platform != "win32":
            raise CapabilityError("Windows console input requires Windows")
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetConsoleMode.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.ReadConsoleW.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        ]
        kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenThread.restype = wintypes.HANDLE
        kernel.CancelSynchronousIo.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.SetConsoleOutputCP.argtypes = [wintypes.UINT]
        self._kernel = kernel
        self._in_handle = msvcrt.get_osfhandle(self._in_fd)
        self._out_handle = msvcrt.get_osfhandle(self._out_fd)
        input_mode, output_mode = wintypes.DWORD(), wintypes.DWORD()
        if not kernel.GetConsoleMode(
            self._in_handle, ctypes.byref(input_mode)
        ) or not kernel.GetConsoleMode(self._out_handle, ctypes.byref(output_mode)):
            raise CapabilityError(
                "The terminal backend needs a Windows console with VT support"
            )
        self._saved = input_mode.value, output_mode.value, kernel.GetConsoleOutputCP()
        self._active = True
        try:
            # Disable cooked echo, Ctrl-C processing and Quick Edit. Keep other
            # input modes and explicitly enable EXTENDED_FLAGS + VT_INPUT.
            raw = (input_mode.value & ~(0x1 | 0x2 | 0x4 | 0x40)) | 0x80 | 0x200
            if not kernel.SetConsoleMode(self._in_handle, raw):
                raise CapabilityError("Windows could not enable VT terminal input")
            if not kernel.SetConsoleMode(
                self._out_handle, output_mode.value | 0x1 | 0x4
            ):
                raise CapabilityError("Windows could not enable VT terminal output")
            if not kernel.SetConsoleOutputCP(65001):
                raise CapabilityError("Windows could not enable UTF-8 terminal output")
            self._stop.clear()

            def read_console():
                pending_surrogate = ""
                while not self._stop.is_set():
                    buffer = ctypes.create_unicode_buffer(4096)
                    received = wintypes.DWORD()
                    if not kernel.ReadConsoleW(
                        self._in_handle, buffer, 4095, ctypes.byref(received), None
                    ):
                        if not self._stop.is_set():
                            self._enqueue(
                                OSError(ctypes.get_last_error(), "ReadConsoleW failed")
                            )
                        return
                    if not received.value:
                        continue
                    value = pending_surrogate + "".join(buffer[: received.value])
                    pending_surrogate = ""
                    if value and 0xD800 <= ord(value[-1]) <= 0xDBFF:
                        pending_surrogate, value = value[-1], value[:-1]
                    if value:
                        # ReadConsoleW counts UTF-16 code units; normalize any
                        # surrogate pair spanning reads before UTF-8 encoding.
                        value = value.encode("utf-16-le", "surrogatepass").decode(
                            "utf-16-le", "replace"
                        )
                        self._enqueue(value.encode("utf-8"))

            self._thread = threading.Thread(
                target=read_console, name="pysual-terminal-input", daemon=True
            )
            self._thread.start()
        except BaseException:
            self.exit()
            raise

    def read(self, timeout=0.0):
        if not self._active:
            return b""
        if sys.platform == "win32":
            try:
                value = self._input.get(timeout=max(0.0, timeout))
            except queue.Empty:
                return b""
            if isinstance(value, BaseException):
                raise value
            chunks = [value]
            # Bound a poll so a flood of input cannot starve rendering.
            for _ in range(63):
                try:
                    value = self._input.get_nowait()
                except queue.Empty:
                    break
                if isinstance(value, BaseException):
                    raise value
                chunks.append(value)
            return b"".join(chunks)
        ready, _, _ = select.select([self._in_fd], [], [], max(0.0, timeout))
        if not ready:
            return b""
        value = os.read(self._in_fd, 65536)
        if not value:
            raise EOFError("The terminal input was disconnected")
        return value

    def write(self, data):
        view = memoryview(data)
        while view:
            count = os.write(self._out_fd, view)
            if count <= 0:
                raise OSError("The terminal output was disconnected")
            view = view[count:]

    def dimensions(self):
        try:
            columns, rows = os.get_terminal_size(self.stdout.fileno())
        except (OSError, ValueError):
            columns, rows = shutil.get_terminal_size((100, 36))
        width = height = 0
        if sys.platform != "win32":
            import fcntl
            import termios

            try:
                rows, columns, width, height = struct.unpack(
                    "HHHH",
                    fcntl.ioctl(self.stdout.fileno(), termios.TIOCGWINSZ, bytes(8)),
                )
            except (OSError, ValueError):
                pass
        return max(1, columns), max(1, rows), width, height

    def exit(self):
        if not self._active and self._thread is None:
            return
        self._active = False
        if sys.platform == "win32":
            self._stop.set()
            failure = None
            if self._thread is not None:
                # Cancel only our thread's synchronous console read; never
                # flush the console input queue or inject a fake keypress.
                handle = self._kernel.OpenThread(0x1, False, self._thread.native_id)
                if handle:
                    try:
                        # A worker can pass its stop check immediately before
                        # cancellation, then enter ReadConsoleW afterwards.
                        # Retry cancellation until it actually terminates.
                        deadline = time.monotonic() + 1
                        while self._thread.is_alive() and time.monotonic() < deadline:
                            self._kernel.CancelSynchronousIo(handle)
                            self._thread.join(timeout=0.01)
                    finally:
                        self._kernel.CloseHandle(handle)
                if self._thread.is_alive():
                    failure = CapabilityError(
                        "Windows could not stop the terminal input reader"
                    )
                else:
                    self._thread = None
            if self._saved is not None:
                input_mode, output_mode, codepage = self._saved
                self._kernel.SetConsoleMode(self._in_handle, input_mode)
                self._kernel.SetConsoleMode(self._out_handle, output_mode)
                self._kernel.SetConsoleOutputCP(codepage)
                self._saved = None
            if failure is not None:
                # Preserve the live thread reference so this is visible and
                # exit() can retry; silently abandoning it could steal input
                # from the next shell prompt.
                raise failure
        else:
            import termios

            termios.tcsetattr(self._in_fd, termios.TCSANOW, self._saved)
        self._saved = None
