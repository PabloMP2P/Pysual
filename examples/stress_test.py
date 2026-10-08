"""Interactive mixed-control stress lab; timings measure work, not screen refresh.

The readout names Backend FPS (host presentation) and Full cycle FPS (completed
Python scene submissions) for the active preset. Dashboard edits apply immediately.
Live metrics refresh at 4 Hz unless --no-telemetry is set. Timed runs print that line.
Defaults are the large preset, continuous redraw, and VSync off.
Try --workload property --fraction .2 --duration 10 --report run.json
or --all-presets --duration 10 to print one line for small, medium, large, and extreme.
Use --backend window|terminal|web and --pysual-theme NAME through autoconfig.
No third-party imports or adjacent assets are required by a bundled HTML app.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path
import random
import sys
from time import perf_counter

from pysual import (
    App, Button, CapabilityError, ChartSeries, ChartSlice, CheckBox, ColorPicker,
    ComboBox, Container, DataGrid, DonutChart, Dropdown, GridColumn, GridRow,
    Hyperlink, Image, Label, LineChart, ListView, NumericInput, ProgressBar,
    RadioButton, Separator, Slider, TextBox, Toggle, TreeNode, TreeView,
    get_theme, image_source, theme_names,
)


PRESETS = {"small": 100, "medium": 500, "large": 2000, "extreme": 5000}
WORKLOADS = ("static", "paint", "layout", "property", "local-update")
MIXES = {
    "balanced": ("label", "button", "checkbox", "toggle", "slider", "progress",
                 "text", "number", "dropdown", "list", "line", "donut",
                 "combo", "radio", "color", "grid", "tree", "image", "link", "separator"),
    "text": ("label", "text", "button", "list", "dropdown"),
    "forms": ("checkbox", "toggle", "slider", "text", "number", "dropdown", "combo", "radio", "color"),
    "visual": ("progress", "slider", "line", "donut", "image", "label"),
    "collections": ("list", "grid", "tree", "dropdown", "combo"),
}
TIMING_CAPACITY = 10_000
PIXEL = image_source(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAYAAABytg0kAAAAG0lEQVR4nGPwWXfrf/6lef8Zvq5J+7+q/fl/AGW2C8/9seZDAAAAAElFTkSuQmCC"
))


def redraw_value(value):
    if value == "on-demand":
        return None
    if value == "continuous":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("redraw must be on-demand, continuous, or seconds") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError("redraw seconds must be finite and nonnegative")
    return number


@dataclass(frozen=True)
class StressConfig:
    controls: int = PRESETS["large"]
    mix: str = "balanced"
    workload: str = "paint"
    fraction: float = 0.1
    rate: float = 30.0
    seed: int = 7
    warmup: float = 2.0
    duration: float = 0.0
    telemetry: bool = True
    fps_limit: int = 120
    redraw: float | None = 0.0
    cache_mib: float = 32.0
    body_cache: bool = True
    vsync: bool | None = False
    full_loop: bool = False

    def __post_init__(self):
        if type(self.controls) is not int or not 0 <= self.controls <= 20_000:
            raise ValueError("controls must be an integer from 0 to 20000")
        if self.mix not in MIXES or self.workload not in WORKLOADS:
            raise ValueError("unknown control mix or workload")
        for name in ("fraction", "rate", "warmup", "duration", "cache_mib"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} must be finite")
        if not 0 <= self.fraction <= 1 or not 0 < self.rate <= 1000:
            raise ValueError("fraction must be 0..1 and rate must be >0..1000")
        if min(self.warmup, self.duration, self.cache_mib) < 0:
            raise ValueError("warmup, duration and cache_mib must be nonnegative")
        if type(self.seed) is not int or not 0 <= self.seed <= 2**31 - 1:
            raise ValueError("seed must be an integer from 0 to 2147483647")
        if type(self.fps_limit) is not int or not 0 <= self.fps_limit <= 1000:
            raise ValueError("fps_limit must be an integer from 0 to 1000")
        for name in ("telemetry", "body_cache", "full_loop"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if self.vsync is not None and type(self.vsync) is not bool:
            raise ValueError("vsync must be boolean or None")
        if self.redraw is not None:
            if type(self.redraw) not in (int, float):
                raise ValueError("redraw must be numeric seconds or None")
            redraw_value(self.redraw)
        if self.full_loop and self.redraw is None:
            raise ValueError("full-loop requires --redraw continuous or an interval")


def preset_label(controls):
    """Name a built-in preset, or the exact control count when it matches none."""
    name = next((name for name, count in PRESETS.items() if count == controls), None)
    return f"Preset {name}" if name is not None else f"{controls} controls"


def format_fps_readout(controls, measurements):
    """One line: preset, backend presentation FPS, and full-cycle submission FPS."""
    if not measurements:
        backend = cycle = "—"
    else:
        backend = f"{measurements['backend_fps']:.1f}"
        cycle = f"{measurements['full_cycle_fps']:.1f}"
    return f"{preset_label(controls)} · Backend FPS {backend} · Full cycle FPS {cycle}"


def percentiles(values):
    """Inclusive linear percentiles, in the same units as the supplied values."""
    ordered = sorted(values)
    if not ordered:
        return {"p50": None, "p95": None, "p99": None}

    def quantile(fraction):
        position = (len(ordered) - 1) * fraction
        low, high = math.floor(position), math.ceil(position)
        return ordered[low] + (ordered[high] - ordered[low]) * (position - low)

    return {f"p{p}": quantile(p / 100) for p in (50, 95, 99)}


class Measurements:
    """One resettable window: counter rates include idle time; timing rings do not."""

    def __init__(self, started_at, frames=0, native=None):
        self.started_at = started_at
        self.start_frames = frames
        self.native_start = native
        self.last_number = frames
        self.samples = deque(maxlen=TIMING_CAPACITY)
        self.mutations = deque(maxlen=TIMING_CAPACITY)
        self.samples_seen = 0
        self.dropped = 0
        self.mutation_ticks = 0
        self.controls_touched = 0

    def collect(self, samples):
        for sample in samples:
            if sample.number <= self.last_number:
                continue
            self.dropped += max(0, sample.number - self.last_number - 1)
            self.last_number = sample.number
            # Exclude submissions which straddle the warmup boundary.
            if sample.completed_at - sample.update_seconds < self.started_at:
                continue
            self.samples.append(sample)
            self.samples_seen += 1

    def mutation(self, seconds, touched):
        self.mutations.append(seconds * 1000)
        self.mutation_ticks += 1
        self.controls_touched += touched

    def summary(self, now, frames, native=None):
        elapsed = max(0.0, now - self.started_at)
        updates = max(0, frames - self.start_frames)
        result = {
            "elapsed_seconds": elapsed,
            "scene_submissions": updates,
            "scene_submissions_per_second": updates / elapsed if elapsed else 0.0,
            "timing_samples": len(self.samples),
            "timing_samples_seen": self.samples_seen,
            "timing_samples_dropped_before_collection": self.dropped,
            "timing_samples_evicted": max(0, self.samples_seen - len(self.samples)),
            "timings_ms": {
                label: percentiles(getattr(s, field) * 1000 for s in self.samples)
                for label, field in (
                    ("python_layout", "layout_seconds"),
                    ("paint_and_submit", "paint_seconds"),
                    ("scene_update", "update_seconds"),
                    ("input_routing", "input_seconds"),
                )
            },
            "mutation_ticks": self.mutation_ticks,
            "controls_touched": self.controls_touched,
            "mutation_ticks_per_second": self.mutation_ticks / elapsed if elapsed else 0.0,
            "mutation_ms": percentiles(self.mutations),
            "native": None,
        }
        if native is not None and self.native_start is not None:
            baseline = self.native_start
            seconds = max(0, native["elapsed_seconds"] - baseline["elapsed_seconds"])
            counters = {}
            for name in ("frames", "scene_updates", "cell_frames_composed",
                         "ansi_frames_encoded", "terminal_bytes_written"):
                if name in native and name in baseline:
                    delta = max(0, native[name] - baseline[name])
                    counters[name] = {"count": delta, "per_second": delta / seconds if seconds else 0}
            updates = counters.get("scene_updates", {}).get("count", 0)
            available = native.get("scene_update_samples_ms", ())
            recent = available[-min(int(updates), len(available)):] if updates else ()
            result["native"] = {
                "elapsed_seconds": seconds,
                "renderer": native.get("renderer"),
                "counters": counters,
                "scene_update_mean_ms": (
                    1000 * max(0, native.get("scene_update_seconds", 0)
                               - baseline.get("scene_update_seconds", 0)) / updates
                    if updates else None
                ),
                "scene_update_recent_ms": percentiles(recent),
                "scene_update_timing_samples": len(recent),
                "scene_update_timing_samples_missing": max(0, updates - len(recent)),
                "continuous": native.get("continuous"),
                "fps_limit": native.get("fps_limit"),
                "vsync": native.get("vsync"),
            }
        result["full_cycle_fps"] = result["scene_submissions_per_second"]
        presented = (result["native"] or {}).get("counters", {}).get("frames")
        if presented is not None:
            result["backend_fps"] = presented["per_second"]
            result["backend_fps_source"] = "native_presentations"
        else:
            result["backend_fps"] = result["full_cycle_fps"]
            result["backend_fps_source"] = "scene_submissions"
        return result


class StressTest(App):
    def __init__(self, config=None, **properties):
        super().__init__(**properties)
        self._configuration = config or StressConfig()
        self._cells = []
        self._tasks = []
        self._running = True
        self._measurements = None
        self._final_report = None
        self._host = None
        self._idle_event = None
        self._export_busy = False

    @property
    def configuration(self):
        return self._configuration

    def build(self):
        self.title, self.width, self.height = "Pysual stress lab", 1120, 820
        self.layout, self.padding, self.spacing = "stack", 12, 7
        self.profile_frames = True
        self.reduce_motion = True
        self.heading = Label(text="Mixed-control stress lab", font_size=23, height=30)
        self.options = Container(layout="grid", columns=8, height=114, spacing=5)
        fields = (
            ("preset_choice", "Preset", Dropdown(items=tuple(PRESETS), selected_index=tuple(PRESETS).index("large"))),
            ("count_input", "Controls", NumericInput(minimum=0, maximum=20000, decimals=0)),
            ("mix_choice", "Type mix", Dropdown(items=tuple(MIXES))),
            ("workload_choice", "Workload", Dropdown(items=WORKLOADS)),
            ("fraction_input", "Update fraction", NumericInput(minimum=0, maximum=1, step=.05)),
            ("rate_input", "Mutation ticks/s", NumericInput(value=30, minimum=0, maximum=1000)),
            ("seed_input", "Seed", NumericInput(minimum=0, maximum=2**31-1, decimals=0)),
            ("theme_choice", "Theme", Dropdown(items=theme_names())),
            ("limit_input", "Present cap (0 off)", NumericInput(minimum=0, maximum=1000, decimals=0)),
            ("redraw_choice", "Redraw", Dropdown(items=("on-demand", "continuous", "interval"), selected_index=1)),
            ("interval_input", "Redraw seconds", NumericInput(value=.25, minimum=0, step=.05)),
            ("cache_input", "Surface cache MiB", NumericInput(minimum=0)),
            ("body_cache", "Body reuse", CheckBox(text="Reuse bodies")),
            ("full_loop", "Python loop", CheckBox(text="Full loop")),
            ("vsync", "Window only", CheckBox(text="VSync")),
            ("telemetry", "Metrics overhead", CheckBox(text="Live 4 Hz")),
        )
        # A labeled two-row panel per field preserves useful keyboard tab order.
        for name, title, editor in fields:
            panel = Container(parent=self.options, layout="stack", spacing=0)
            Label(parent=panel, text=title, height=20, font_size=11)
            panel.add(editor)
            setattr(self, name, editor)
        self.actions = Container(layout="stack", direction="horizontal", height=34, spacing=8)
        for name, title in (("pause_button", "Pause"), ("reset_button", "Reset"),
                            ("sample_button", "Sample"), ("export_button", "Export JSON")):
            setattr(self, name, Button(parent=self.actions, text=title, width=130))
        self.status = Label(height=22, font_size=12)
        self.fps_readout = Label(text=format_fps_readout(self._configuration.controls, None), height=28, font_size=16)
        self.metrics = Label(text="Live 4 Hz is on. Sample reads the current window immediately.", height=48, font_size=12)
        self.field = Container(layout="grid", flex=1, spacing=2)
        self.note = Label(text="Backend FPS is host presentation. Full cycle FPS is Python submissions. Neither is screen refresh.", height=20, font_size=12)
        self._write_editors()
        self.apply_configuration(self._configuration)
        for name in ("preset_choice", "count_input", "mix_choice", "workload_choice",
                     "fraction_input", "rate_input", "seed_input", "theme_choice",
                     "limit_input", "redraw_choice", "interval_input", "cache_input",
                     "body_cache", "full_loop", "vsync", "telemetry"):
            getattr(self, name).changed.connect(self._editor_changed)

    def _write_editors(self):
        cfg = self._configuration
        for name, value in (("count_input", cfg.controls), ("fraction_input", cfg.fraction),
                            ("rate_input", cfg.rate), ("seed_input", cfg.seed),
                            ("limit_input", cfg.fps_limit), ("cache_input", cfg.cache_mib)):
            getattr(self, name).value = value
        self.mix_choice.selected_index = tuple(MIXES).index(cfg.mix)
        self.workload_choice.selected_index = WORKLOADS.index(cfg.workload)
        self.preset_choice.selected_index = next(
            (index for index, count in enumerate(PRESETS.values()) if count == cfg.controls), -1
        )
        self._theme_name = next((name for name in theme_names() if get_theme(name) == self.theme), "custom")
        self.theme_choice.selected_index = theme_names().index(self._theme_name) if self._theme_name != "custom" else 0
        self.redraw_choice.selected_index = 0 if cfg.redraw is None else 1 if cfg.redraw == 0 else 2
        if cfg.redraw:
            self.interval_input.value = cfg.redraw
        self.body_cache.checked, self.full_loop.checked = cfg.body_cache, cfg.full_loop
        self.telemetry.checked, self.vsync.checked = cfg.telemetry, bool(cfg.vsync)

    def _make_cell(self, kind, index, rng):
        value = rng.randrange(101)
        constructors = {
            "label": lambda: Label(text=f"Label {index}: αβ"),
            "button": lambda: Button(text=f"Action {index}", icon="check"),
            "checkbox": lambda: CheckBox(text=f"Check {index}", checked=bool(value % 2)),
            "toggle": lambda: Toggle(text=f"Toggle {index}", checked=bool(value % 2)),
            "slider": lambda: Slider(value=value),
            "progress": lambda: ProgressBar(value=value),
            "text": lambda: TextBox(text=f"Edit {index}", placeholder="Type here"),
            "number": lambda: NumericInput(value=value, minimum=0, maximum=100, decimals=0),
            "dropdown": lambda: Dropdown(items=("Alpha", "Beta", "Gamma"), selected_index=index % 3),
            "list": lambda: ListView(items=("North", "East", "South", "West"), selected_index=index % 4, row_height=16),
            "line": lambda: LineChart(series=(ChartSeries("s", "Signal", tuple((float(x), rng.random()) for x in range(8))),), show_legend=False),
            "donut": lambda: DonutChart(slices=(ChartSlice("a", "A", value + 1), ChartSlice("b", "B", 101-value)), show_legend=False),
            "combo": lambda: ComboBox(items=("Mercury", "Venus", "Earth"), text="Earth"),
            "radio": lambda: RadioButton(text=f"Radio {index}", group=f"group{index}", checked=bool(value % 2)),
            "color": lambda: ColorPicker(value="#7289fa"),
            "grid": lambda: DataGrid(columns=(GridColumn("name", "Name", 64), GridColumn("value", "Value", 64, kind="number")), rows=tuple(GridRow(str(i), (f"Row {i}", i)) for i in range(3))),
            "tree": lambda: TreeView(nodes=(TreeNode("root", "Root", (TreeNode("a", "Alpha"), TreeNode("b", "Beta"))),), expanded_keys=("root",), selected_key="a", row_height=16),
            "image": lambda: Image(source=PIXEL, fit="contain"),
            "link": lambda: Hyperlink(text=f"Link {index}"),
            "separator": lambda: Separator(),
        }
        cell = constructors[kind]()
        cell.cache_paint = self._configuration.body_cache
        self.field.add(cell)
        return cell

    def apply_configuration(self, config):
        """Rebuild the seeded scene and restart its measurement window."""
        if not isinstance(config, StressConfig):
            raise TypeError("config must be StressConfig")
        started = perf_counter()
        self._configuration = config
        for cell in self._cells:
            cell.destroy()
        rng = random.Random(config.seed)
        kinds = MIXES[config.mix]
        self._cells = [self._make_cell(kinds[i % len(kinds)], i, rng) for i in range(config.controls)]
        self.field.columns = max(1, math.ceil(math.sqrt(config.controls * 1.6)))
        self._order = list(range(config.controls))
        rng.shuffle(self._order)
        self._cursor = self._tick = 0
        self.fps_limit = config.fps_limit or None
        self.render_cache_bytes = int(config.cache_mib * 1024 * 1024)
        self._build_ms = (perf_counter() - started) * 1000
        self._write_editors()
        self.reset_measurements()

    def reset_measurements(self):
        """Keep the tree; discard statistics and start a fresh warmup."""
        self._measurements = self._final_report = None
        self.drain_frame_timings()
        self.redraw_interval = self._configuration.redraw if self._running else None
        self.status.text = f"{len(self._cells)} controls · {self._configuration.mix} · {self._configuration.workload} · warmup {self._configuration.warmup:g}s"
        self._show_fps(None)
        if self._host is None:
            return
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        self._idle_event = asyncio.Event()
        if self._running and not self.is_suspended:
            self._tasks = [self.create_task(self._drive()), self.create_task(self._measurement_clock())]
            if self._configuration.telemetry:
                self._tasks.append(self.create_task(self._live_metrics()))
        if self.vsync.enabled and self._configuration.vsync is not None:
            self._host.configure_rendering(redraw_interval=self.redraw_interval, fps_limit=self.fps_limit,
                                           reduce_motion=self.reduce_motion, vsync=self._configuration.vsync)

    def step_workload(self):
        """Run one deterministic mutation tick; return the touched control indices."""
        cfg = self._configuration
        if cfg.workload == "static" or not self._cells:
            return ()
        started = perf_counter()
        count = 1 if cfg.workload == "local-update" else math.ceil(len(self._cells) * cfg.fraction)
        chosen = tuple(self._order[(self._cursor + i) % len(self._order)] for i in range(count))
        self._cursor = (self._cursor + count) % len(self._order)
        self._tick += 1
        updates = {}
        for index in chosen:
            cell = self._cells[index]
            if cfg.workload == "paint":
                cell.invalidate()  # Every selected body is rerecorded, even with body reuse.
            elif cfg.workload == "layout":
                updates[cell] = {"margin": 0.0 if cell.margin else 1.0}
            elif cfg.workload == "local-update":
                updates[cell] = {"background": None if cell.background else "#34536c"}
            elif isinstance(cell, (CheckBox, Toggle)):
                updates[cell] = {"checked": not cell.checked}
            elif isinstance(cell, (Slider, ProgressBar, NumericInput)):
                updates[cell] = {"value": (cell.value + 7) % 101}
            elif isinstance(cell, (Dropdown, ListView)):
                updates[cell] = {"selected_index": (cell.selected_index + 1) % len(cell.items)}
            elif isinstance(cell, LineChart):
                updates[cell] = {"series": (ChartSeries("s", "Signal", tuple((float(x), (x + self._tick) % 8) for x in range(8))),)}
            elif isinstance(cell, DonutChart):
                updates[cell] = {"slices": (ChartSlice("a", "A", self._tick % 100 + 1), ChartSlice("b", "B", 101-self._tick % 100))}
            elif isinstance(cell, DataGrid):
                updates[cell] = {"rows": tuple(GridRow(str(i), (f"Row {i}", self._tick + i)) for i in range(3))}
            elif isinstance(cell, TreeView):
                updates[cell] = {"selected_key": "b" if cell.selected_key == "a" else "a"}
            elif isinstance(cell, ColorPicker):
                updates[cell] = {"value": "#d67466" if cell.value == "#7289fa" else "#7289fa"}
            elif isinstance(cell, Image):
                updates[cell] = {"fit": "stretch" if cell.fit == "contain" else "contain"}
            elif isinstance(cell, Separator):
                updates[cell] = {"orientation": "vertical" if cell.orientation == "horizontal" else "horizontal"}
            else:
                updates[cell] = {"text": f"{type(cell).__name__} {index}: {self._tick}"}
        if updates:
            self.field.update_children(updates)
        if self._measurements is not None:
            self._measurements.mutation(perf_counter() - started, len(chosen))
            self._measurements.collect(self.drain_frame_timings())
        return chosen

    def _native_snapshot(self):
        if self._host is not None and getattr(self._host, "native_retained", False):
            return self._host.native_stats()
        return None

    async def _measurement_clock(self):
        async def wait_seconds(seconds):
            deadline = perf_counter() + seconds
            while (remaining := deadline - perf_counter()) > 0:
                await asyncio.sleep(remaining)

        await wait_seconds(self._configuration.warmup)
        self.drain_frame_timings()
        self._measurements = Measurements(
            perf_counter(), self.diagnostics().scene_submissions, self._native_snapshot()
        )
        if self._configuration.duration:
            await wait_seconds(self._configuration.duration)
            self._final_report = self.snapshot_report()
            self._show_fps(self._final_report["measurements"])
            self.close()

    async def _drive(self):
        cfg = self._configuration
        next_mutation = perf_counter()
        period = 1 / cfg.rate
        previous_frame = -1
        while True:
            now = perf_counter()
            dynamic = cfg.workload != "static" and cfg.fraction > 0 or cfg.workload == "local-update"
            if dynamic and now >= next_mutation:
                self.step_workload()
                # Keep the requested cadence independent of mutation cost,
                # skipping deadlines already missed instead of catching up.
                missed = max(1, math.floor((perf_counter() - next_mutation) / period) + 1)
                next_mutation += missed * period
            if cfg.full_loop and (submitted := self.diagnostics().scene_submissions) != previous_frame:
                # Native present() returns after the commit acknowledgement.
                # Drawing and VSync happen in the host after that reply. At most
                # one new explicit request follows each completed submission.
                previous_frame = submitted
                self.request_frame()
            if not dynamic and not cfg.full_loop:
                await self._idle_event.wait()  # Idle means no polling timer.
            elif cfg.full_loop:
                delay = max(cfg.redraw or 0, 1 / cfg.fps_limit if cfg.fps_limit else 0)
                await asyncio.sleep(min(delay, max(0, next_mutation - perf_counter())) if dynamic else delay)
            else:
                await asyncio.sleep(max(0, next_mutation - perf_counter()))

    async def _live_metrics(self):
        while True:
            await asyncio.sleep(.25)
            self.sample_metrics()

    def snapshot_report(self):
        """Sample without changing the UI. Native stats requests are explicit overhead."""
        if self._final_report is not None:
            return self._final_report
        now = perf_counter()
        diagnostics = self.diagnostics()
        # Sample detailed native timings once; the basic snapshot does no IPC.
        native = self._native_snapshot()
        measured = self._measurements
        if measured is not None:
            measured.collect(self.drain_frame_timings())
        is_native = native is not None
        return {
            "format": "pysual-stress-1",
            "configuration": asdict(self._configuration),
            "reduce_motion": self.reduce_motion,
            "theme": self._theme_name,
            "host": diagnostics.host_name,
            "host_backend": diagnostics.backend,
            "web_execution": diagnostics.web_execution,
            "terminal_renderer": diagnostics.terminal_renderer,
            "hidden": diagnostics.hidden,
            "viewport": {"width": self.viewport.width, "height": self.viewport.height, "scale": self.viewport.scale},
            "control_types": dict(Counter(type(c).__name__ for c in self._cells)),
            "stress_controls": len(self._cells),
            "scene_build_ms": self._build_ms,
            "native_renderer": native.get("renderer") if native else None,
            "native_pixel_size": native.get("pixel_size") if native else None,
            "grid_rows": sum(len(c.rows) for c in self._cells if isinstance(c, DataGrid)),
            "tree_nodes": sum(3 for c in self._cells if isinstance(c, TreeView)),
            "list_rows": sum(len(c.items) for c in self._cells if isinstance(c, ListView)),
            "chart_points": sum(sum(len(s.points) for s in c.series) for c in self._cells if isinstance(c, LineChart)),
            "chart_slices": sum(len(c.slices) for c in self._cells if isinstance(c, DonutChart)),
            "surface_cache_supported": "render_surfaces" in self.capabilities,
            "cache": asdict(self.cache_stats),
            "resource_errors": list(self.resource_errors),
            "warming_up": measured is None,
            "measurements": measured.summary(now, diagnostics.scene_submissions, native) if measured is not None else None,
            "submission_source": "completed Python-to-native ACK cycles" if is_native else "Python scene submissions (no display acknowledgement)",
            "notes": [
                "No rate measures screen refresh, GPU completion or display scanout.",
                "backend_fps is native presentation frames in this window when the host reports them; otherwise it matches full_cycle_fps. full_cycle_fps is completed Python scene submissions per second, including idle time.",
                "Python layout is separate; paint_and_submit and scene_update include transport and the native commit acknowledgement. Presentation can finish after that acknowledgement.",
                "Live web submissions may have no browser connected; bundled submissions patch the DOM without awaiting paint.",
                "Native frames include cached presentation loops; scene_updates count changed scenes. Console writes do not establish terminal refresh.",
                "Native timing percentiles use only the endpoint's recent ring (up to 120 updates); missing earlier samples are counted. The native mean uses total counter deltas.",
                "Rates cover the whole post-warmup window, including idle time. Python timing percentiles retain the last 10000 collected samples; losses are reported.",
                "Live metrics add four requests/UI updates per second. Sample and Export also add diagnostic work; snapshots precede their UI feedback.",
                "Surface cache budget and body reuse are distinct. Zero surface cache does not disable native retained scenes or textures.",
                "Mutation ticks may coalesce into fewer scene submissions. Dense cells are not necessarily readable, especially in a terminal.",
            ],
        }

    def _show_fps(self, measurements):
        self.fps_readout.text = format_fps_readout(self._configuration.controls, measurements)

    def sample_metrics(self):
        report = self.snapshot_report()
        stats = report["measurements"]
        self._show_fps(stats)
        if stats is None:
            self.metrics.text = "Warming up…"
            return
        p95 = stats["timings_ms"]["scene_update"]["p95"]
        latency = f"{p95:.2f} ms" if p95 is not None else "no samples"
        self.metrics.text = (
            f"Update p95 {latency} · measured {stats['elapsed_seconds']:.2f}s\n"
            f"Ticks {stats['mutation_ticks']} · samples {stats['timing_samples']} · lost {stats['timing_samples_dropped_before_collection']}"
        )

    def StressTest_on_loaded(self, event):
        # Only the stress lab's detailed native timing/resource probes and VSync
        # control need the host adapter. Counts/identity use public diagnostics.
        self._host = self._runtime.host
        self.vsync.enabled = self.diagnostics().backend == "window"
        if self.vsync.enabled and self._configuration.vsync is None:
            self.vsync.checked = bool((self._native_snapshot() or {}).get("vsync", False))
        self.cache_input.enabled = "render_surfaces" in self.capabilities
        self.cache_input.tooltip = "Surface cache is unavailable on this host; body reuse remains independent." if not self.cache_input.enabled else "Python surface-cache budget; distinct from native retained textures."
        self.export_button.enabled = "text_files" in self.capabilities
        self.reset_measurements()

    def StressTest_on_suspended(self, event):
        for task in self._tasks:
            task.cancel()

    def StressTest_on_resumed(self, event):
        self.reset_measurements()

    def StressTest_on_viewport_changed(self, event):
        if self._host is not None:
            self.reset_measurements()

    def StressTest_on_closing(self, event):
        if self._final_report is None:
            self._final_report = self.snapshot_report()

    def _editor_changed(self, event):
        """Apply a user edit. Programmatic editor refresh must not rebuild the scene."""
        if event.origin != "user":
            return
        if event.source is self.preset_choice:
            self.count_input.value = tuple(PRESETS.values())[event.new_value]
        self._commit_editors()

    def _commit_editors(self):
        try:
            config = replace(
                self._configuration, controls=int(self.count_input.value), mix=tuple(MIXES)[self.mix_choice.selected_index],
                workload=WORKLOADS[self.workload_choice.selected_index], fraction=self.fraction_input.value,
                rate=self.rate_input.value, seed=int(self.seed_input.value), fps_limit=int(self.limit_input.value),
                redraw=(None, 0.0, self.interval_input.value)[self.redraw_choice.selected_index],
                cache_mib=self.cache_input.value, body_cache=self.body_cache.checked,
                full_loop=self.full_loop.checked, telemetry=self.telemetry.checked,
                vsync=self.vsync.checked if self.vsync.enabled else None,
            )
        except ValueError as error:
            self.status.text = str(error)
            return
        theme = get_theme(theme_names()[self.theme_choice.selected_index])
        if config == self._configuration and theme == self.theme:
            return
        self.theme = theme
        self.apply_configuration(config)

    def pause_button_on_click(self, event):
        if self._running:
            self._final_report = self.snapshot_report()
            self._running = False
            for task in self._tasks:
                task.cancel()
            self.redraw_interval = None
            self.sample_metrics()
            self.status.text = "Paused. Resume starts a fresh warmup and measurement window."
        else:
            self._running = True
            self.reset_measurements()
        self.pause_button.text = "Pause" if self._running else "Resume"

    def reset_button_on_click(self, event):
        self.apply_configuration(self._configuration)

    def sample_button_on_click(self, event):
        self.sample_metrics()

    async def export_button_on_click(self, event):
        if self._export_busy:
            return
        self._export_busy = True
        self.export_button.enabled = False
        report = self.snapshot_report()
        try:
            result = await self.save_text_file(json.dumps(report, indent=2), suggested_name="pysual-stress.json")
            self.status.text = "Report saved." if result else "Export cancelled."
        except (CapabilityError, OSError, ValueError) as error:
            self.status.text = str(error)
        finally:
            self._export_busy = False
            if self.lifecycle_state != "DESTROYED":
                self.export_button.enabled = "text_files" in self.capabilities


def main(argv=None):
    if argv is not None:
        sys.argv = [sys.argv[0], *argv]
    from pysual import autoconfig  # Explicit CLI/env opt-in, before any UI objects.

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preset", choices=PRESETS, default="large", help="Control-count preset (default: large)")
    parser.add_argument("--all-presets", action="store_true",
                        help="Measure small, medium, large, and extreme; requires a positive --duration")
    parser.add_argument("--controls", type=int, help="Exact stress-control count; overrides preset")
    parser.add_argument("--mix", choices=MIXES, default="balanced")
    parser.add_argument("--workload", choices=WORKLOADS, default="paint")
    parser.add_argument("--fraction", type=float, default=.1, help="Fraction touched per tick; local-update always touches one")
    parser.add_argument("--rate", type=float, default=30, help="Mutation ticks per second, independently of present cap")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--warmup", type=float, default=2)
    parser.add_argument("--duration", type=float, default=0, help="Measured seconds after warmup; 0 waits for window close")
    parser.add_argument("--report", type=Path, help="Write JSON after closing (browser: use Export JSON to download)")
    parser.add_argument("--telemetry", action=argparse.BooleanOptionalAction, default=True,
                        help="Refresh metrics at 4 Hz (default). --no-telemetry leaves them on demand")
    parser.add_argument("--fps-limit", type=int, default=120, help="Presentation cap; 0 means uncapped")
    parser.add_argument("--redraw", default="continuous",
                        help="on-demand, continuous, or interval in seconds (default: continuous)")
    parser.add_argument("--cache-mib", type=float, default=32, help="Python surface-cache budget; 0 disables this cache only")
    parser.add_argument("--no-body-cache", action="store_true", help="Disable reuse of stable control bodies")
    parser.add_argument("--vsync", choices=("on", "off"), default="off",
                        help="Window host only; default off. Actual status is reported")
    parser.add_argument("--full-loop", action="store_true", help="Request Python submissions even for static scenes; requires redraw")
    args = parser.parse_args()
    if args.all_presets and args.controls is not None:
        parser.error("--all-presets cannot be combined with --controls")
    if args.all_presets and args.duration <= 0:
        parser.error("--all-presets requires a positive --duration")
    try:
        config = StressConfig(
            controls=args.controls if args.controls is not None else PRESETS[args.preset],
            mix=args.mix, workload=args.workload, fraction=args.fraction, rate=args.rate, seed=args.seed,
            warmup=args.warmup, duration=args.duration, telemetry=args.telemetry,
            fps_limit=args.fps_limit, redraw=redraw_value(args.redraw), cache_mib=args.cache_mib,
            body_cache=not args.no_body_cache, vsync=args.vsync == "on",
            full_loop=args.full_loop,
        )
    except ValueError as error:
        parser.error(str(error))
    configs = [replace(config, controls=count) for count in PRESETS.values()] if args.all_presets else [config]
    reports = []
    for item in configs:
        app = StressTest(item)
        app.run_blocking()
        report = app.snapshot_report()
        reports.append(report)
        print(format_fps_readout(report["stress_controls"], report["measurements"]), file=sys.stderr)
    payload = reports[0] if len(reports) == 1 else {"format": "pysual-stress-presets-1", "runs": reports}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if args.duration:
        print(json.dumps(payload, indent=2))
    return payload


if __name__ == "__main__":
    main()
