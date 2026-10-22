# Changelog

## 0.1.4 — 2026-10-22

`App.choose_path()` opens a system dialog and returns the selected absolute
filesystem path. Cancellation returns `None`. The call selects a path and does
not read, create, or write the file or folder. `directory=True` selects a
folder, and together with `save=True` selects a folder to save into.
`extensions` limits a file dialog to those suffixes. `all_files=True` permits
any file type. The owning window suspends input while the dialog is open.

Desktop windows and the live web host use the operating-system dialog on the
machine running Python, so a live web path belongs to that machine. A terminal
uses a system dialog when one is available. The Python terminal renderer asks
for the path in the terminal when no system dialog is available. The native
terminal renderer requires a desktop dialog provider. Missing or failed dialog
services raise `CapabilityError`. A packaged browser app does not advertise
`path_dialogs`. `open_text_file()` and `save_text_file()` still move UTF-8 text
on every backend.

Number columns in a DataGrid are right-aligned unless `align` is set. Without
`format_spec`, every value in the column uses the same number of decimal
places: the most fractional digits any value needs, including trailing zeros.
Blank cells stay empty. An all-integer column shows no decimal point. An
explicit `format_spec` is used as given. A column with a value that needs more
than 16 fractional digits keeps each value's ordinary number text.

## 0.1.3 — 2026-10-21

DataGrid accepts up to 2,000,000 rows, 640 columns, and 200,000,000 cells, and
remains an in-memory control. Layout, painting, text measurement, and hit
testing reuse work whose inputs are unchanged. A host can separate font and
metric changes from image-resource changes. Native caption measurements are
batched, and unchanged native and SVG output is reused.

`Painter.caption` draws vertically centered text with an optional leading icon.
Set `measure_resources=False` when a custom control's size depends on metrics
and its declared inputs, and the host exposes `metrics_revision`.

### Fixes

- Draw a text selection only while the editor is focused.
- Remeasure chart axes and truncated text when `Painter.measure` is overridden.
- Keep scoped icon stroke overrides on deferred native captions.

## 0.1.2 — 2026-10-16

Published releases install with `pip install pysual`. Windows x64, Linux x64,
and Apple Silicon wheels include the native window host and its SDL libraries.
Other platforms receive the pure-Python wheel, with the Python terminal and web
backends. A `v*` tag that matches `__version__` builds those wheels, checks
them outside the build SDK, and publishes the GitHub Release and the PyPI files.

Linux wheels require glibc 2.28 or newer. X11, Wayland, OpenGL, and GLib stay
on the recipient's machine.

## 0.1.1 — 2026-10-09

First public release of Pysual, a Python 3.11+ UI library for native windows,
terminals, and the web. It includes 43 controls, 18 themes, typed factories,
Unicode text editing, retained rendering, and executable/HTML build tools.

### Fixes

- Keep the C terminal session usable when a paste exceeds the encoded event
  budget, and encode common control characters compactly.
- Bound caption measurement work so long Unicode labels do not exceed native
  batch or transport limits.
- Preserve text-selection autoscroll when rendering is delayed or the frame
  rate is low.
- Validate default numeric grid formatting before accepting rows and edits.
- Isolate chart axis formatting from an application's Decimal precision and
  exception traps.
- Reject unsupported native animation metadata before retaining or comparing
  commands.
- Clarify when installed packages need a prebuilt native host or a source
  checkout for executable builds.

### Supported scope

The API remains in early development. Native builds target Windows, Linux, and
macOS; the Python terminal and live web hosts also work without a native helper.
Standalone web applications require a browser with WebAssembly JSPI support.
Linux/macOS native library wheels require their documented SDL shared libraries
unless separately bundled and repaired. Build frozen applications on their
target OS and architecture.

Controls are custom drawn. OS accessibility bridges, complex-script shaping,
full font fallback, mobile hosts, and OS-level modality across native processes
are not provided in this release. See [building](docs/building.md),
[native rendering](docs/native.md), and [web delivery](docs/web.md) for platform
requirements and validation commands.
