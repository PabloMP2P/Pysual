# SVG web applications

`web` is one backend with two delivery modes. Both use the same Python controls,
layout, events and SVG scene renderer. The browser reconciles changed SVG nodes,
retains unchanged nodes and gradient resources, and forwards input to Python.
It does not use a Canvas renderer or translate event handlers to JavaScript.

## Run from Python

```sh
python examples/hello.py --backend web
```

The application runs in native Python. A standard-library HTTP host binds to
`127.0.0.1` on an available port and opens a browser tab. The printed URL includes
a per-opening capability token. Input and service requests require this token
and matching origin; the listener cannot be bound to a public network address.
Keep the Python process running while using the application. Closing the tab
requests application closure after a brief reconnect grace period.

Applications can use `Window.run(backend="web")` directly. `WebHost` from
`pysual.backends.web` also accepts `open_browser=False` and an optional `port`
for embedding. Its `url` is available after opening. `export_svg(path)` and
`export_html(path)` save portable pictures of the current frame; these snapshots
contain fonts and images but do not contain a running Python application.

## Build a complete HTML application

```sh
python -m pysual examples/hello.py --backend web --output dist/hello.html
```

The builder obtains the checksum-pinned Pyodide 0.29.0 npm runtime once and
caches it. To avoid build-time network access, provide the extracted npm runtime:

```sh
python -m pysual examples/hello.py --backend web --runtime path/to/pyodide --output dist/hello.html
```

The result is one HTML file containing the application, Pysual, both bundled
fonts, SVG JavaScript modules, the Python interpreter, standard library and
license notices. It needs no CDN or Python application server. Python callbacks
execute in Pyodide; application source is preserved. The browser must support
WebAssembly JavaScript Promise Integration (JSPI) for blocking lifecycle calls.
Serve the HTML through an ordinary static HTTP server for predictable browser
permissions. The runtime makes even a small app substantially larger than its
Python source.

Use repeated `--include` options for files and local pure-Python modules below
the entry script's directory. Included files preserve their relative paths in
the browser's virtual filesystem. Paths outside that directory, symlinks,
Windows junctions/reparse points (including nested ones), and reserved package
names are rejected. Keep the input tree unchanged while building. Files are
published atomically only after
the entire bundle is prepared. Native binary Python extensions need their own
WebAssembly distributions and cannot be copied into a browser application.

The Python builder API is `pysual.bundle.build_web(script, output, runtime=None,
includes=())`. Supplying `runtime` prevents runtime download. It returns the
generated HTML path.

## Input, services and limits

The shared surface supports pointer capture, keyboard focus, wheel scrolling,
text input and composition, viewport changes, device scaling, safe areas and
on-screen keyboard occlusion. It draws text, icons, images, clipped groups,
gradients and theme effects as SVG. Cached control bodies remain vector groups.
The browser owns viewport size and zoom; use automatic scale or `ui_scale=1`.
Local PNG, JPEG, GIF, WebP and SVG assets are embedded in live web scenes.
SVG stays an image resource rather than page markup and is limited to 32 MiB.

Clipboard, file selection/download and URL opening are explicit browser
operations and can be denied or cancelled. File and session text is limited to
8 MiB. Standalone recovery uses origin-scoped IndexedDB with per-application
namespaces; live delivery uses the native session store. File saving and draft
recovery remain distinct operations. Keep application work cooperative so it
does not block Python input handlers.

Native sprite and property-animation clocks are window-host capabilities.
Applications should check capabilities before relying on them. SVG output is
not an operating-system accessibility bridge, and complex-script/IME behavior
requires qualification on the intended browser and device.

## Verification

The web tests cover real loopback authentication and input transport, SVG
clipping and resources, bounded image handling, browser-service cancellation,
unchanged-node reconciliation, vector caches and atomic HTML packaging. Node
tests use controlled DOM objects. They complement real-browser checks and do
not establish browser rendering, permission or device compatibility by themselves.

Install the development dependencies and Chromium, then run
`python tools/check_web.py --mode all` for real standalone and live-browser
coverage. Standalone mode verifies offline assets and Python callbacks; live
mode exercises Hello and Dashboard through the Python host, including Unicode
entry, clipboard, undo/redo, filtering and edits. `--mode live` avoids downloading
Pyodide. Results and screenshots are saved in `.build/web-smoke` and
`.build/web-live`, including failure diagnostics for CI.
