"""Keep a native Windows dialog owner usable when its helper is terminated."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from ..host import CapabilityError

if TYPE_CHECKING:
    import ctypes
    from _ctypes import CFuncPtr


class _WindowAPI(Protocol):
    def find_owner(self, process_id: int) -> int: ...
    def process_id(self, handle: int) -> int: ...
    def enabled(self, handle: int) -> bool: ...
    def enable(self, handle: int) -> None: ...


class _User32:
    _dll: ctypes.CDLL
    _callback: type[CFuncPtr]

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise CapabilityError("Native Windows dialog ownership requires Windows")
        import ctypes
        from ctypes import wintypes

        self._dll = ctypes.WinDLL("user32", use_last_error=True)
        self._callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self._dll.EnumWindows.argtypes = [self._callback, wintypes.LPARAM]
        self._dll.EnumWindows.restype = wintypes.BOOL
        self._dll.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        self._dll.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._dll.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self._dll.GetWindow.restype = wintypes.HWND
        self._dll.IsWindowVisible.argtypes = [wintypes.HWND]
        self._dll.IsWindowVisible.restype = wintypes.BOOL
        self._dll.IsWindowEnabled.argtypes = [wintypes.HWND]
        self._dll.IsWindowEnabled.restype = wintypes.BOOL
        self._dll.EnableWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
        self._dll.EnableWindow.restype = wintypes.BOOL

    def process_id(self, handle: int) -> int:
        import ctypes
        from ctypes import wintypes

        process_id = wintypes.DWORD()
        if not self._dll.GetWindowThreadProcessId(handle, ctypes.byref(process_id)):
            return 0
        return process_id.value

    def find_owner(self, process_id: int) -> int:
        handle = 0

        def visit(window: int, _parameter: int) -> bool:
            nonlocal handle
            # NativeHost has one visible top-level window per native process.
            if (
                self.process_id(window) == process_id
                and self._dll.IsWindowVisible(window)
                and not self._dll.GetWindow(window, 4)  # GW_OWNER
            ):
                handle = window
                return False
            return True

        completed = self._dll.EnumWindows(self._callback(visit), 0)
        if not completed and not handle:
            raise CapabilityError("The native dialog owner could not be located")
        return handle

    def enabled(self, handle: int) -> bool:
        return bool(self._dll.IsWindowEnabled(handle))

    def enable(self, handle: int) -> None:
        self._dll.EnableWindow(handle, True)


@dataclass(frozen=True)
class OwnerWindow:
    handle: int
    process_id: int
    was_enabled: bool
    _api: _WindowAPI = field(repr=False, compare=False)

    def restore(self) -> None:
        # TerminateProcess skips the common dialog's normal modal cleanup.
        # Do not touch a closed/replaced window or one disabled before we began.
        if (
            self.was_enabled
            and self._api.process_id(self.handle) == self.process_id
            and not self._api.enabled(self.handle)
        ):
            self._api.enable(self.handle)


def capture_owner(process_id: int) -> OwnerWindow:
    api = _User32()
    handle = api.find_owner(process_id)
    if not handle or api.process_id(handle) != process_id:
        raise CapabilityError("The native dialog owner no longer has a window")
    return OwnerWindow(handle, process_id, api.enabled(handle), api)
