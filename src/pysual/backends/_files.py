"""Desktop file services.

Dialog processes are awaited, never started through a shell. Caller text is
placed in the environment or in one argument, and it is never interpolated
into a script. Cancelling a dialog is not a failure.
"""

import asyncio
import atexit
import base64
import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

from ..host import (
    MAX_TEXT_BYTES,
    CapabilityError,
    PathDialog,
    TextFile,
    ensure_allowed_extension,
    prepare_path_dialog,
)
from ._windows_dialog import OwnerWindow, capture_owner


_ZENITY_CANCEL_CODE = 10


_WINDOWS_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try {
    Add-Type -AssemblyName System.Windows.Forms | Out-Null
    [System.Windows.Forms.Application]::EnableVisualStyles()
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [Console]::OutputEncoding = $utf8
    $OutputEncoding = $utf8
    $title = [Environment]::GetEnvironmentVariable('PYSUAL_TITLE')
    $initial = [Environment]::GetEnvironmentVariable('PYSUAL_INITIAL')
    $name = [Environment]::GetEnvironmentVariable('PYSUAL_FILENAME')
    $extensions = [Environment]::GetEnvironmentVariable('PYSUAL_EXTENSIONS')
    $allFiles = [Environment]::GetEnvironmentVariable('PYSUAL_ALL_FILES') -eq '1'
    $directory = [Environment]::GetEnvironmentVariable('PYSUAL_DIRECTORY') -eq '1'
    $save = [Environment]::GetEnvironmentVariable('PYSUAL_SAVE') -eq '1'
    $owner = $null
    $ownerPid = [Environment]::GetEnvironmentVariable('PYSUAL_OWNER_PID')
    $ownerHandle = [Environment]::GetEnvironmentVariable('PYSUAL_OWNER_HWND')
    if ($ownerPid) {
        Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Windows.Forms;
public sealed class PysualDialogOwner : IWin32Window {
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(IntPtr handle, out uint processId);
    private readonly IntPtr handle;
    public PysualDialogOwner(IntPtr handle, uint processId) {
        uint actualProcessId;
        if (GetWindowThreadProcessId(handle, out actualProcessId) == 0 || actualProcessId != processId) {
            throw new InvalidOperationException("The dialog owner no longer has a window");
        }
        this.handle = handle;
    }
    public IntPtr Handle { get { return handle; } }
}
'@ | Out-Null
        $owner = New-Object PysualDialogOwner -ArgumentList @([IntPtr]([long]$ownerHandle), [uint32]$ownerPid)
    }
    $dialog = $null
    try {
        $chosen = ''
        if ($directory) {
            $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
            $dialog.ShowNewFolderButton = $true
            if ($title) { $dialog.Description = $title }
            if ($initial) { $dialog.SelectedPath = $initial }
            if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
                $chosen = $dialog.SelectedPath
            }
        } else {
            if ($save) {
                $dialog = New-Object System.Windows.Forms.SaveFileDialog
                $dialog.OverwritePrompt = $true
                $dialog.CheckFileExists = $false
            } else {
                $dialog = New-Object System.Windows.Forms.OpenFileDialog
                $dialog.CheckFileExists = $true
            }
            $dialog.CheckPathExists = $true
            $dialog.RestoreDirectory = $true
            $dialog.AddExtension = $false
            $dialog.SupportMultiDottedExtensions = $true
            $dialog.ValidateNames = $true
            if ($title) { $dialog.Title = $title }
            if ($name) { $dialog.FileName = $name }
            if ($initial) { $dialog.InitialDirectory = $initial }
            if ($extensions) {
                $patterns = @()
                foreach ($item in ($extensions -split ';')) {
                    if ($item) { $patterns += ('*' + $item) }
                }
                if ($patterns.Count -gt 0) {
                    $pattern = $patterns -join ';'
                    $dialog.Filter = "$pattern|$pattern"
                    if ($allFiles) { $dialog.Filter += '|*.*|*.*' }
                    if ($save -and -not $allFiles) {
                        $dialog.DefaultExt = $extensions.Split(';')[0].TrimStart('.')
                        $dialog.AddExtension = $true
                    }
                }
            }
            if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
                $chosen = $dialog.FileName
            }
        }
        if ($chosen) { [Console]::Out.Write($chosen) }
    } finally {
        if ($null -ne $dialog) { $dialog.Dispose() }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 2
}
exit 0
"""

_APPLE_SCRIPT = """
use framework "AppKit"
use scripting additions
set dialogTitle to system attribute "PYSUAL_TITLE"
set suggested to system attribute "PYSUAL_FILENAME"
set startFolder to system attribute "PYSUAL_INITIAL"
set extensionText to system attribute "PYSUAL_EXTENSIONS"
set allowAny to (system attribute "PYSUAL_ALL_FILES") is "1"
set choosingFolder to (system attribute "PYSUAL_DIRECTORY") is "1"
set saving to (system attribute "PYSUAL_SAVE") is "1"

-- An unbundled osascript helper must opt in to activation before showing UI.
set dialogApp to current application's NSApplication's sharedApplication()
dialogApp's setActivationPolicy:(current application's NSApplicationActivationPolicyAccessory)

if saving and choosingFolder is false then
    set panel to current application's NSSavePanel's savePanel()
    if suggested is "" then set suggested to "untitled"
    panel's setNameFieldStringValue:suggested
else
    set panel to current application's NSOpenPanel's openPanel()
    panel's setCanChooseFiles:(not choosingFolder)
    panel's setCanChooseDirectories:choosingFolder
    panel's setAllowsMultipleSelection:false
end if
panel's setCanCreateDirectories:true
panel's setTreatsFilePackagesAsDirectories:choosingFolder
if dialogTitle is not "" then panel's setTitle:dialogTitle
if startFolder is not "" then
    panel's setDirectoryURL:(current application's NSURL's fileURLWithPath:startFolder)
end if

if extensionText is not "" and choosingFolder is false and allowAny is false then
    set savedDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to ";"
    set typeList to {}
    repeat with part in text items of extensionText
        set oneType to part as text
        if oneType starts with "." then set oneType to text 2 thru -1 of oneType
        if oneType is not "" then set end of typeList to oneType
    end repeat
    set AppleScript's text item delimiters to savedDelimiters
    -- Extensions also support custom types and macOS versions before UTType.
    panel's setAllowedFileTypes:typeList
end if
panel's setAllowsOtherFileTypes:allowAny
dialogApp's activateIgnoringOtherApps:true
set response to panel's runModal()
if response is not (current application's NSModalResponseOK) then error number -128
set chosenPath to panel's |URL|()'s |path|() as text
chosenPath
"""


@dataclass(frozen=True)
class DialogLaunch:
    """One dialog process. ``env`` carries caller text; ``argv`` does not build a shell."""

    argv: tuple[str, ...]
    env: dict[str, str]
    stdin: bytes | None
    tool: str


@dataclass(eq=False)
class _ActiveDialog:
    loop: asyncio.AbstractEventLoop
    owner: object | None
    process: asyncio.subprocess.Process | None = None
    killed: bool = False
    cancel_requested: bool = False


_active_dialogs: set[_ActiveDialog] = set()
_registry_lock = threading.Lock()
_dialog_locks: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, weakref.ReferenceType[asyncio.Lock]
] = weakref.WeakKeyDictionary()


def _dialog_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _registry_lock:
        reference = _dialog_locks.get(loop)
        lock = reference() if reference is not None else None
        if lock is None:
            lock = asyncio.Lock()
            _dialog_locks[loop] = weakref.ref(lock)
        return lock


def _finder(
    which: Callable[[str], str | None] | None,
) -> Callable[[str], str | None]:
    return shutil.which if which is None else which


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _dialog_problem(
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    system = sys.platform if platform is None else platform
    find = _finder(which)
    env = _environment(environ)
    if system == "win32":
        if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
            return "The platform file dialog requires a local Windows desktop session"
        if find("powershell.exe"):
            return None
        return "The platform file dialog requires Windows PowerShell"
    if system == "darwin":
        if env.get("SSH_CONNECTION") or env.get("SSH_TTY"):
            return "The platform file dialog requires a local macOS desktop session"
        if find("osascript"):
            return None
        return "The platform file dialog requires osascript"
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        return "The platform file dialog requires a graphical desktop session"
    if find("zenity") or find("kdialog"):
        return None
    return "Install zenity or kdialog to enable desktop file dialogs"


def dialog_available(
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """True when this process can show a system file or folder dialog."""
    return _dialog_problem(platform=platform, which=which, environ=environ) is None


def unavailable_reason(
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str:
    return (
        _dialog_problem(platform=platform, which=which, environ=environ)
        or "The platform file dialog is not available"
    )


def _require_tool(
    find: Callable[[str], str | None], name: str, message: str
) -> str:
    located = find(name)
    if not located:
        raise CapabilityError(message)
    return located


def _slash(folder: str, platform: str) -> str:
    if folder.endswith(("/", "\\") if platform == "win32" else "/"):
        return folder
    return folder + ("\\" if platform == "win32" else "/")


def _join_path(folder: str, name: str, platform: str) -> str:
    return _slash(folder, platform) + name


def _home_directory(home: str | None) -> str:
    if home is not None:
        return home
    try:
        return str(Path.home())
    except (OSError, RuntimeError):
        return ""


def _dialog_env(request: PathDialog) -> dict[str, str]:
    return {
        "PYSUAL_TITLE": request.title,
        "PYSUAL_FILENAME": request.suggested_name,
        "PYSUAL_INITIAL": request.initial_directory or "",
        "PYSUAL_DIRECTORY": "1" if request.directory else "0",
        "PYSUAL_SAVE": "1" if request.save else "0",
        "PYSUAL_EXTENSIONS": ";".join(request.extensions),
        "PYSUAL_ALL_FILES": "1" if request.all_files else "0",
        "PYSUAL_OWNER_PID": "",
        "PYSUAL_OWNER_HWND": "",
    }


def _extension_patterns(extensions: tuple[str, ...], *, ignore_case: bool = False) -> str:
    if ignore_case:
        # GTK glob filters are case-sensitive on Unix, unlike our suffix API.
        return " ".join(
            "*" + "".join(
                f"[{letter.lower()}{letter.upper()}]" if letter.isalpha() else letter
                for letter in suffix
            )
            for suffix in extensions
        )
    return " ".join("*" + suffix for suffix in extensions)


def _zenity_filename(request: PathDialog) -> str | None:
    if request.directory:
        if not request.initial_directory:
            return None
        return _slash(request.initial_directory, "linux")
    if request.save:
        if request.initial_directory and request.suggested_name:
            return _join_path(request.initial_directory, request.suggested_name, "linux")
        if request.suggested_name:
            return request.suggested_name
        if request.initial_directory:
            return _slash(request.initial_directory, "linux")
        return None
    if request.initial_directory:
        return _slash(request.initial_directory, "linux")
    return None


def _kdialog_start(request: PathDialog, home: str) -> str:
    if request.directory:
        return request.initial_directory or home or "."
    if request.save:
        if request.initial_directory and request.suggested_name:
            return _join_path(request.initial_directory, request.suggested_name, "linux")
        if request.suggested_name:
            return _join_path(home or os.getcwd(), request.suggested_name, "linux")
        return request.initial_directory or home or "."
    return request.initial_directory or home or "."


def _linux_tool(find: Callable[[str], str | None]) -> tuple[str, str]:
    zenity = find("zenity")
    if zenity:
        return "zenity", zenity
    kdialog = find("kdialog")
    if kdialog:
        return "kdialog", kdialog
    raise CapabilityError("Install zenity or kdialog to enable desktop file dialogs")


def plan_dialog(
    request: PathDialog,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] | None = None,
    home: str | None = None,
) -> DialogLaunch:
    """Build the dialog process for ``platform`` without starting it."""
    system = sys.platform if platform is None else platform
    find = _finder(which)
    env = _dialog_env(request)
    if system == "win32":
        executable = _require_tool(
            find, "powershell.exe", "The platform file dialog requires Windows PowerShell"
        )
        encoded = base64.b64encode(_WINDOWS_SCRIPT.encode("utf-16le")).decode("ascii")
        return DialogLaunch(
            (executable, "-STA", "-NoProfile", "-EncodedCommand", encoded),
            env,
            None,
            "powershell",
        )
    if system == "darwin":
        executable = _require_tool(
            find, "osascript", "The platform file dialog requires osascript"
        )
        return DialogLaunch(
            (executable, "-s", "he", "-"), env, _APPLE_SCRIPT.encode("utf-8"), "osascript"
        )
    tool, executable = _linux_tool(find)
    if tool == "zenity":
        # Older GLib versions transcode g_print even when stdout is a pipe.
        # Match Python's UTF-8 argv, preserving explicit filename encodings.
        if (
            sys.getfilesystemencoding().lower().replace("-", "") == "utf8"
            and "G_FILENAME_ENCODING" not in os.environ
            and "G_BROKEN_FILENAMES" not in os.environ
        ):
            env["CHARSET"] = "UTF-8"
        # Zenity inherits configurable exit codes, including DIALOG_* aliases.
        # Keep user cancellation distinct from GTK startup failures (status 1).
        env.update({
            "ZENITY_OK": "0",
            "ZENITY_CANCEL": str(_ZENITY_CANCEL_CODE),
            "ZENITY_ESC": str(_ZENITY_CANCEL_CODE),
            "ZENITY_EXTRA": str(_ZENITY_CANCEL_CODE),
            "ZENITY_ERROR": "11",
            "ZENITY_TIMEOUT": "12",
        })
        argv = [executable, "--file-selection"]
        if request.title:
            argv.append("--title=" + request.title)
        if request.directory:
            argv.append("--directory")
        elif request.save:
            argv.extend(("--save", "--confirm-overwrite"))
        filename = _zenity_filename(request)
        if filename:
            argv.append("--filename=" + filename)
        if request.extensions and not request.directory:
            argv.append(
                "--file-filter=Allowed files | "
                + _extension_patterns(request.extensions, ignore_case=True)
            )
            if request.all_files:
                argv.append("--file-filter=All files | *")
        return DialogLaunch(tuple(argv), env, None, "zenity")
    argv = [executable]
    if request.title:
        argv.append("--title=" + request.title)
    if request.directory:
        argv.append("--getexistingdirectory")
    elif request.save:
        argv.append("--getsavefilename")
    else:
        argv.append("--getopenfilename")
    argv.extend(("--", _kdialog_start(request, _home_directory(home))))
    if request.extensions and not request.directory:
        file_filter = "Allowed files (" + _extension_patterns(request.extensions) + ")"
        if request.all_files:
            file_filter += ";;All files (*)"
        argv.append(file_filter)
    return DialogLaunch(tuple(argv), env, None, "kdialog")


def _local_dialog_text(text: str) -> str:
    if not text.lower().startswith("file://"):
        if re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", text) and not Path(text).is_absolute():
            raise CapabilityError("The platform file dialog returned a non-local path")
        return text
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise CapabilityError("The platform file dialog returned an invalid path")
    try:
        split = urlsplit(text)
        path = unquote(split.path, errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise CapabilityError("The platform file dialog returned an invalid path") from exc
    if split.netloc.lower() not in ("", "localhost") or split.query or split.fragment:
        raise CapabilityError("The platform file dialog returned an invalid path")
    if (
        sys.platform == "win32"
        and path.startswith("/")
        and len(path) >= 3
        and path[2] == ":"
    ):
        path = path[1:]
    return path


def chosen_path(text: str) -> Path:
    """Normalize one dialog result to an absolute path without resolving links."""
    raw = _local_dialog_text(text)
    if (
        not raw
        or len(raw) > 32767
        or "\0" in raw
        or (
            sys.platform == "win32"
            and any(ord(character) < 32 or ord(character) == 127 for character in raw)
        )
    ):
        raise CapabilityError("The platform file dialog returned an invalid path")
    try:
        return Path(os.path.abspath(Path(raw).expanduser()))
    except (OSError, ValueError) as exc:
        raise CapabilityError("The platform file dialog returned an invalid path") from exc


def _dialog_text(tool: str, out: bytes) -> str:
    if out.startswith(b"\xef\xbb\xbf"):
        out = out[3:]
    try:
        # Linux tools print filesystem bytes, which need not be valid UTF-8.
        text = (
            os.fsdecode(out)
            if tool in ("zenity", "kdialog") and sys.platform != "win32"
            else out.decode("utf-8")
        )
    except UnicodeError as exc:
        raise CapabilityError("The platform file dialog returned an invalid path") from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    # POSIX helpers append one LF; CR/LF can also belong to a filename.
    if tool != "powershell":
        return text[:-1] if text.endswith("\n") else text
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith(("\n", "\r")):
        return text[:-1]
    return text


def _was_cancelled(tool: str, returncode: int, err: bytes) -> bool:
    if tool == "osascript":
        return returncode != 0 and re.search(rb"\(-128\)\s*$", err) is not None
    if tool == "zenity":
        return returncode == _ZENITY_CANCEL_CODE
    if tool == "kdialog":
        return returncode == 1
    return False


def interpret_dialog(tool: str, returncode: int, out: bytes, err: bytes) -> Path | None:
    """Return the chosen path, None for a user cancellation, or raise."""
    if _was_cancelled(tool, returncode, err):
        return None
    if returncode != 0:
        detail = err[:4096].decode("utf-8", "replace").strip()
        message = "The platform file dialog failed"
        if detail:
            message = message + ": " + detail[:500]
        raise CapabilityError(message)
    if len(out) > 4 * 32767 + 5:
        raise CapabilityError("The platform file dialog returned an invalid path")
    text = _dialog_text(tool, out)
    if not text:
        if tool == "powershell":
            return None
        raise CapabilityError("The platform file dialog returned an invalid path")
    raw = _local_dialog_text(text)
    if not os.path.isabs(raw):
        raise CapabilityError("The platform file dialog returned a relative path")
    return chosen_path(raw)


def classify_typed_path(path: Path, *, save: bool, directory: bool) -> str | None:
    """None accepts a typed path. ``confirm`` asks before replacing a file."""
    try:
        if directory:
            if path.is_dir():
                return None
            return "Choose an existing folder"
        if path.is_dir():
            return "Choose a file"
        if save:
            if path.exists():
                return "confirm"
            if path.parent.is_dir():
                return None
            return "The destination folder does not exist"
        if path.is_file():
            return None
        return "Choose an existing file"
    except OSError as exc:
        return (str(exc).strip() or "The path is not available")[:240]


def _stop(active: _ActiveDialog) -> None:
    process = active.process
    if process is not None and process.returncode is None:
        active.killed = True
        try:
            process.kill()
        except OSError:
            pass


def cancel_dialog(owner: object | None = None) -> None:
    """Kill the dialog ``owner`` opened. Omit ``owner`` while the process exits."""
    with _registry_lock:
        active_dialogs = tuple(
            active for active in _active_dialogs
            if owner is None or active.owner is owner
        )
        for active in active_dialogs:
            active.cancel_requested = True
    for active in active_dialogs:
        # Hosts may close on a different thread from their asyncio subprocess.
        if active.loop.is_running():
            with contextlib.suppress(RuntimeError):
                active.loop.call_soon_threadsafe(_stop, active)
        else:
            _stop(active)


atexit.register(cancel_dialog)


async def _spawn(argv: tuple[str, ...], env: dict[str, str]) -> asyncio.subprocess.Process:
    try:
        if sys.platform == "win32":
            return await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        return await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, NotImplementedError) as exc:
        raise CapabilityError("The platform file dialog could not start") from exc


async def _settle(task: asyncio.Task[object]) -> None:
    """Finish cleanup even if the caller is cancelled more than once."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break


async def _execute(
    launch: DialogLaunch, active: _ActiveDialog
) -> tuple[int, bytes, bytes, bool]:
    env = dict(os.environ)
    env.update(launch.env)
    spawning = asyncio.create_task(_spawn(launch.argv, env))
    communication: asyncio.Task[tuple[bytes, bytes]] | None = None
    try:
        try:
            # Cancellation during create_subprocess_exec must not orphan a
            # process whose handle has not yet been delivered to this task.
            process = await asyncio.shield(spawning)
            active.process = process
            communication = asyncio.create_task(process.communicate(launch.stdin))
            if active.cancel_requested:
                _stop(active)
            output = await asyncio.shield(communication)
        except asyncio.CancelledError:
            await _settle(spawning)
            if not spawning.cancelled() and spawning.exception() is None:
                process = spawning.result()
                active.process = process
                _stop(active)
                if communication is None:
                    communication = asyncio.create_task(process.communicate(launch.stdin))
                await _settle(communication)
                with contextlib.suppress(Exception):
                    communication.result()
            raise
        out, err = output
        code = process.returncode
        if code is None:
            raise CapabilityError("The platform file dialog did not finish")
        return code, out or b"", err or b"", active.killed
    except Exception:
        if active.process is not None:
            _stop(active)
            waiting = asyncio.create_task(active.process.wait())
            await _settle(waiting)
            with contextlib.suppress(Exception):
                waiting.result()
        raise
    finally:
        active.process = None


async def choose_path(
    save: bool = False,
    suggested_name: str = "document.txt",
    *,
    directory: bool = False,
    title: str = "",
    initial_directory: str | None = None,
    extensions: Sequence[str] = (),
    all_files: bool = False,
    owner: object | None = None,
    owner_process_id: int | None = None,
    accept: Callable[[], bool] | None = None,
) -> Path | None:
    """Show one system dialog. None means the user cancelled or the host closed."""
    request = prepare_path_dialog(
        save=save,
        directory=directory,
        suggested_name=suggested_name,
        title=title,
        initial_directory=initial_directory,
        extensions=extensions,
        all_files=all_files,
    )
    if not dialog_available():
        raise CapabilityError(unavailable_reason())
    if owner_process_id is not None and (
        type(owner_process_id) is not int or not 0 < owner_process_id <= 0x7FFFFFFF
    ):
        raise ValueError("owner_process_id must be a positive process ID")
    active = _ActiveDialog(asyncio.get_running_loop(), owner)
    with _registry_lock:
        _active_dialogs.add(active)
    try:
        async with _dialog_lock():
            if active.cancel_requested or (accept is not None and not accept()):
                return None
            while True:
                launch = plan_dialog(request)
                window: OwnerWindow | None = None
                if launch.tool == "powershell" and owner_process_id is not None:
                    window = capture_owner(owner_process_id)
                    launch = replace(
                        launch, env={
                            **launch.env,
                            "PYSUAL_OWNER_PID": str(window.process_id),
                            "PYSUAL_OWNER_HWND": str(window.handle),
                        },
                    )
                try:
                    code, out, err, killed = await _execute(launch, active)
                finally:
                    # _execute reaps the helper before returning or propagating
                    # cancellation, so it cannot disable the owner again.
                    if window is not None:
                        window.restore()
                if killed or active.cancel_requested or (accept is not None and not accept()):
                    return None
                chosen = interpret_dialog(launch.tool, code, out, err)
                if chosen is None:
                    return None
                result = ensure_allowed_extension(
                    chosen, save=request.save, extensions=request.extensions,
                    all_files=request.all_files,
                )
                if result == chosen:
                    return result
                # A helper may not append the suffix itself. Have the user
                # accept the actual destination, including its overwrite
                # confirmation, before returning a different path.
                request = replace(
                    request, suggested_name=result.name, initial_directory=str(result.parent),
                )
    finally:
        with _registry_lock:
            _active_dialogs.discard(active)


def _read_text(path: str | Path, strip_bom: bool = True) -> TextFile:
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


async def read_text(path: str | Path, *, strip_bom: bool = True) -> TextFile:
    """Read and decode without blocking the UI; cancellation has no side effects."""
    return await asyncio.to_thread(_read_text, path, strip_bom)


def _discard_temporary(path: str | Path) -> None:
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


def _prepare_write(path: Path, text: str, cancelled: threading.Event) -> Path | None:
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


async def write_text(path: str | Path, text: str) -> TextFile:
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
