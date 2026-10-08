# Python API

## Construction and events

Subclass `App` and create controls in `build()`. The first assignment of a
detached control to a public attribute adopts it and establishes its name:

```python
from pysual import App, Button, Container, Label

class Demo(App):
    def build(self):
        self.layout = "stack"
        self.heading = Label(text="Tasks")
        self.actions = Container(layout="stack", direction="horizontal")
        self.save = Button(parent=self.actions, text="Save")

    def save_on_click(self, event):
        self.heading.text = "Saved"
```

`parent=` makes nesting explicit. Factory methods such as
`self.actions.button(text="Save")` create and attach controls in one call.
Variables, lists, and private attributes do not infer parents. Each control
has one parent; invalid ownership and duplicate names fail before attachment.

Handlers follow `<name>_on_<event>`: `save_on_click`, `entry_on_changed`,
`grid_on_edited`. App lifecycle handlers use the class name, for example
`Demo_on_loaded`. Use `control.click.connect(callback)` for explicit binding.
Typed event objects expose the source and event-specific values. Async handlers
can await I/O without blocking the interface; CPU-intensive work belongs in an
explicit worker. A blocking callback still blocks Python input processing.

## Lifetime and threads

`App` and `Window` are names for the same class.

```python
ui = Demo()
ui.run(backend="window")       # returns after first presentation
ui.heading.text = "Ready"      # public operations route to the UI owner
ui.wait()                      # wait for this opening to close
```

`run_blocking()` opens and waits. `run()` is useful in a Python REPL. `wait_async()`
allows an async caller to wait. `ui.wait()` and `await ui.wait_async()` return the
`Outcome` of that particular opening, with its `result` and closure `reason`.
The module functions `pysual.wait()` and `await pysual.wait_async()` instead return
`None` at the first point with no open windows, including windows added while
waiting. A failed opening is reported to the waiters already waiting for that
idle point, or to the next module wait if none were waiting. Once observed,
that failure is cleared for later module waits; per-window outcomes are retained.
Module waits return immediately when already idle; they do not wait for future
openings after that point. Opening waits for the first content frame or an
actual startup error; slow construction does not expire under the ordinary UI
dispatch timeout. `close()` requests closure; `destroy()` disposes
the control tree. Closing handlers can veto a normal close. Property writes and
methods route to the managed UI owner, which serves queued requests before each
frame; user event callbacks run there. Property reads from another thread return
the latest committed value immediately. Use these public interfaces from workers
and avoid mutating private fields.

`SubWindow` provides in-app dialogs on every backend. Separate native windows
use separate host processes. OS-owned modality between those processes is not
advertised by the supplied backends. `Window.show_modal(owner)` and its async
counterpart require hosts advertising `native_modal`; unsupported hosts raise
`CapabilityError` before opening the child. Custom hosts can provide this
capability. Use `SubWindow.show_modal` or `SubWindow.show_modal_async` for portable
dialogs. Blocking waits are rejected from the UI owner.

Closing keeps the control tree available for another `run()`; destroying it is
permanent. An invocation from an earlier opening cannot access a reopened window.
Finish or cancel background work belonging to an opening before starting another.

### Owned tasks and callbacks

Use `control.create_task(coroutine)` for background asynchronous work belonging
to a live control. The returned handle is awaitable and has `cancel()`.
Destroying the control cancels its work; native-window shutdown also cancels and
joins owned work. Cancellation is cooperative, so let `asyncio.CancelledError`
propagate after any necessary cleanup.
`shutdown_timeout` bounds each window's cleanup waits. Process-wide `shutdown()`
also has a bounded engine deadline and can interrupt those waits sooner. Even
when cancellation cleanup times out, hosts close and presentations settle with
the failure instead of remaining open indefinitely.

```python
import asyncio
from pysual import App, Label

class Refreshing(App):
    def build(self):
        self.message = Label(text="Loading")

    def Refreshing_on_loaded(self, event):
        self.create_task(self.refresh())

    async def refresh(self):
        await asyncio.sleep(0.05)  # Replace with asynchronous application I/O.
        self.message.text = "Ready"
```

`button.activate()` returns without waiting for its handlers to finish. It uses
the same click dispatch as input, while retaining the invocation's origin
(`program` for an ordinary direct call). Activation requires a running App:
calling it before `run()`, inside `build()`, or after closure raises
`LifecycleError` before changing checkbox/toggle/radio state or opening a URL.
Run startup actions in the app's `loaded` handler. Initializing properties and
data during construction remains silent; connecting a listener does not make
setup changes emit events. Avoid `wait()` or blocking I/O in a
callback. Use `await`, or an explicit worker for CPU-intensive work, and update
controls through their public properties and methods.

Each value change in an open window is delivered to its listeners; intermediate
values are not merged. `max_pending_handlers` limits queued and running event
handler invocations together (default 1,000). Exceeding it raises
`EventOverloadError` and fails the opening, even if the emitting callback catches
the exception. Initialize data before opening where possible, or assign the
final value once. For a known larger burst that must deliver every transition,
set a suitable limit on the app, for example `Demo(max_pending_handlers=2000)`.
Async listeners can overlap while awaiting, so the limit also includes those
unfinished invocations.

## Backends and configuration

The public names are `window`, `terminal`, and `web`:

```python
from pysual import configure, terminal

configure(backend="terminal", terminal_renderer="python")
# Or select one concrete host when opening:
Demo().run_blocking(backend=terminal(renderer="python"))
```

Call `configure()` before constructing UI objects. Import `pysual.autoconfig`
before construction to enable command-line and environment settings. Importing
`pysual` alone does not consume application arguments.

| CLI option | Environment | Values |
| --- | --- | --- |
| `--backend` | `PYSUAL_BACKEND` | `window`, `terminal`, `web` |
| `--terminal` | `PYSUAL_TERMINAL` | `auto`, `c`, `python` |
| `--pysual-theme` | `PYSUAL_THEME` | A catalog theme name |
| `--pysual-scale` | `PYSUAL_SCALE` | A finite scale of at least 0.25 |

Explicit command-line settings take precedence over configured defaults.
Executable and HTML builds fix their named backend without rewriting the app.
Terminal and Web own their cell/device scale; use automatic scale or `1` there.

## Controls

| Group | Public controls |
| --- | --- |
| Basic | `Label`, `Button`, `Image`, `Separator`, `Hyperlink` |
| Values | `CheckBox`, `Toggle`, `RadioButton`, `Slider`, `ProgressBar`, `NumericInput`, `ColorPicker` |
| Text and selection | `TextBox`, `Dropdown`, `ComboBox`, `ListView`, `TreeView` |
| Data | `DataGrid`, `LineChart`, `DonutChart` |
| Composition | `Container`, `GroupBox`, `ScrollArea`, `TabControl`, `TabPage`, `SplitPane`, `SubWindow`, `Popup` |
| Commands | `Menu`, `MenuBar` |

Controls expose validated properties. Ordinary assignments invalidate the
necessary layout or paint work. Group changes with `control.update(...)` or
`container.update_children({control: {"text": "New value"}})`; invalid batches
fail before partial property changes are applied.
Batch changes obey the same lifecycle rules as individual assignments, including
startup-only window options and visibility restrictions on active modals.

Finite-choice properties expose the same `Literal` choices in constructors and
container factories, for example `Container(layout="stack")` and
`parent.panel(layout="stack")`. Custom properties can declare
`mode: Literal["compact", "wide"] = prop(default="compact")`; import `Literal`
from `typing`. The annotation supplies runtime validation and editor choices
without a separate `choices=` list. Existing primitive annotations with
`prop(choices=...)` remain supported. Run `python tools/check_typing.py` with the
development dependencies installed to verify the public consumer fixtures.

<!-- recipe: panel-factory -->
```python
from pysual import Container

parent = Container()
direct = Container(parent=parent, layout="stack")
factory = parent.panel(layout="stack")
```

`parent.create(ControlType, ...)` accepts the concrete constructor's arguments
and returns that type, with the same ownership lifecycle as named factories.
It also works for unregistered custom controls. The parent is chosen by the
container; use a direct constructor for an explicit `parent=` argument.

For catalog discovery without adding another reserved Container member, use
`register_control(MyControl, factory=False)`. `registered_controls()` includes
these entries; `registered_controls(factories_only=True)` selects installed
legacy aliases. Existing named factories remain compatible.

TextBox supports multiline editing, selection, undo/redo, passwords, indentation,
and Unicode grapheme boundaries. Clipboard and composition availability depend
on the host. Complex shaping, bidi layout, and missing-font fallback are not
fully supported across platforms.

Collections paint visible rows. DataGrid accepts immutable `GridColumn` and
`GridRow` records, with keyed updates, sorting, editing, and column resizing.
Its limits are 20,000 rows, 64 columns, and 200,000 cells; it is an in-memory
control rather than a remote data provider. LineChart supports up to eight
series and 20,000 points total with bounded drawable geometry. Its axes adapt
label precision to the visible range and show a shared offset or exponent when
needed to keep labels compact. DonutChart
supports up to eight slices. Data preparation remains application work.

Captions in fixed rows, headings, menus, dropdowns, and chart legends display
line breaks as spaces; stored values and copied grid-cell text retain their
original contents.

Set a control's `tooltip` to explain an action, especially an icon-only button.
The hint appears after a short hover or after reaching the control with Tab or
Shift+Tab. Escape, activation, and editing dismiss it. Multiline editors keep
plain Tab for editing; Ctrl+Tab moves focus and can show the next control's hint.

### Selection controls

`Dropdown` selects one item by index; `selected_index=-1` means no selection and
`selected_item` then returns `None`. `ComboBox` remains an editable text field:
its `selected_index` reports the first matching item, or `-1` for other text.

```python
from pysual import ComboBox, Dropdown

region = Dropdown(items=("North", "South"), selected_index=0)
region.update(items=("East", "West"), selected_index=1)
assert region.selected_item == "West"

search = ComboBox(items=("North", "South"), text="Custom region")
assert search.selected_index == -1
```

On a running app, `open()` toggles either control's choices without waiting for
a selection. Empty or disabled controls do nothing; a read-only ComboBox also
does nothing. A user choice updates the Dropdown index or replaces ComboBox text
as an undoable edit. Replacing items dismisses an open choices popup; Dropdown
clears an index that no longer fits the new items.

### Grid data, edits and selection

Create immutable `GridColumn` and `GridRow` records with unique, stable keys.
Cell positions follow the column tuple; number and boolean columns validate
their cell types. Use tuples for both records and cells.

The following recipe runs without opening a window. Inside an app's `build()`,
assign the grid to `self.grid` to adopt it, as in the
[dashboard example](../examples/dashboard.py).

<!-- recipe: grid-data -->
```python
from pysual import DataGrid, GridColumn, GridEditEvent, GridRow

columns = (
    GridColumn("account", "Account", width=180),
    GridColumn("revenue", "Revenue", kind="number", editable=True),
)
grid = DataGrid(columns=columns, rows=(
    GridRow("acacia", ("Acacia", 1200)),
    GridRow("birch", ("Birch", 850)),
))
edits = []

def record_edit(event: GridEditEvent):
    edits.append(event)

grid.edited.connect(record_edit)
grid.update(selected_key="acacia", selected_column=1)
grid.set_cell("acacia", "revenue", 2400)
assert grid.selected_row.cells == ("Acacia", 2400)
assert grid.selected_cell_text == "2400"

# Replace a known row, append a new key, and remove an existing key atomically.
grid.update_rows((
    GridRow("acacia", ("Acacia", 2600)),
    GridRow("cedar", ("Cedar", 630)),
), remove=("birch",))
assert tuple(row.key for row in grid.rows) == ("acacia", "cedar")
assert grid.selected_key == "acacia"

# Replace the entire dataset, for example after loading or filtering records.
grid.set_data(columns=columns, rows=(GridRow("cedar", ("Cedar", 630)),))
assert grid.selected_row is None  # The selected key no longer exists.
```

`update_rows()` preserves the source order of existing keys and appends new
ones. A removal key must exist and cannot also appear in the update tuple.
`set_data()` validates the whole replacement before publishing it; surviving
selection and sort keys are retained. Neither operation emits individual cell
edit events. The records returned by `rows` and `selected_row` are snapshots:
replace them through these methods rather than trying to mutate their cells.

`set_cell()` validates one value and emits `GridEditEvent` when it changes.
The event carries `row_key`, `column_key`, `old_value`, `new_value`, `source`
and `origin`. Programmatic calls default to `origin="program"`; a committed
editor action uses `"user"`. The column's `editable` flag controls the user
editor, not application updates. Invalid data leaves the current dataset intact.

Numeric editors accept plain numbers and the column's copied decimal presentation,
including its prefix, suffix, and comma or underscore grouping with an empty
format specification or an `f`/`F` format. For example, a column using `prefix="$"` and
`format_spec=",.2f"` accepts `$1,234.57`. Pasting commits the displayed, rounded
amount; it cannot recover precision omitted by formatting. Other display formats
continue to use plain numeric entry.

Selection changes emit `ChangeEvent[str | None]` through `grid.changed`, with
`old_value` and `new_value` row keys. `selected_column` is a zero-based column
index; `selected_row` and `selected_cell_text` return `None` without selection.
In an App, `grid_on_changed` and `grid_on_edited` bind these events by name.
Callbacks are delivered while the control belongs to a running App; connecting
them before opening is supported. Detached recipe updates still change data,
but do not dispatch callbacks.
For filtering, keep the complete source tuple in your application and update it
from edit events before rebuilding the filtered grid, as the dashboard does.

### Tree replacement, selection and reveal

`TreeNode` records contain a key, display text and an immutable children tuple.
Keys must be unique across the entire tree, including nodes in closed branches.
This recipe also runs without opening a window:

<!-- recipe: tree-data -->
```python
from pysual import ChangeEvent, TreeNode, TreeView

tree = TreeView(nodes=(
    TreeNode("projects", "Projects", (
        TreeNode("report", "Report"),
        TreeNode("notes", "Notes"),
    )),
))
selections = []

def record_selection(event: ChangeEvent[str | None]):
    selections.append(event.new_value)

tree.changed.connect(record_selection)
tree.selected_key = "report"  # Expands the ancestors and reveals the row.
assert tree.selected_node.text == "Report"
assert "projects" in tree.expanded_keys

tree.reveal("notes")  # Scroll/expand without changing the selected key.
assert tree.selected_key == "report"

# Keep surviving selection/expansion keys when replacing the tree.
tree.set_tree((TreeNode("projects", "Projects", (
    TreeNode("report", "Quarterly report"),
)),))
assert tree.selected_node.text == "Quarterly report"

# An explicit None clears selection during replacement.
tree.set_tree((TreeNode("archive", "Archive"),), selected_key=None)
assert tree.selected_node is None
```

Omit `selected_key` and `expanded_keys` in `set_tree()` to preserve keys that
still exist; pass explicit values to choose the new state. Selecting a
descendant expands its ancestors. `reveal(key)` expands and scrolls without
selecting; `toggle(key)` opens or closes a branch. Collapsing a selected node's
ancestor moves selection to a visible ancestor. Unknown keys are rejected.

`tree.changed` emits `ChangeEvent[str | None]` for selection, and
`expanded_changed` emits `ChangeEvent[tuple[str, ...]]` for expansion.
Enter emits `TreeEvent` through `activated`; its `key` identifies the selected
node. Bind with `tree_on_changed`, `tree_on_expanded_changed` and
`tree_on_activated` when the control is named `tree` in an App.
Call `grid.destroy()` / `tree.destroy()` when finished with detached recipe
controls; destroying their owning App disposes attached controls automatically.

### Text editing and history

Use `select(start, end)` for Python string offsets between zero and `len(text)`;
out-of-range offsets raise `ValueError`. The control snaps valid offsets to
Unicode grapheme boundaries. `selection_range` returns
the ordered endpoints, while `selection_text` exposes the selected text.
During a selection drag, holding the pointer beyond an editor edge continues
scrolling and extending the selection until release or the document boundary.

```python
from pysual import TextBox

note = TextBox(text="Hello, world")
note.select(7, 12)
note.replace_selection("Pysual")
assert note.text == "Hello, Pysual"
note.undo()
assert note.text == "Hello, world"
note.redo()
```

`replace_selection()` inserts at the caret when nothing is selected. It normalizes
line endings, moves the caret after inserted text, and records one undoable edit;
a changed edit clears redo. Read-only controls ignore the edit. Rejected
validation preserves the text, selection, and history. Use `load_text(new_text)`
to replace an entire document and reset its selection, scroll, and history.
Use editing methods when the user should be able to undo the operation.

Native windows rasterize long text into the visible area before uploading a
texture, preserving full-line shaping and the complete document. The existing
printable-ASCII monospace path also limits Python drawing to visible fragments.
A subclass overriding only `paint_line()` continues receiving complete rows;
override `paint_line_fragment(painter, text, x, y, row, style, start_column)` as
well to participate in that fragment path. `start_column` refers to the original
row's string offsets. Web measurement composes canonical accents on a measuring
copy without normalizing stored text or selection indices.

### Popup content

A `Popup` is a transient subtree owned by the running app. Create fresh content
and call `show(anchor)` from a handler; it returns immediately. The popup is
placed below its anchor, or above when needed, and kept inside the app area.
`show(anchor, at=(x, y))` instead uses an app-logical point.

```python
from pysual import App, Button, Label, Popup

class HelpDemo(App):
    def build(self):
        self.help = Button(text="Show help")

    def help_on_click(self, event):
        popup = Popup(layout="stack", width=240, padding=12)
        Label(parent=popup, text="Choose a region to filter the report.")
        popup.show(self.help)
```

`dismiss()` closes and disposes the popup; create a new one for the next opening.
Opening another popup replaces the current one. A popup cannot be anchored
inside another popup. NumericInput and ColorPicker use this same behavior:
their `edit()` methods return immediately, Apply or Enter commits a valid value,
and dismissal leaves the previous value unchanged.

## Layout

All backends share measurement, arrangement, clipping, and hit testing.

| Mode | Use |
| --- | --- |
| `stack` | Vertical/horizontal rows with spacing and flex |
| `grid` | Fixed, automatic, and proportional tracks |
| `flow` | Wrapping items |
| `dock` | Edges and remaining content |
| `absolute` | Explicit coordinates and anchors |

Use `width`, `height`, minimum/maximum dimensions, `padding`, `spacing`, and
`flex` to describe geometry. `ScrollArea` clips and scrolls its contents. Terminal
layout uses the same logical geometry projected into character cells, so tiny
pixel decorations and tightly packed text need a terminal-appropriate design.

### Custom measurement

Override `measure(host)` to return a preferred `(width, height)` in logical
pixels. Override `measure_available(host, *, width=None, height=None)` when
preferred size depends on the space offered by the parent, such as wrapped text.
Explicit dimensions and minimum/maximum limits are applied separately.

Measurement results are cached by default for every control, including custom
subclasses. The shared layout code keeps a bounded set of results for different
offered sizes and accounts for theme, host metrics, and attachment context.
There is no custom-control registration step. Parent `arrange_children` hooks
still decide child rectangles through the existing layout path; caching only
reuses a preferred-size answer when its inputs are unchanged.

Treat measurement as a repeatable size query: the same inputs should give the
same result. Do not rely on it running once per frame, mutate the tree, or put
application work in it. Declare properties that affect preferred size with
`affects=Dirty.MEASURE | Dirty.PAINT`; properties that affect only drawing can
keep the default `Dirty.PAINT`.

When private or external sizing state changes, call
`control.invalidate(Dirty.MEASURE)`. If one control's measurement reads another
control's state, explicitly invalidate the dependent control when that state
changes, for example from the source's change handler. These dependencies are
not discovered by tracking property reads. `invalidate()` without arguments
requests painting and does not invalidate a cached preferred size.

Set `cache_measure=False` on a control whose measurement cannot follow that
dependency contract. Its size query is not reused, and a cached ancestor cannot
hide it when measuring the subtree. This option does not request continuous
layout or poll external state: a size change still needs a layout request, such
as `invalidate(Dirty.MEASURE)`. Measurement caching is independent of
`cache_paint`.

## Themes and custom painting

```python
from pysual import get_theme, theme_names

ui.theme = get_theme("macos_dark")
print(theme_names())
```

The catalog contains nine families, each with a light and dark version (18
themes). The base identifier selects light; append `_dark` to select dark:

| Family | Light | Dark | Visual character |
| --- | --- | --- | --- |
| Modern | `modern` | `modern_dark` | Soft surfaces, balanced spacing, and gentle elevation |
| Windows | `windows` | `windows_dark` | Compact primary actions, restrained corners, and accented fields |
| macOS | `macos` | `macos_dark` | Rounded gradient actions, recessed wells, and raised selections |
| Terminal | `terminal` | `terminal_dark` | Monospaced ncurses and MS-DOS panels with crisp outlines |
| Win31 | `win31` | `win31_dark` | Square beveled controls and deep blue window titles |
| WinXP | `winxp` | `winxp_dark` | Glossy blue chrome, cream surfaces, and green progress |
| Macintosh | `macintosh` | `macintosh_dark` | Monochrome controls, striped title bars, and hard shadows |
| Analog arcade | `analog_arcade` | `analog_arcade_dark` | Warm cabinet colors, scanlines, and tactile illuminated controls |
| Neon | `neon` | `neon_dark` | Cyan and magenta accents with luminous outlines |

`theme_names()` returns only these identifiers, paired in the order above.
`light()` and `dark()` construct the Modern appearances. Only the 18 catalog
identifiers are supported; there are no legacy theme aliases. Theme lookup is
case-insensitive; unknown names raise `ValueError`. Startup configuration also
uses the catalog identifiers, such as `modern` and `modern_dark`.

Immutable `Theme`, `Tokens`, `Style`, and `Rule`
objects describe colors, fonts, control parts, interaction states, gradients,
shadows, and surfaces. `load_theme()` and `save_theme()` serialize theme data.
Set `reduce_motion=True` for immediate interaction states.

Theme changes preserve control values and application state. Primary actions,
secondary controls, inset editors, selected rows, focus outlines, and disabled
states retain distinct roles within each family. Try
[theme_gallery.py](../examples/theme_gallery.py) for live control interactions
and family/appearance switching. These are portable rendered interpretations;
terminal hosts approximate them within their character grid.

`SubWindow` exposes `body`, `titlebar`, and `close` style parts. The close button
uses the title bar's foreground and typography by default; themes can give its
normal, hover, pressed, and disabled states a separate face. Without a `close`
rule, its face is transparent.

ProgressBar percentage text uses the `foreground` of its `track` and `fill`
parts on the corresponding portions of the bar. Customize those parts when
changing their backgrounds so that text remains readable across the boundary.

Custom controls subclass `Control`, declare typed observable properties using
`prop`, and implement `paint(painter)`. Painter coordinates are **local to the
control**. Its primitives work across the three backends; text terminals
approximate images, gradients, and geometry within their cell grid.

`painter.line(x1, y1, x2, y2, color, width=1)` draws one stroke;
`painter.lines(points, color, width=1)` connects consecutive `(x, y)` points.
Use `painter.segments(strokes, color, width=1)` for independent strokes, each
given as `(x1, y1, x2, y2)`. They share a color and width and never connect to
the next stroke. Hosts may batch these operations; hosts without the optional
`segments` method receive ordinary `line` calls in the original order.

```python
from pysual import Control, Dirty, Rect, prop

class Meter(Control):
    value: float = prop(default=0.5, minimum=0)
    caption: str = prop(default="Level", affects=Dirty.MEASURE | Dirty.PAINT)

    def measure(self, host):
        width, height = host.measure(self.caption, 16)
        return width + 24, max(32, height + 16)

    def paint(self, painter):
        painter.rect(Rect(0, 0, self.bounds.width * min(1, self.value),
                          self.bounds.height), "#127c6a", radius=8)
        painter.text(self.caption, 12, 8, color="#ffffff", size=16,
                     font_family="ui")
```

Changing `caption` invalidates this meter's preferred size and painting;
changing `value` only repaints. The complete
[custom control example](../examples/custom_control.py) connects the meter to a
slider.

For private drawing state, set `cache_paint=True` only when all changes call
`invalidate()`. Uncached custom painters run on requested updates. Never mutate
the control tree during painting or layout.

`Control.paint_focus(painter)` draws the keyboard outline after the cached body.
Composite controls may override it when their owner provides the visible focus
indication, as SearchField does for its internal entry. Invalidate that owner's
paint on focus and blur so retained and direct renderers agree.

### Composing control behavior

`PressBehavior` supplies pointer and keyboard gestures independently of a
control's properties and painting. Create one per control in `_initialize`,
delegate input, and reset it when destroying the control:

```python
from pysual import Control, Dirty, PressBehavior, Rect, prop, register_control

class Latch(Control):
    checked: bool = prop(default=False)
    focusable: bool = prop(default=True, affects=Dirty.HIT_TEST)

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior()

    def handle_input(self, event, /):
        if self._press.handle_input(self, event) and self.effective_enabled:
            self.checked = not self.checked

    def destroy(self):
        self._press.reset(self)
        super().destroy()

    def paint(self, painter, /):
        painter.body()
        painter.marker(Rect(8, 8, 20, 20), painter.style(), checked=self.checked)

register_control(Latch, factory=False)  # Discoverable; create with parent.create(Latch).
```

The behavior returns `True` on a matching primary-pointer release inside the
control or an Enter/Space release. It updates pressed styling, ignores keyboard
repeat, and cancels on blur or pointer cancellation. It clears the completed
gesture before returning, so the action can open a popup or destroy its control.
`keys=()` leaves keyboard handling to the control; `part=` identifies a hit
region that must match on pointer press and release, such as a numeric editor's
minus/value/plus regions. Call behavior methods from control hooks on the UI
thread. A behavior never retains its owner or emits events itself.

Existing `Button`, `CheckBox`, `Toggle`, `RadioButton`, and `Hyperlink` subclassing
continues to work. New control families can use this component directly with
`Control` and choose their own schema, action, and rendering. See the
[control architecture guide](control-architecture.md) for catalog development.

Appearance can be reused separately from behavior. For example,
`style_fallbacks = ("Button",)` on a direct Control subclass adds button defaults
before the class's own theme selector. `style_excludes = ("Button",)` omits that
selector. Both are class metadata inherited by subclasses; ordinary theme rule
and interaction-state precedence remain unchanged.

### Composing editors and panels

`NumericEditor(value, decimals=2, parent=panel)` and
`ColorEditor(value, parent=panel)` are reusable draft content. Mount them inline,
read the draft with `read()`, or supply fresh content to
`EditorPopup(owner, content, value_property="value").show(owner)`. The popup
assigns through the owner's observable property and preserves its validation,
change events, and lifetime. Invalid drafts remain open; dismissal never commits.
The popup is anchored to its value owner so disposing the owner closes editing.

Custom draft content subclasses `EditorContent`, builds ordinary children in
`build(value)`, and implements `read()`. Optional `is_untouched()`,
`equivalent(value, current)`, and `error_message(error, owner)` hooks provide
precision, semantic equality, and feedback policies. The default equality hook
does not bypass owner validation. Calling `submit()` emits `submitted`, which
an inline host or the popup handles. Each editor instance owns its draft.

Larger controls use ordinary Container composition. See
[composite_control.py](../examples/composite_control.py) for a range field made
from Label, Slider, and NumericInput, with independent instances and batched
value synchronization. Small child adapters validate edits against the parent
before committing, then synchronize its value and the other editor immediately.
The parent preserves the incoming user/program origin. Queued `changed` handlers
remain available for application reactions after the values are coherent.

### Rating, search, and dates

These catalog controls use direct constructors or `Container.create`; they do
not add named factory methods to Container:

```python
from pysual import CalendarEditor, DatePicker, Rating, SearchField

score = panel.create(Rating, value=3, maximum=5)
query = panel.create(SearchField, placeholder="Search reviews")
due = panel.create(DatePicker, value="2026-10-15",
                   minimum="2026-01-01", maximum="2027-12-31")
draft = CalendarEditor("2026-10-20", parent=panel,
                       minimum=due.minimum, maximum=due.maximum)
```

`Rating.value` is an integer from zero (unrated) through `maximum`. Pointer
release on the pressed star selects it; hovering previews the score. Arrow
keys change one star, Home clears, End selects the maximum, and Delete or
Backspace clears. `read_only=True` preserves presentation while preventing
edits. The `changed` event carries the old and new integer values.

`SearchField.text` is the semantic value. `changed` emits after edits or
programmatic updates; `submitted` requests a search when Enter is pressed.
Read `query.text` in the handler. The clear button clears the text and returns
focus to the entry. `focus()` focuses the entry, and `read_only=True` permits
selection and submission but prevents editing and clearing. Filtering,
suggestions, and network requests remain application responsibilities.
The search icon focuses the entry. The composite draws one continuous themed
input surface and a shared focus outline; the clear glyph appears only when
there is text, without shifting the entry as the query changes.

`DatePicker.value`, `minimum`, and `maximum` use valid, canonical Gregorian
dates in `YYYY-MM-DD` format. Bounds are inclusive. Use
`due.update(minimum=..., maximum=..., value=...)` to change dependent values
atomically. `open()` presents a calendar; Apply commits through the picker's
current validation, while Escape or dismissal leaves its value unchanged.
`read_only=True` prevents opening an editor. The `changed` event carries ISO
strings.

`CalendarEditor` owns an independent draft, a `YYYY-MM-DD` entry field,
previous/next month navigation, weekday headings, and a day grid. Type or paste
a date in the field and press Enter or Apply; invalid or out-of-range dates
leave the popup open with feedback. Choosing a day replaces the text draft.
`read()` validates and returns the ISO date; inline hosts should handle
`ValueError` for an invalid draft.
Mount it inline as above, or supply fresh content to `EditorPopup`; inline
hosts decide when to read or commit it. See
[catalog_controls.py](../examples/catalog_controls.py) for a runnable example
using all thirteen catalog additions, including both calendar presentations and
a live theme selector.

### Ranges, progress, and navigation

```python
from pysual import Breadcrumb, CircularProgress, Pagination, RangeSlider, SegmentedControl

effort = panel.create(RangeSlider, minimum=0, maximum=12, value=(2, 8),
                       lower_label="Min hours", upper_label="Max hours")
completion = panel.create(CircularProgress, value=68, maximum=100)
view = panel.create(SegmentedControl, items=("All", "Pending", "Complete"),
                     selected_index=0)
path = panel.create(Breadcrumb, items=("Workspace", "Reviews", "API design"))
pages = panel.create(Pagination, page=1, page_count=20, visible_pages=5)
```

`RangeSlider.value` is an ordered `(lower, upper)` tuple within `minimum` and
`maximum`. Drag the nearest thumb; the ends stop at each other. Space or Enter
switches the active thumb, arrows move by `step`, PageUp/PageDown move ten steps,
and Home/End reach that thumb's allowed extremes. `active_thumb` is `"lower"` or
`"upper"`; `read_only` blocks user edits. `show_values` controls the visible end
labels. Use `set_range(minimum, maximum, value=...)` or `update(...)` for an atomic
change of bounds and values; omitting the value from `set_range` clamps the pair.
The `changed` event carries the old and new tuples.

`CircularProgress` is determinate: `0 <= value <= maximum`, with a positive
maximum. `show_text` toggles its centered percentage and `thickness` sets the ring
width. `set_range(maximum, value=...)` changes dependent values atomically and
clamps an omitted value. It has no interactive or indeterminate mode. The
`track` and `fill` theme parts color the arcs; the body foreground colors the text.

`SegmentedControl.items` is an immutable tuple of labels. `selected_index=-1`
means no selection; otherwise the index must exist. Arrow keys and Home/End
select an item, while Enter/Space select the active item. `selected_item` is the
label or `None`, and `changed` carries integer indices. A constrained row reveals
the selected segment. Use `update(items=..., selected_index=...)` when replacing
the options and selection together.

`Breadcrumb.items` describes a path owned by the application. The final label
is current and inert. Clicking an ancestor, calling `activate(index)`, or using
arrows/Home/End followed by Enter/Space emits `navigated` with a `BreadcrumbEvent`
containing `index` and `item`. Handle `path_on_navigated` to load that destination
and replace the path. Long paths collapse intermediate labels while retaining
the root, current item, and keyboard destination.

`Pagination.page` is one-based, between 1 and `page_count` inclusive.
`visible_pages` (1–15, default 5) bounds the central page window; first/last links
and ellipses appear when space allows. Previous/next disable at the boundaries;
arrows and Home/End work from the keyboard. `changed` carries page numbers.
On filtering, use `update(page_count=..., page=...)` to commit a new count and
valid page together. Represent an empty result set with `page_count=1`, `page=1`,
and `enabled=False`; the control does not fetch or slice application data.

### Choice cards, disclosure groups, and feedback

```python
from pysual import AlertBanner, Badge, ChoiceCard, Disclosure, Label, Meter

focused = panel.create(ChoiceCard, text="Focused", description="One area",
                      icon="search", group="approach", checked=True)
complete = panel.create(ChoiceCard, text="Full pass", group="approach")
details = panel.create(Disclosure, title="Advanced settings", expanded=False)
Label(parent=details.content, text="State survives collapsing this section")
status = panel.create(Badge, text="Ready", tone="success")
notice = panel.create(AlertBanner, text="Your review is ready", action_text="Open")
capacity = panel.create(Meter, label="Capacity", value=65, unit="%",
                        segments=20, warning_at=75, danger_at=90)
```

`ChoiceCard` groups are exclusive among siblings with the same `group` (including
the default empty group). `checked` emits a boolean `changed` event. Matching
pointer releases or Enter/Space select; arrows and Home/End move through available
peers. `read_only` blocks user selection, and `icon` uses the bundled icon catalog.
Use `orientation="vertical"` for option tiles with an icon plaque, a corner
selection indicator, and up to three wrapped description lines. The default
`"horizontal"` presentation remains suitable for compact rows. Both use the
`icon` theme part's surface and foreground.

`Disclosure.content` is an owned vertical-stack Container. Add children there, or
pass a detached subtree as `content=` at construction to adopt it inside the
container. `expanded` emits `changed`; `toggle_expanded()` toggles when enabled.
Its heading supports Enter/Space, Left to collapse, and Right to expand. Collapsing
preserves child values and visibility, excludes the subtree from picking and tab
navigation, and returns focus from a hidden child to the heading. Destruction
disposes the owned subtree.

`Badge` is a noninteractive caption with an optional `show_dot`. Its `tone` is
`neutral`, `info`, `success`, `warning`, or `danger`. `AlertBanner` supports the
same tones except neutral, an optional `action_text`, and `dismissible=True`.
Handle `action` to perform the application action. `dismiss()` hides the banner
and emits `dismissed` once; set `visible=True` to show it again. Arrow keys and
Home/End choose its action or close button, Enter/Space activate, and Escape
dismisses. `activate()` and `dismiss()` require a running App, like button
activation. Neither control performs application work on its own.

`Meter` represents a measured level within a strictly increasing `minimum` and
`maximum`, with optional `warning_at` and `danger_at` thresholds in ascending
order. Higher values enter those ranges. `segments=0` draws a continuous bar;
1–100 draw discrete segments. `label`, `unit`, and `show_value` control the caption.
Use `update(...)` to validate dependent properties atomically or
`set_range(minimum, maximum, value=...)` to clamp an omitted value and existing
thresholds to the new range. Value changes emit `changed`. Tone parts and meter
`track`/`fill` parts can be customized through ordinary theme rules.

These five additions use direct construction, `Container.create`, and blueprints.
They add no reserved Container factory names. The catalog demo combines them with
the earlier controls, adapts to a single column on smaller windows, and lets you
switch all 18 themes without resetting application state.

## Performance and host services

Rendering is on demand by default. `request_frame()` requests an update;
`redraw_interval=0` requests continuous updates and `fps_limit` bounds them.
Native segments and caches retain unchanged control drawing. Native declared
sprites and animated rectangles run in C; arbitrary Python animation handlers
continue to use Python.

`app.capabilities` reports optional services such as clipboard, text files,
session storage, URL opening, and composition. Handle unsupported/denied
operations explicitly. Browser file operations require user interaction.
Measure Python scene updates, native presentation, and end-to-end input latency
separately; cached presentation counts are not physical display refresh rates.

### Rendering diagnostics

`app.diagnostics()` returns an immutable `RenderDiagnostics` snapshot without
requesting a frame or contacting the native process:

```python
stats = app.diagnostics()
print(stats.scene_submissions, stats.backend, stats.host_name)

# Opt in to one native statistics IPC request, when the host supports it.
sampled = app.diagnostics(include_native=True)
print(sampled.native_presentations)  # None for Python-terminal and web hosts.
```

`scene_submissions` counts completed Python scene submissions in this opening.
`native_presentations` counts native presentations, which can also redraw a
retained scene without a new Python submission. It is `None` unless explicitly
sampled on a supported native host after the first scene has completed. Reading
either counter does not measure physical display refresh or browser paint.
The existing `frame_count` behavior remains unchanged.

Identity fields are `host_name`, `backend`, `web_execution` (`"live"` or
`"bundled"` for web), `terminal_renderer` (`"python"` or `"c"`), and `hidden`.
Inapplicable or unknown facts are `None`; outside an active opening the snapshot
has zero submissions and no identity or native count. Calls from workers route
to the UI owner, and reading a static scene does not request ongoing rendering.
Use `drain_frame_timings()` for Python timing samples, `cache_stats` for cache
use, and `resource_errors` for rendering resource failures.

With the `dev` dependencies installed, `python tools/benchmark_png.py` measures
cold RGB PNG decoding and verifies exact output pixels. Its JSON output includes
dimensions, interpreter version, individual samples, and the median. Keep the
same dimensions, repeat count, and machine conditions when comparing changes;
fixture creation and disk access are excluded from the decode timings.

For property/layout, retained painting, and native transport measurements, run
`python tools/benchmark_core.py`. For sparse retained SVG preparation, run
`python tools/benchmark_web_scene.py`. Both emit JSON with interpreter/platform,
revision (when Git is available), working-tree state, scenario settings, explicit
warmup/sample counts, raw samples, and their existing summary fields. A missing
native helper is recorded as a skip with its reason. Native records include
renderer identity and requested/actual VSync where the host exposes them.

Save the complete JSON for each run. Compare the same scenarios, dimensions,
sample counts, backend, and machine conditions; repeat runs after other workloads
finish. Record the changes when comparing dirty checkouts: a commit ID alone
does not identify uncommitted edits. SVG timings cover Python composition and
packet encoding, not browser layout/paint. Native present timings cover
submission/acknowledgement, not GPU completion or display scanout. Cold and warm
cache samples are separate workloads, and hidden-terminal timings exclude the
terminal emulator's display work. Do not interpret these counts as display FPS.
