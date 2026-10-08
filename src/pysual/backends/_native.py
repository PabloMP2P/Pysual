"""Operating-system services shared by native rendering backends."""

import asyncio
import hashlib
import os
import re
import sys
import webbrowser
from pathlib import Path

from ..host import CapabilityError, validate_session_key, validate_session_text
from . import _files


class NativeServices:
    """File dialogs, recovery storage and URL opening for native hosts.

    Hosts initialize ``_session_lock`` alongside their other instance state.
    """

    _session_lock: asyncio.Lock

    async def open_text_file(self):
        path = await _files.choose_path()
        return await _files.read_text(path) if path is not None else None

    async def save_text_file(self, text, suggested_name, location):
        path = location or await _files.choose_path(True, suggested_name)
        return await _files.write_text(path, text) if path is not None else None

    async def read_session(self, key):
        validate_session_key(key)
        try:
            return (
                await _files.read_text(self._session_path(key), strip_bom=False)
            ).text
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError) as exc:
            raise CapabilityError(
                "Desktop session storage is unavailable or invalid"
            ) from exc

    def set_session_namespace(self, name):
        self._session_app_id = name

    def _session_directory(self):
        if sys.platform == "win32":
            root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
        elif sys.platform == "darwin":
            root = Path.home() / "Library/Application Support"
        else:
            root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
        identity = getattr(self, "_session_app_id", None) or Path(sys.argv[0]).stem or "python"
        readable = re.sub(r"[^a-zA-Z0-9_-]", "-", identity).strip("-")[:40] or "python"
        namespace = "app-" + readable + "-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
        return root / "pysual/sessions" / namespace

    def _session_path(self, key):
        # Preserve case-sensitive key identity on Windows/macOS filesystems too.
        filename = hashlib.sha256(key.encode("ascii")).hexdigest() + ".session"
        return self._session_directory() / filename

    async def write_session(self, key, text):
        validate_session_key(key)
        validate_session_text(text)
        path = self._session_path(key)
        try:
            async with self._session_lock:
                if text is None:
                    path.unlink(missing_ok=True)
                else:

                    def prepare_directory():
                        path.parent.mkdir(parents=True, exist_ok=True)
                        files = [
                            entry
                            for entry in path.parent.iterdir()
                            if entry.is_file() and entry != path
                        ]
                        if (
                            len(files) >= 32
                            or sum(entry.stat().st_size for entry in files)
                            + len(text.encode("utf-8"))
                            > 32 * 1024 * 1024
                        ):
                            raise CapabilityError(
                                "Desktop session storage exceeds 32 entries or 32 MiB"
                            )

                    await asyncio.to_thread(prepare_directory)
                    await _files.write_text(path, text)
        except (OSError, UnicodeError) as exc:
            raise CapabilityError("Desktop session storage is unavailable") from exc

    async def open_url(self, url):
        try:
            opened = await asyncio.to_thread(webbrowser.open, url)
        except (OSError, webbrowser.Error) as exc:
            raise CapabilityError("The system could not open this URL") from exc
        if not opened:
            raise CapabilityError("No system URL opener is available")
