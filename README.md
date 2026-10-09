# Pysual

Python interfaces for **Window**, **Terminal**, and **Web**.

Assign controls, change their properties, and name event handlers. The same
application runs in a native window, a terminal, or an SVG browser interface.
Python 3.11+. MIT licensed. Early development; APIs may change.

See the [0.1.1 release notes](CHANGELOG.md) for fixes and supported scope.

## Start in a browser

```sh
git clone https://github.com/PabloMP2P/Pysual.git
cd Pysual
python -m venv .venv
# Activate .venv using your shell, then:
python -m pip install -e '.[dev,build]'
python examples/hello.py --backend web
```

Activate with `.venv\Scripts\Activate.ps1` in PowerShell or
`source .venv/bin/activate` on macOS/Linux. The live Web host and Python terminal
renderer need no graphics packages or C compiler. For library-only installation,
use `python -m pip install .`.

```sh
python examples/hello.py --backend terminal --terminal python
python -m pysual native
python examples/hello.py --backend window
```

The native builder finds a C compiler and provisions checksum-pinned SDL
development packages on Windows. Linux/macOS require a C11 compiler,
`pkg-config`, SDL3, SDL3_ttf, and SDL3_image development libraries.
See [building](https://github.com/PabloMP2P/Pysual/blob/main/docs/building.md) for compiler and platform details.

## Write an application

```python
from pysual import App, Button, Label

class Hello(App):
    def build(self):
        self.title = "Hello"
        self.layout, self.padding, self.spacing = "stack", 24, 12
        self.message = Label(text="Ready")
        self.greet = Button(text="Say hello")

    def greet_on_click(self, event):
        self.message.text = "Hello, Pysual!"

Hello().run_blocking(backend="web")
```

Assignment adopts and names each control; `greet_on_click` binds automatically.
Names must not collide with existing members: the factory names `label`,
`button`, `image`, `slider`, `menu` and the other snake-case control names are
taken, so prefer descriptive names such as `message` or `save_button`
(a `BindingError` names the conflict). Handlers can be synchronous or `async def`. Properties and public methods route
to the UI owner automatically. `run()` returns after the first frame for REPLs
and scripts doing other work; `wait()` waits for closure. `run_blocking()` combines
the two. Import `pysual.autoconfig` before creating controls to enable
`--backend`, `--terminal`, `--pysual-theme`, and `--pysual-scale` in your own script.

## Three backends

| Backend | Runtime | Distribution |
| --- | --- | --- |
| `window` | Standalone C renderer with SDL3, text, images, effects, retained scenes, and native animations | Executable for the build machine's OS and architecture |
| `terminal` | C character renderer; `auto`, `c`, or `python` renderer selection | Executable, including a Python-only option requiring no C build |
| `web` | SVG with local Python when served; Python in Pyodide when bundled | One HTML file embedding the application, fonts, and runtime |

Terminal `auto` prefers an available C host and falls back to Python when it
cannot start. Choose `--terminal c` to require C, or `--terminal python` for a
dependency-free renderer. Terminal typography follows the emulator's cell grid.

```sh
python -m pysual examples/hello.py --backend window -o dist/hello.exe
python -m pysual examples/hello.py --backend terminal --terminal python -o dist/hello-terminal.exe
python -m pysual examples/hello.py --backend web -o dist/hello.html
```

Use extensionless executable names on Linux/macOS. Build on each target OS;
these commands do not cross-compile. Outside a checkout the same commands are
`pysual build …` (or `python -m pysual build …`) after `pip install`. Web builds fetch a checksum-pinned Pyodide
runtime once; `--runtime PATH` makes subsequent builds fully local. Serve the
bundled HTML from a static HTTP host. Application handlers remain Python and
need no application Python server. See [Web delivery](https://github.com/PabloMP2P/Pysual/blob/main/docs/web.md).

## Included

- 43 catalog controls: editable grids, virtual lists/trees, tabs, menus, dialogs,
  text and value editors, charts, range selection, navigation, choice cards,
  disclosure groups, badges, actionable alerts, meters, and progress.
- Five layout modes, nine theme families in light and dark (18 themes), 44 icons, bundled fonts, shadows, gradients,
  surfaces, interaction motion, and custom painting.
- Unicode text editing, selection, clipboard where supported, undo/redo,
  async events, drag/drop, and batched property updates.
- Retained rendering, bounded caches, on-demand frames, and incremental native
  scene updates.

| Example | What it demonstrates |
| --- | --- |
| [hello.py](https://github.com/PabloMP2P/Pysual/blob/main/examples/hello.py) | Automatic binding, text input, async events |
| [dashboard.py](https://github.com/PabloMP2P/Pysual/blob/main/examples/dashboard.py) | Editable data, filtering, charts |
| [showcase.py](https://github.com/PabloMP2P/Pysual/blob/main/examples/showcase.py) | Controls, themes, layout, effects |
| [theme_gallery.py](https://github.com/PabloMP2P/Pysual/blob/main/examples/theme_gallery.py) | An interactive control studio with all nine theme families, light/dark switching, and desktop-inspired materials |
| [stress_test.py](https://github.com/PabloMP2P/Pysual/blob/main/examples/stress_test.py) | Configurable control mixes, workloads, timing, and JSON reports |
| [custom_control.py](https://github.com/PabloMP2P/Pysual/blob/main/examples/custom_control.py) | Observable properties and portable drawing |
| [composite_control.py](https://github.com/PabloMP2P/Pysual/blob/main/examples/composite_control.py) | Reusable compound controls with independent child state and handlers |
| [catalog_controls.py](https://github.com/PabloMP2P/Pysual/blob/main/examples/catalog_controls.py) | Thirteen catalog ideas in a responsive review workspace with collapsible filters, review plans, status feedback, and live theme switching |

Choose Modern, Windows, macOS, Terminal, Win31, WinXP, Macintosh, Analog arcade,
or Neon. Each has a light and dark version with its own control surfaces,
typography, borders, and interaction states. Preview them with:

```sh
python examples/theme_gallery.py --backend web
```

See the [rendered comparison of all 18 appearances](https://github.com/PabloMP2P/Pysual/blob/main/docs/theme-families.png).

Use `get_theme("winxp")` for light and `get_theme("winxp_dark")` for dark.
See [themes and custom painting](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md#themes-and-custom-painting) for the
complete catalog and compatibility details.

### Stress test

The stress lab mixes 20 control types, including grids, trees, charts, images,
and editors. Choose balanced, text, forms, visual, or collections scenes, with
presets from 100 to 5,000 controls or an exact count up to 20,000. It opens on
the large preset with continuous redraw and VSync off.

```sh
python examples/stress_test.py --backend window --controls 2000 --mix balanced --workload local-update
python examples/stress_test.py --backend terminal --terminal python --controls 100 --mix forms
python examples/stress_test.py --backend web --mix visual
python examples/stress_test.py --controls 500 --workload property --fraction 0.25 --rate 60 --warmup 2 --duration 10 --report work/stress.json
```

Changing a preset or another dashboard control applies it immediately. Pause,
restart measurements, or export a JSON report from the same bar. `--help` lists
the control mixes and rendering options. Timed runs
measure `--duration` seconds after warmup. Use the same seed, viewport, theme,
workload, and rendering settings when comparing runs.

Workloads cover idle scenes, paint invalidation, layout changes, property
updates, and one-control updates. Rendering options include the presentation
cap, redraw interval, VSync, body reuse, and surface-cache budget.

Python submission rates, native presentation loops, and terminal output counters
are reported separately. These do not measure physical screen refresh or browser
paint. Live metrics refresh at 4 Hz by default and add work to a static scene;
pass `--no-telemetry` to leave them on demand. Collection rows and chart points
are counted separately from widgets.

## Develop

Style and commit conventions are in [Contributing](https://github.com/PabloMP2P/Pysual/blob/main/CONTRIBUTING.md).

```sh
python -m pytest
```

After changing control properties, run `python tools/generate_factory_types.py`
to refresh the factory typing signatures. Add `--check` to verify them without
writing changes; the generator needs only the Python standard library.

For new controls, see [control composition and catalog development](https://github.com/PabloMP2P/Pysual/blob/main/docs/control-architecture.md).

The full test suite requires Node.js 24 for the Web JavaScript contracts.
Native tests require a built host and report explicit skips otherwise. CI builds
and checks the native implementation separately. Applications need no Node.js
or npm project.

[API guide](https://github.com/PabloMP2P/Pysual/blob/main/docs/api.md) · [Native rendering](https://github.com/PabloMP2P/Pysual/blob/main/docs/native.md) ·
[Building](https://github.com/PabloMP2P/Pysual/blob/main/docs/building.md) · [Web](https://github.com/PabloMP2P/Pysual/blob/main/docs/web.md) ·
[Contributing](https://github.com/PabloMP2P/Pysual/blob/main/CONTRIBUTING.md)

Controls are custom drawn. OS accessibility bridges, complex-script shaping,
full font fallback, mobile hosts, and OS-level modality between separate native
processes are outside this release's supported scope. Real display, terminal,
clipboard, IME, and packaged-app behavior should be checked on the target system.
