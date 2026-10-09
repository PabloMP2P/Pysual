# Changelog

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
