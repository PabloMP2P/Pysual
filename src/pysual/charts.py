"""Bounded charts; data preparation belongs to the controls, not the host."""

from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from math import ceil, cos, fsum, isfinite, pi, sin
from typing import ClassVar, Literal

from ._text_display import single_line
from .controls import Control
from .geometry import Rect
from .schema import Dirty, _persist_record, prop

_PALETTE = ("#38bdf8", "#34d399", "#fbbf24", "#f472b6", "#a78bfa", "#fb923c", "#94a3b8")


def _chart_identity(key, title, color):
    if not isinstance(key, str) or not key or not isinstance(title, str):
        raise ValueError("Chart data needs a nonempty key and title string")
    if not isinstance(color, str) or (
        color
        and (
            len(color) != 7
            or color[0] != "#"
            or any(c not in "0123456789abcdefABCDEF" for c in color[1:])
        )
    ):
        raise ValueError("Chart colors must be empty or #RRGGBB")


@_persist_record
@dataclass(frozen=True)
class ChartSeries:
    key: str
    title: str
    points: tuple[tuple[float, float], ...]
    color: str = ""

    def __post_init__(self):
        _chart_identity(self.key, self.title, self.color)
        if not isinstance(self.points, tuple) or any(
            not isinstance(p, tuple)
            or len(p) != 2
            or any(type(v) not in (int, float) or not isfinite(v) for v in p)
            for p in self.points
        ):
            raise TypeError(
                "Chart points must be a tuple of finite numeric (x, y) pairs"
            )
        if len(self.points) > 10_000:
            raise ValueError("A chart series supports at most 10,000 points")
        if any(b[0] < a[0] for a, b in zip(self.points, self.points[1:])):
            raise ValueError("Chart points must be ordered by increasing x")


def _reduce(points, buckets):
    """Retain each bucket's endpoints and y extrema in their original order."""
    if len(points) <= buckets * 4:
        return points
    result: list[tuple[float, float]] = []
    for i in range(buckets):
        start, end = i * len(points) // buckets, (i + 1) * len(points) // buckets
        indices = range(start, end)
        chosen = sorted(
            {
                start,
                end - 1,
                min(indices, key=lambda j: points[j][1]),
                max(indices, key=lambda j: points[j][1]),
            }
        )
        result.extend(points[j] for j in chosen)
    return tuple(result)


def _axis_labels(values, available, measure):
    """Format a small tick set together, retaining its visible differences."""
    numbers = tuple(Decimal(str(value)) for value in values)

    def labels_for(ticks):
        gaps = [abs(b - a) for a, b in zip(ticks, ticks[1:]) if a != b]
        largest = max(map(abs, ticks))
        precision = max(4, largest.adjusted() - min(gaps).adjusted() + 2) if gaps else 4
        for digits in range(min(17, precision), 18):
            labels = tuple(format(float(value), f".{digits}g") for value in ticks)
            if len(set(labels)) == len(set(ticks)):
                return labels
        return labels

    labels = labels_for(numbers)
    if max(map(measure, labels)) <= available:
        return labels, ""
    span = max(numbers) - min(numbers)
    largest = max(map(abs, numbers))
    # Close values with a large shared base (such as timestamps) benefit from
    # an explicit additive offset; wide ranges instead share an exponent.
    if span and largest > span * 100:
        shifted = labels_for(tuple(value - numbers[0] for value in numbers))
        if max(map(measure, shifted)) <= available:
            offset = format(float(numbers[0]), ".17g")
            return shifted, ("+" if numbers[0] >= 0 else "") + offset
    exponent = largest.adjusted() if largest else 0
    if exponent:
        scale = Decimal(10) ** exponent
        return labels_for(tuple(value / scale for value in numbers)), f"×1e{exponent}"
    return labels, ""


class LineChart(Control):
    cache_paint: bool = prop(default=True)
    series: tuple[ChartSeries, ...] = prop(default=())
    title: str = prop(default="", affects=Dirty.MEASURE)
    show_legend: bool = prop(default=True, affects=Dirty.MEASURE)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "axis", "legend", "line")

    def _initialize(self):
        super()._initialize()
        self._rebuild()

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "series":
            if len(value) > 8 or sum(len(s.points) for s in value) > 20_000:
                raise ValueError(
                    "LineChart supports at most eight series and 20,000 total points"
                )
            if len({s.key for s in value}) != len(value):
                raise ValueError("Chart series keys must be unique")

    def _changed(self, field, old, value):
        if field.name == "series":
            self._rebuild()
        super()._changed(field, old, value)

    def _rebuild(self):
        # At most 2048 vertices across the chart; recompute only on data changes.
        buckets = max(1, 512 // max(1, len(self.series)))
        self._points = tuple(_reduce(s.points, buckets) for s in self.series)
        values = [p for s in self.series for p in s.points]
        self._extent = (
            (
                min(p[0] for p in values),
                max(p[0] for p in values),
                min(p[1] for p in values),
                max(p[1] for p in values),
            )
            if values
            else (0, 1, 0, 1)
        )

    def measure(self, host):
        return 560, 300

    def paint(self, p, /):
        p.body()
        if self.title:
            p.text(p.elide(single_line(self.title), max(0, p.width - 24)), 12, 10)
        palette = (p.theme.tokens.accent, *_PALETTE)
        if self.show_legend and p.height - 19 >= (38 if self.title else 0):
            legend = p.style("legend")
            slot = max(0, p.width - 24) / max(1, len(self.series))
            for i, series in enumerate(self.series):
                x, y = 12 + i * slot, p.height - 19
                p.line(x, y + 6, x + 14, y + 6, series.color or palette[i], 2)
                p.text(
                    p.elide(single_line(series.title), max(0, slot - 26),
                            size=11, font_family=legend.font_family),
                    x + 20, y, size=11, color=legend.foreground,
                    font_family=legend.font_family,
                )
        axis = p.style("axis")
        xmin, xmax, ymin, ymax = self._extent
        measure = lambda text: p.measure(text, size=11, font_family=axis.font_family)[0]
        yvalues = tuple(ymin * (1 - i / 4) + ymax * (i / 4) for i in range(5))
        ylabels, ynote = _axis_labels(yvalues, max(56, min(120, p.width / 5)), measure)
        left = max(66, max(map(measure, ylabels)) + 14)
        xvalues = tuple(xmin * (1 - ratio) + xmax * ratio for ratio in (0, 0.5, 1))
        xlabels, xnote = _axis_labels(xvalues, max(32, (p.width - left - 16) / 3 - 8), measure)
        top = 38 if self.title else 16
        bottom = 50 if self.show_legend and self.series else 30
        top += 16 if ynote else 0
        bottom += 16 if xnote else 0
        plot = Rect(left, top, max(0, p.width - left - 16), max(0, p.height - top - bottom))
        self._painted_segments = 0
        if plot.width <= 0 or plot.height <= 0:
            return

        # Halving before subtraction avoids overflow for finite extreme ranges.
        def fraction(value, low, high):
            if low == high:
                return 0.5
            span = high - low
            return (
                (value - low) / span
                if isfinite(span)
                else (value / 2 - low / 2) / (high / 2 - low / 2)
            )

        for i, text in enumerate(ylabels):
            ratio = i / 4
            y = plot.bottom - plot.height * ratio
            p.line(plot.x, y, plot.right, y, axis.border)
            p.text(
                text,
                4,
                y - 7,
                color=axis.foreground,
                size=11,
                font_family=axis.font_family,
            )
        p.line(plot.x, plot.y, plot.x, plot.bottom, axis.foreground)
        for ratio, text in zip((0, 0.5, 1), xlabels):
            width = measure(text)
            x = max(
                plot.x, min(plot.right - width, plot.x + plot.width * ratio - width / 2)
            )
            p.text(
                text,
                x,
                plot.bottom + 5,
                size=11,
                color=axis.foreground,
                font_family=axis.font_family,
            )
        for note, x, y in ((ynote, 4, plot.y - 16),
                           (xnote, plot.right - measure(xnote), plot.bottom + 20)):
            if note:
                p.text(note, x, y, color=axis.foreground, size=11,
                       font_family=axis.font_family)
        line_width = max(1, p.style("line").border_width)
        for i, (series, points) in enumerate(zip(self.series, self._points)):
            color = series.color or palette[i]
            mapped = [
                (
                    plot.x + plot.width * fraction(x, xmin, xmax),
                    plot.bottom - plot.height * fraction(y, ymin, ymax),
                )
                for x, y in points
            ]
            # One opaque path per series lets supporting hosts batch native
            # strokes. Painter retains segment semantics on every other host.
            p.lines(mapped, color, line_width)
            self._painted_segments += max(0, len(mapped) - 1)
            if len(mapped) == 1:
                x, y = mapped[0]
                p.rect(Rect(x - 2, y - 2, 4, 4), color, radius=2)
        if not any(self._points):
            p.text(
                "No data",
                plot.x + 12,
                plot.y + 12,
                color=axis.foreground,
                font_family=axis.font_family,
            )




@_persist_record
@dataclass(frozen=True)
class ChartSlice:
    key: str
    title: str
    value: float
    color: str = ""

    def __post_init__(self):
        _chart_identity(self.key, self.title, self.color)
        if (
            type(self.value) not in (int, float)
            or not isfinite(self.value)
            or self.value < 0
        ):
            raise ValueError("ChartSlice.value must be a finite nonnegative number")


class DonutChart(Control):
    """At most eight immutable slices, drawn using bounded Painter line segments."""

    cache_paint: bool = prop(default=True)
    slices: tuple[ChartSlice, ...] = prop(default=())
    title: str = prop(default="", affects=Dirty.MEASURE)
    show_legend: bool = prop(default=True, affects=Dirty.MEASURE)
    center_mode: Literal["percent", "total", "none"] = prop(default="percent")
    center_text: str | None = prop(default=None)
    style_parts: ClassVar[tuple[str, ...]] = ("body", "legend")

    def _initialize(self):
        super()._initialize()
        self._rebuild()

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "slices":
            if len(value) > 8:
                raise ValueError("DonutChart supports at most eight slices")
            if len({s.key for s in value}) != len(value):
                raise ValueError("Chart slice keys must be unique")

    def _changed(self, field, old, value):
        if field.name == "slices":
            self._rebuild()
        super()._changed(field, old, value)

    def _rebuild(self):
        # Normalize before adding to avoid overflow from finite extreme values.
        maximum = max((s.value for s in self.slices), default=0)
        scaled = tuple(s.value / maximum if maximum else 0 for s in self.slices)
        total = fsum(scaled)
        self._fractions = tuple(value / total if total else 0 for value in scaled)
        # Decimal keeps a finite display even when the sum exceeds float range.
        # It does not participate in the normalized ring geometry.
        with localcontext(Context(prec=28)):
            self._total_text = format(
                sum((Decimal(str(s.value)) for s in self.slices), Decimal(0)), ".4g"
            )
        self._arcs = []
        start = -pi / 2
        for fraction in self._fractions:
            count = ceil(fraction * 512)
            end = start + 2 * pi * fraction
            self._arcs.append(
                tuple(
                    (
                        cos(start + (end - start) * (i + 0.5) / count),
                        sin(start + (end - start) * (i + 0.5) / count),
                    )
                    for i in range(count)
                )
                if count
                else ()
            )
            start = end

    def measure(self, host):
        return 360, 340

    def paint(self, p, /):
        p.body()
        if self.title:
            p.text(p.elide(single_line(self.title), max(0, p.width - 24)), 12, 10)
        top = 38 if self.title else 12
        legend_rows = ceil(len(self.slices) / 2) if self.show_legend else 0
        bottom = 12 + legend_rows * 22
        radius = min(max(0, p.width - 24), max(0, p.height - top - bottom)) / 2
        center_x, center_y = p.width / 2, top + radius
        self._painted_segments = 0
        palette = (p.theme.tokens.accent, *_PALETTE)
        legend = p.style("legend")
        slot = max(0, p.width - 24) / 2
        if self.show_legend:
            slot = min(slot, max((
                p.measure(single_line(slice_.title), size=11,
                          font_family=legend.font_family)[0]
                + p.measure(f"{fraction:.0%}", size=11,
                            font_family=legend.font_family)[0] + 34
                for slice_, fraction in zip(self.slices, self._fractions)
            ), default=0))
        # Narrow radial spans keep rounded ring edges consistent on hosts whose
        # wide line strokes are axis-aligned rather than perpendicular to a path.
        thickness = max(1, ceil(2 * pi * radius / 512) + 1)
        for index, (slice_, arc) in enumerate(zip(self.slices, self._arcs)):
            color = slice_.color or palette[index]
            if radius > 0:
                p.segments(
                    ((
                        center_x + x * radius * 0.56,
                        center_y + y * radius * 0.56,
                        center_x + x * radius,
                        center_y + y * radius,
                    ) for x, y in arc), color, thickness,
                )
                self._painted_segments += len(arc)
            if self.show_legend:
                x = 12 + (index % 2) * slot
                y = p.height - bottom + (index // 2) * 22
                if y >= top:
                    p.rect(Rect(x, y + 4, 10, 10), color, radius=2)
                    available = max(0, slot - 22)
                    percentage = p.elide(
                        f"{self._fractions[index]:.0%}", available,
                        size=11, font_family=legend.font_family,
                    )
                    value_width = p.measure(
                        percentage, size=11, font_family=legend.font_family,
                    )[0]
                    title = p.elide(
                        single_line(slice_.title), max(0, available - value_width - 6),
                        size=11, font_family=legend.font_family,
                    )
                    title_width = p.measure(
                        title, size=11, font_family=legend.font_family,
                    )[0]
                    p.text(
                        title,
                        x + 16,
                        y + 2,
                        size=11,
                        color=legend.foreground,
                        font_family=legend.font_family,
                    )
                    p.text(
                        percentage, x + 16 + min(title_width + 6, available - value_width), y + 2,
                        size=11, color=legend.foreground,
                        font_family=legend.font_family,
                    )
        if radius > 0:
            text = self.center_text
            if text is None:
                text = (
                    ""
                    if self.center_mode == "none"
                    else self._total_text
                    if self.center_mode == "total"
                    else "100%"
                    if any(self._fractions)
                    else "No data"
                )
            if not text:
                return
            width, height = p.measure(text, size=14)
            if width <= radius:
                p.text(text, center_x - width / 2, center_y - height / 2, size=14)
