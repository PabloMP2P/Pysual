"""Desktop file services. Dialog processes are awaited, never shell-interpolated."""

import asyncio
import os
import shutil
import stat
import sys
import tempfile
import threading
from pathlib import Path

from ..host import MAX_TEXT_BYTES, CapabilityError, TextFile


def dialog_available():
    return bool(
        shutil.which("osascript")
        if sys.platform == "darwin"
        else shutil.which("powershell.exe")
        if sys.platform == "win32"
        else shutil.which("zenity") or shutil.which("kdialog")
    )


async def choose_path(save=False, suggested_name="document.txt"):
    env = dict(os.environ, PYSUAL_FILENAME=suggested_name)
    if sys.platform == "darwin":
        expression = (
            'POSIX path of (choose file name default name (system attribute "PYSUAL_FILENAME"))'
            if save
            else "POSIX path of (choose file)"
        )
        command = ["osascript", "-e", expression]
    elif sys.platform == "win32":
        dialog = "SaveFileDialog" if save else "OpenFileDialog"
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            f"$dialog = New-Object System.Windows.Forms.{dialog}; "
            "$dialog.FileName = $env:PYSUAL_FILENAME; "
            "if ($dialog.ShowDialog() -eq 'OK') { [Console]::Write($dialog.FileName) }"
        )
        command = ["powershell.exe", "-NoProfile", "-STA", "-Command", script]
    elif shutil.which("zenity"):
        command = ["zenity", "--file-selection"]
        if save:
            command += ["--save", "--confirm-overwrite", f"--filename={suggested_name}"]
    elif shutil.which("kdialog"):
        command = ["kdialog", "--getsavefilename" if save else "--getopenfilename"]
        command += [suggested_name if save else str(Path.home())]
    else:
        raise CapabilityError(
            "Install zenity or kdialog to enable desktop file dialogs"
        )
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise CapabilityError("The platform file dialog could not start") from exc
    communication = asyncio.create_task(process.communicate())
    try:
        out, err = await asyncio.shield(communication)
    except asyncio.CancelledError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        while not communication.done():
            try:
                await asyncio.shield(communication)
            except asyncio.CancelledError:
                continue
        communication.result()
        raise
    if process.returncode:
        # Zenity/kdialog cancellation is 1; AppleScript uses -128 for cancellation.
        if (sys.platform != "darwin" and process.returncode == 1) or b"(-128)" in err:
            return None
        raise CapabilityError(
            "The platform file dialog failed: " + err.decode("utf-8", "replace").strip()
        )
    value = out.decode("utf-8").rstrip("\r\n")
    return Path(value) if value else None


def _read_text(path, strip_bom=True):
    path = Path(path)
    with path.open("rb") as stream:
        data = stream.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError("Text files must be at most 8 MiB")
    return TextFile(
        path.name,
        data.decode("utf-8-sig" if strip_bom else "utf-8"),
        str(path.absolute()),
    )


async def read_text(path, *, strip_bom=True):
    """Read and decode without blocking the UI; cancellation has no side effects."""
    return await asyncio.to_thread(_read_text, path, strip_bom)


def _discard_temporary(path):
    """Remove only our temporary, without hiding a preparation/commit error."""
    primary = sys.exception()
    path = Path(path)
    try:
        try:
            path.unlink(missing_ok=True)
        except PermissionError:
            mode = path.stat().st_mode
            if os.name != "nt" or mode & stat.S_IWRITE:
                raise
            path.chmod(mode | stat.S_IWRITE)
            path.unlink(missing_ok=True)
    except OSError as error:
        if primary is None:
            raise
        primary.add_note(f"Temporary-file cleanup also failed: {error}")


def _prepare_write(path, text, cancelled):
    """The worker owns only a temporary file, never the destination."""
    data = text.encode("utf-8")
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError("Text files must be at most 8 MiB")
    if cancelled.is_set():
        return None
    fd, temporary = tempfile.mkstemp(prefix=".pysual-", dir=path.parent)
    complete = False
    try:
        with os.fdopen(fd, "wb") as stream:
            for offset in range(0, len(data), 64 * 1024):
                if cancelled.is_set():
                    return None
                stream.write(data[offset : offset + 64 * 1024])
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        if cancelled.is_set():
            return None
        complete = True
        return Path(temporary)
    finally:
        if not complete:
            _discard_temporary(temporary)


async def write_text(path, text):
    """Prepare off-thread, then atomically commit without an intervening await.

    Cancellation waits for worker cleanup and cannot leave a late replacement.
    Once replacement starts, callers receive the successful file outcome.
    """
    path = Path(path)
    cancelled = threading.Event()
    preparation = asyncio.create_task(
        asyncio.to_thread(_prepare_write, path, text, cancelled)
    )
    temporary = None
    try:
        try:
            temporary = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            cancelled.set()
            # Repeated owner cancellation must not detach a worker with a file.
            while not preparation.done():
                try:
                    await asyncio.shield(preparation)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not preparation.cancelled() and preparation.exception() is None:
                temporary = preparation.result()
            raise
        if temporary is None:
            raise asyncio.CancelledError
        os.replace(temporary, path)
        return TextFile(path.name, text, str(path.absolute()))
    finally:
        if temporary is not None:
            _discard_temporary(temporary)
