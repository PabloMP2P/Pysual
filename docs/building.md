# Run and distribute applications

Use Python 3.11 or newer in a virtual environment. From the repository:

```sh
python -m venv .venv
# Activate .venv using your shell, then:
python -m pip install -e '.[dev,build]'
python -m pysual run examples/hello.py --backend terminal --terminal python
```

`pip install .` installs the library without compiling C or downloading SDL.
The Python terminal renderer is available immediately. The `dev` extra installs
pytest, Pillow and Playwright for verification; the `build` extra installs PyInstaller and
the Python distribution builder. Application imports use `pysual`.

Installing also provides the `pysual` command (`python -m pysual` is identical):
`pysual build app.py --backend web|window|terminal` and `pysual run app.py …`
work from any installation; `pysual native` and `pysual package` need the
repository checkout because they compile `native/*.c` and package the sources.

## Choose a target

| Target | Development command | Distributed output | Recipient needs |
| --- | --- | --- | --- |
| Window | `python -m pysual run examples/hello.py --backend window` | Native executable containing Python and the C host | The target desktop OS and architecture |
| Terminal | `python -m pysual run examples/hello.py --backend terminal` | Console executable containing Python | An interactive VT-compatible terminal |
| Web | `python -m pysual run examples/hello.py --backend web` | One self-contained HTML file | A supported browser with WebAssembly |

Build the native host before running a window application. A direct Python entry
script can choose its host in `run()` or `run_blocking()`. `pysual run` selects
the requested target through a backend override, including when the script names
another backend. The same selection is embedded into distributed executables.

Pass application arguments after `--`; runner options go before it:

```sh
python -m pysual run examples/stress_test.py --backend terminal --terminal python -- --controls 100 --duration 1
```

## Native renderer

```sh
python -m pysual native
python -m pysual run examples/hello.py --backend window
```

On Windows x64, the helper discovers Visual Studio C++ Build Tools from an
ordinary shell. If that toolset is unavailable, it looks for MinGW-w64 GCC on
PATH and in common MSYS2 UCRT64/MINGW64 directories. Select a compiler explicitly
with `--compiler cl`, `--compiler gcc`, or an absolute executable path.

The helper downloads official SDL3 3.4.16, SDL3_ttf 3.2.2 and SDL3_image 3.4.6
development archives into `.build/deps`, verifies their pinned SHA256 hashes,
and copies the runtime DLLs and license notices beside `pysual-host.exe`.
Reusing the verified archives needs no download. Existing packages can be used
with all three options: `--sdl-root`, `--ttf-root`, and `--image-root`.
Explicit Windows SDK roots must contain the pinned official SDL3_ttf DLL so
its bundled dependency notices remain accurate. A custom SDL_ttf build requires
updating `native/notices` for its dependencies.

Linux and macOS need a C11 compiler, `pkg-config`, and SDL3, SDL3_ttf and
SDL3_image development packages. Their package managers may use different
package names; the helper accepts both common SDL pkg-config naming schemes.

On macOS with Homebrew:

```sh
xcode-select --install
brew install pkg-config sdl3 sdl3_ttf sdl3_image
python -m pysual native
```

On Ubuntu 25.10 or newer:

```sh
sudo apt install build-essential pkg-config libsdl3-dev libsdl3-ttf-dev libsdl3-image-dev
python -m pysual native
```

Ubuntu 24.04 requires building SDL3 from source. Package availability is recorded
in the official [Homebrew SDL3_ttf](https://formulae.brew.sh/formula/sdl3_ttf),
[Homebrew SDL3_image](https://formulae.brew.sh/formula/sdl3_image), and
[Ubuntu SDL3_ttf](https://packages.ubuntu.com/questing/libsdl3-ttf-dev) listings.

For libraries installed under a private prefix, set `PKG_CONFIG_PATH` to its
`lib/pkgconfig` directory and make its shared libraries available to the loader.
The CI workflow shows a complete build from checksum-pinned release sources.

Native files are installed into `src/pysual/bin`. `--output DIRECTORY` chooses a
different directory; `PYSUAL_HOST` can point at that host for diagnostics. A
capability marker records whether the build includes the window renderer.
Compiler intermediates and dependency caches stay under `.build`.

## Terminal renderers

The terminal target accepts `--terminal auto`, `--terminal c`, or
`--terminal python`. Direct application runs can use the `PYSUAL_TERMINAL`
environment variable or the `TerminalHost(renderer=...)` constructor.

| Choice | Behavior |
| --- | --- |
| `auto` (default) | Uses the C host when available and the Python renderer otherwise; compiling a terminal app does not require C. |
| `python` | Uses the Python renderer and excludes the native host from the executable. |
| `c` | Requires the C renderer; executable builds compile a terminal-only host if needed. |

An independent C terminal host can be built without SDL:

```sh
python -m pysual native --terminal-only
```

That build supports terminal text and vector-to-cell drawing; image decoding
requires the full SDL-enabled host. Building the full host again restores the
window renderer. The window executable builder detects a terminal-only build
and automatically replaces it with a full one.

## Executables

```sh
python -m pysual examples/hello.py --backend window -o dist/hello-window.exe
python -m pysual examples/hello.py --backend terminal --terminal python -o dist/hello-terminal.exe
```

Use extensionless output names on Linux/macOS. PyInstaller builds for the OS and
architecture of the build interpreter; it does not cross-compile. Build each
platform's executable on that platform. Windows and macOS window builds suppress
the console; terminal builds retain it.

Window builds compile a missing native renderer. Native executables and shared
libraries are registered as PyInstaller binaries, allowing dependency discovery
for DLLs, `.so` and `.dylib` files. Fonts and notices are packaged as data. Resolve
missing-library warnings and verify a launch on a clean target machine before
distribution. Signing, notarization and OS installer creation are separate
release steps.

Frozen applications include the full CPython license notice supplied by the build
interpreter, alongside Pysual and asset notices. The builder looks in the base
interpreter installation (also when building from a virtual environment). If
that distribution keeps its notice elsewhere, pass `--python-license PATH` to
`pysual build`, or `python_license=PATH` to `build_executable`. Supply the complete
notice for the interpreter being bundled; the browser runtime's Python notice
is for a different distribution. A missing or unrelated notice stops the build
before compiling or replacing any output.

The builder works in an isolated staging directory. It replaces the requested
output only after PyInstaller has produced a complete file; a failed build
preserves an existing output.

## Self-contained HTML

```sh
python -m pysual examples/hello.py --backend web -o dist/hello.html
python -m http.server --directory dist 8000
```

The HTML contains the application, the Pysual package, fonts, browser host,
Python standard library and pinned Pyodide 0.29.0 runtime. Python callbacks run
inside the browser. No application Python server or runtime CDN is needed after
the HTML has been delivered. The development web host uses local Python; its
file-system and networking capabilities differ from browser Python.

The first web build downloads a checksum-verified runtime into the user's
application cache. To build without network access, provide the extracted npm
runtime explicitly:

```sh
python -m pysual examples/hello.py --backend web --runtime path/to/pyodide
```

Serve the HTML over HTTP. Bundled apps require browser support for WebAssembly
JSPI (`WebAssembly.Suspending` and `WebAssembly.promising`) so synchronous Python
waits can cooperate with browser events; current supporting Chromium browsers
are the intended target. The bootstrap reports an unsupported browser explicitly.
Browser interaction limits are described in the Web guide. The full Python/WebAssembly payload dominates
the file size. Native Python extensions cannot be copied into Pyodide directly.

## Local files and packages

```sh
python -m pysual app/main.py --backend web --include helpers.py --include assets
python -m pysual app/main.py --backend window --include assets --package package_name
```

`--include` paths are relative to the entry script and preserve their paths.
Both builders reject symlinks and Windows junctions/reparse points, including
redirects inside included directories. Keep source assets unchanged during a build.
Resolve assets relative to `__file__`; keep writable user documents outside the
packaged application. Executables analyze regular imports; `--package` collects
a dynamically imported installed package. Web builds accept local pure-Python
modules through `--include`.

## Library packages and validation

```sh
python -m pytest
python -m pysual package
```

A source distribution includes the Python and C sources, tools, examples, tests,
documentation and licenses, excluding generated native files. A wheel built
without `pysual-host` is pure Python and supports the Python terminal and web
hosts. Build the native host before building a wheel to include it; that wheel
gets an OS/architecture tag rather than a CPython ABI tag. Wheel construction
does not invoke a compiler or download dependencies.
On macOS, the tag comes from the native host's actual Mach-O architectures and
minimum OS load commands. A universal Python installation alone does not make
the native wheel universal; unreadable native metadata stops the package build.
The native compiler defaults to macOS 11.0 for arm64 and 10.15 for x86_64.
`MACOSX_DEPLOYMENT_TARGET` overrides that default. For macOS 11 and newer it must
name a whole-major target, such as `14.0`; wheel compatibility tags cannot express
minor or patch deployment requirements on those systems. Linked SDL libraries
may have a newer requirement and remain external dependencies of library wheels.

`python -m pysual package --wheel` or `--sdist` selects one distribution type.
The command invokes the standard Python packaging frontend. You can also
run `python -m build --wheel` directly from the checkout to include an already
built native host. To build both artifacts from the checkout, use
`python -m pysual package` or `python -m build --wheel --sdist`.
A bare `python -m build` builds its wheel from the source distribution, which
excludes generated native files, so that wheel is pure Python even if the
checkout contains a built host.

Unlike executable freezing, wheel building does not discover additional system
libraries. Linux/macOS wheels linked to system SDL require those libraries on
the recipient's machine unless separately bundled and repaired for distribution.
The automated workflow tests the pure library, then real native rendering and
the independent terminal build across Windows, Linux and macOS. A configured
workflow is not a substitute for inspecting the results of the release run.

After building, verify the matching wheel in a fresh environment outside the
checkout, and build and launch the three executable probes:

```sh
python tools/check_wheel.py --kind pure
# After building a native wheel:
python tools/check_wheel.py --kind native
python tools/check_frozen.py
```

Use `--wheel FILE` to select a wheel when `dist` contains multiple versions.
Successful wheel checks write `.build/wheel-probes/<kind>/result.json` with
the tested wheel's SHA256, interpreter/platform details, and CI commit when
available. A matching source archive is checksummed too; the wheel is the
artifact exercised by the installation probe. CI retains the pure wheel and
source archive once, and each tested native wheel with its compact probe
results, for 14 days. Artifact names include commit, platform, and Python
version. Pure artifacts are uploaded before the native build, so the exact
validated files remain available even if a later stage fails. These artifacts
are build outputs, not automatically published releases.

For the standalone browser check, install Chromium once and run:

```sh
python -m playwright install chromium
python tools/check_web.py
python tools/check_web.py --mode live
```

The default browser check verifies included PNG/SVG pixels, Unicode input and an
async Python callback while blocking external requests. `--runtime DIR` reuses a
local Pyodide distribution. Live mode checks Hello and Dashboard through the
Python web host, including clipboard, undo/redo, filtering and committed edits.
Use `--mode all` to run both; live mode alone needs no Pyodide runtime. CI runs
both modes in Chromium and caches the pinned runtime. Browser results and
screenshots are saved under `.build/web-smoke` and `.build/web-live`; wheel and
executable diagnostics are also saved under `.build`.

With the development dependencies installed, `python tools/check_typing.py`
checks valid consumer code and marked invalid expressions using Pyright against
this checkout. CI runs it alongside the ordinary test suite. Native text,
resource and diagnostics tests run again after building the native host;
Windows CI also opts into the existing isolated console-restoration test.

## Visual regression checks

The small visual fixture covers menus, enabled/disabled buttons, selected rows,
text entry, a selected card and a fixed-date calendar in modern, modern-dark
and macOS themes. Native and Chromium images have separate baselines; both
use an 800 by 560 logical viewport at 1.5 device scale, including fractional
menu metrics. It requires the development dependencies and the relevant host:

```sh
python tools/check_visual.py --mode capture
python tools/check_visual.py --mode check
# Only after inspecting the captures and intended differences:
python tools/check_visual.py --mode update
```

Use `--backend native` or `--backend web` to select one renderer. Captures,
environment details and results go to `.build/visual`; a failed comparison
adds the expected image and an amplified difference image. Every pixel must
be within 8 channel levels of its reference (`--tolerance` adjusts this).
The check refuses missing baselines or changed OS-family, renderer/browser,
font, scale or viewport metadata. Baselines are platform-specific under
`tests/visual_baselines`; the initial reviewed set is Windows/Direct3D11 and
Chromium. Metadata does not identify GPU drivers or OS releases, so inspect
differences after those changes too. A fresh baseline is never accepted
automatically. `--baselines DIR` supports a separately reviewed environment.

CI captures the scene and retains review artifacts on each native platform
and in Chromium. Pixel gating remains an explicit check in a matching,
reviewed environment instead of making unqualified cross-platform images
equivalent. Keep this small matrix alongside the behavioral and incremental
render-parity tests; do not replace those tests with screenshots.

## Format native terminal code

With clang-format 19 or newer installed (`python -m pip install clang-format`
provides one), use the repository's `.clang-format` configuration for the three
hand-written files:

```sh
clang-format -i native/host.c native/sdl_renderer.c native/terminal_renderer.c
```

CI checks the same files with `clang-format --dry-run --Werror`. Leave the
generated Unicode tables and vendor sources unchanged. Rebuild both full and
terminal-only hosts and run the native tests after changes to this implementation.

## Regenerate Unicode tables

The generators run offline on any supported Python version:

```sh
python tools/generate_graphemes.py --check
python tools/generate_terminal_unicode.py --check
```

Omit `--check` to regenerate the checked-in files; `--output PATH` writes a
separate candidate for review. Check mode reports missing or stale output without
modifying it. Run `python -m pytest tests/test_unicode_generators.py tests/test_text.py`
and the native terminal tests after intentional data changes.

Complete official UCD inputs are compressed under `tools/unicode_data`, included
in source distributions, and verified against pinned SHA256 hashes before use.
They add no installed-library dependency or wheel data. Grapheme properties stay
at Unicode 17.0.0; native terminal category, width and case properties stay at the
existing 15.1.0 baseline. The build interpreter's Unicode version does not affect
the output. Source provenance and deliberate update instructions are in
`tools/unicode_data/README.md`.

## Developer benchmarks

`python tools/benchmark_core.py` measures property updates, layout, cached
painting, and native text/transport when a helper is available. It emits
JSON and opens only hidden native windows. `python tools/benchmark_png.py`
measures cold PNG decoding and verifies exact pixels. Compare runs with
the same fixtures and environment; these timings are not display FPS.
