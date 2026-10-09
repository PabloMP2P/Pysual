# Window and terminal rendering

Pysual exposes three targets: `window`, `terminal`, and `web`. The native
window renderer is a standalone C11 process using SDL3, SDL3_ttf and SDL3_image.
Python owns controls, layout, theme rules and event handlers. C owns retained
drawing segments, text and image resources, damage repaint, style transitions,
sprite animation and presentation. Each window has its own helper process, so
SDL runs on its actual main thread and can present while Python is busy.

Native window images accept local PNG files and `data:image/png;base64,...`
sources. File content is copied into a buffer capped at 8 MiB; that same buffer
is checked and decoded, so changing the file cannot bypass the header check.
PNG dimensions are checked against the 32 MiB image budget before pixel
decoding, allowing eight bytes per pixel for 16-bit PNG channels. Actual decoded
row storage and the renderer's selected texture format are checked too. Other
formats, malformed images and oversized inputs produce a
`resource_error` event and leave the window running; convert other image
formats to PNG first. The window does not expose SDL_image's other decoders.
These bounds cover image input, pixels and retained resources, not all decoder
working memory or the helper's total memory. `native_stats()` includes
`image_decode_attempts`, excluding inputs rejected before decoding. The web
backend retains its separately documented image formats.

Both C backends accept 16-bit PNG only with RGBA channels (PNG color type 6).
Other 16-bit color modes are rejected before invoking the pinned decoder;
convert those images to 8-bit PNG or 16-bit RGBA first. Existing PNG modes with
at most 8-bit channels retain their decoder support. The C terminal also checks
an input copy capped at 8 MiB and eight bytes per pixel for 16-bit RGBA before
decoding, then converts accepted pixels to its 8-bit cell colors.

```python
from pysual import App, Button, Label, terminal

class Demo(App):
    def build(self):
        self.layout = "stack"
        self.message = Label(text="Ready", height=32)
        self.action = Button(text="Change", height=40)

    def action_on_click(self, event):
        self.message.text = "Changed"

Demo().run_blocking(backend="window")
# Demo().run_blocking(backend=terminal(renderer="python"))
```

## Choosing the terminal renderer

`backend="terminal"` uses `TerminalHost` with automatic selection. Set
`PYSUAL_TERMINAL=auto|c|python`, configure `terminal_renderer`, pass `--terminal`
to an application using `pysual.autoconfig`, or provide an explicit host:

```python
target = terminal(renderer="auto")
app.run(backend=target)
app.wait()
print(target.renderer)        # "c" or "python", after opening
print(target.fallback_reason) # explanation if automatic fallback was needed
```

Explicit `renderer` takes precedence over application configuration, then the
environment. Build commands fix the renderer for named terminal targets so the
application uses the implementation included in its executable; an explicit
host instance still controls its own renderer. Automatic selection tries the native helper and falls back to the
Python character-cell renderer when the helper cannot initialize. It does not
switch implementations after a bad drawing command or later process failure.
`renderer="c"` reports missing or unusable native dependencies instead of falling
back; `renderer="python"` never launches the helper and needs only the standard
library. Opening a terminal normally requires interactive stdin and stdout.
Opening from the main thread also installs `SIGTERM`/`SIGHUP` handlers that
restore the console before the process ends; the C renderer restores its own
console when its helper process exits.

Both terminal renderers implement Unicode grapheme cells, wide-glyph repair,
clipping, gradients, semantic icons and markers, keyboard/mouse input, bracketed
paste, and changed-run ANSI output. One column is eight logical units and one
row sixteen; `ui_scale` must be 1. The terminal emulator owns the physical font
and DPI. `color="auto"|"truecolor"|"256"|"16"|"none"` controls color depth.
The Python renderer can decode supported PNG data using the standard library.
Cold PNG loads run one at a time on a background worker; a temporary `[image]`
placeholder is replaced when decoding finishes, without waiting for another
input event. Decoded pixels retain the 32 MiB cache budget. When another image
would exceed the cache's byte or entry budget, that opening falls back to the
original synchronous LRU loading so larger working sets keep rendering without
repeated placeholder reloads. Decoding still uses Python CPU time; the worker
improves input responsiveness rather than decoding throughput. Closing a host
discards pending results without waiting for an already-running decode.

Clipboard text is limited to 8 MiB of UTF-8. Native clipboard operations also
need to fit the 16 MiB JSON transport frame after escaping. Exceeding either
limit fails the clipboard action and preserves the window and cut selection.
A failed native transport remains a session error.

The C terminal retains changed control segments and composes one cell frame per
scene update. Unchanged frames reuse cells and emit no ANSI diff. Changed scenes
still replay all terminal commands once; terminal damage-region repaint and
native sprite/property animation are not implemented. Native terminal capability
metadata excludes clipboard and text composition; ordinary paste is supported.
Python clipboard availability depends on the terminal and OS services.

## Native builds

Run `python tools/native.py` for the full window/terminal helper. On Windows
the build tool fetches and verifies pinned development archives and supports
MSVC or MinGW-w64; see the root README for platform prerequisites. The output is
`src/pysual/bin/pysual-host.exe` on Windows or `pysual-host` elsewhere.
`PYSUAL_HOST` overrides that path. No CPython headers or ABI binding are needed.

For a native terminal without SDL libraries:

```sh
python tools/native.py --terminal-only --output .build/terminal-only
```

The terminal-only helper uses OS timing and socket APIs plus vendored cJSON.
Its `images` capability is false because it omits SDL_image; use the full helper
or the Python renderer for image decoding. The build records supported targets
in `native-build.json`. A terminal-only helper cannot open a window.

## Scene ownership and diagnostics

The first frame records drawing segments. Subsequent Python changes submit
validated patches containing only changed command bodies plus necessary scene
order metadata. SDL repaints intersecting segments in damaged regions and caches
a settled scene in a bounded texture. Ordinary unchanged frames need no Python
paint callback or frame IPC. Custom paint/layout hooks execute in Python when
the model changes; explicit per-frame application coroutines remain Python work.

The parent and child communicate over authenticated loopback TCP using framed
JSON, a per-session random token, bounded queues and atomic command validation.
Acknowledgements follow commit. Presentation is asynchronous: the host draws
and presents after the acknowledgement, coalescing a newer patch that arrives
first. `present` and `capture` still wait until that frame has been drawn. A
transport timeout closes the session because the caller cannot infer whether an
operation committed.

`NativeHost.native_stats()` distinguishes native presentations, scene updates,
command work, cache hits and resource usage. Terminal statistics also separate
cell compositions, encoded ANSI bytes, completed writes and pending output.
Terminal loop rates do not measure terminal-emulator refresh. Window frame counts
measure completed presentation calls, not physical monitor refreshes.

`NativeHost.capture(path)` reads an explicit offscreen render target, avoiding
driver-dependent hidden-window backbuffer contents. `terminal(hidden=True)`
uses deterministic cells without touching the caller's console; `snapshot()`
returns rows and complete cell metadata for either implementation.
The C renderer preflights snapshots against its 16 MiB reply budget, including
UTF-8 text in both the cells and rows. A snapshot too large for one reply raises
`NativeHostError` without changing the scene or closing the host; reduce the
headless viewport before requesting another snapshot. Rendering may support a
larger grid than can be returned in one snapshot.

## Verification boundary

The native tests exercise actual helper processes: retained/full-frame pixel
parity, all 18 themes, clipping/effects, animation epochs, bounded resources,
atomic invalid commands, terminal Unicode/input, automatic fallback and public
application lifecycle. A Windows opt-in test uses its own hidden console and
checks real console output, keyboard input and exact mode restoration:

```powershell
$env:PYSUAL_TEST_TERMINAL_IO = "1"
python -m pytest tests/test_native_protocol.py tests/test_native_window.py tests/test_native_terminal.py tests/test_native_scene_parity.py tests/test_native_runtime.py tests/test_terminal_selection.py
```

Native builds and tests are also configured for Linux and macOS. Their CI results
are the qualification evidence; Windows execution does not establish behavior
on other platforms. OS accessibility/IME qualification and OS-level modal
ownership between separate native window processes remain open work.
