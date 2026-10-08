"""Pure layout algorithms over the control tree; computes shared hit/paint geometry."""

from contextlib import contextmanager
from math import isfinite

from ._engine import ui_method
from .geometry import Rect
from .host import text_row_height
from .schema import _LAYOUT_MASK


@contextmanager
def _measurement_rows(c, host):
    """Expose requested measurement metrics without changing arranged geometry."""
    root = c._root()
    previous = root._measurement_row_height
    root._measurement_row_height = text_row_height(host, 0.0)
    try:
        yield
    finally:
        root._measurement_row_height = previous


def _size(c, host, *, width=None, height=None):
    with _measurement_rows(c, host):
        if c._measure_cache_host is not host:
            c._measure_cache.clear()
            c._measure_cache_host = host
        ancestors = []
        parent = c._parent
        while parent is not None:
            # Local parent changes affect inherited/private sizing context;
            # propagated child dirt must not evict unchanged sibling sizes.
            ancestors.append((parent, parent._measure_local_revision, parent._rect))
            parent = parent._parent
        key = (
            c._measure_revision, width, height, _measure_environment(host),
            c.effective_theme, tuple(ancestors),
            c.__dict__.get("measure", type(c).measure),
            c.__dict__.get("measure_available", type(c).measure_available),
        )
        entries = c._measure_cache
        for previous, preferred in entries if not c._measure_uncached else ():
            if previous == key:
                break
        else:
            preferred = c.measure_available(host, width=width, height=height)
            # Hooks may invalidate during measurement. Do not commit that
            # result under an obsolete generation or after a resource change.
            if (not c._measure_uncached and c._measure_revision == key[0]
                    and _measure_environment(host) == key[3]):
                entries.insert(0, (key, preferred))
                del entries[4:]
        w, h = preferred
        values = c._values
        return c._layout_size(
            _clamp(
                values["width"] if values["width"] is not None else w,
                values["min_width"], values["max_width"],
            ),
            _clamp(
                values["height"] if values["height"] is not None else h,
                values["min_height"], values["max_height"],
            ),
        )


def _measure_environment(host):
    """Host facts that can change preferred sizes without property writes."""
    return (
        getattr(host, "resource_revision", 0), getattr(host, "render_scale", 1),
        text_row_height(host, 0.0), tuple(host.size), getattr(host, "viewport", None),
    )


def _clear_measurements(root):
    """Release host/context references and invalidate placement on host reuse."""
    pending = [root]
    while pending:
        c = pending.pop()
        c._measure_cache.clear()
        c._measure_cache_host = None
        c._measure_revision += 1
        c._layout_valid = False
        c._layout_measure_host = None
        c._layout_measure_environment = None
        pending.extend(getattr(c, "_children", ()))


def _clamp(value, minimum, maximum):
    if maximum is not None and maximum < minimum:
        raise ValueError("Maximum size cannot be smaller than minimum")
    return max(minimum, min(value, maximum if maximum is not None else value))


def _grid_cells(c, normal):
    cells = {}
    occupied = set()
    auto = 0
    parent = c._values
    columns = len(parent["column_tracks"]) or parent["columns"]
    entries = []
    for x in normal:
        child = x._values
        entries.append((
            x, child["grid_row"], child["grid_col"],
            child["grid_row_span"], child["grid_col_span"],
        ))
    for x, r, col, row_span, col_span in sorted(entries, key=lambda item: item[1] < 0):
        if (r < 0) != (col < 0):
            raise ValueError("Grid row and column must be provided together")
        if col_span > columns:
            raise ValueError("Grid span exceeds columns")
        if r < 0:
            while True:
                r, col = divmod(auto, columns)
                auto += 1
                if col + col_span <= columns and all(
                    (a, b) not in occupied
                    for a in range(r, r + row_span)
                    for b in range(col, col + col_span)
                ):
                    break
        slots = {
            (a, b)
            for a in range(r, r + row_span)
            for b in range(col, col + col_span)
        }
        if col + col_span > columns or slots & occupied:
            raise ValueError("Overlapping or out-of-range grid cells")
        occupied.update(slots)
        cells[x] = (r, col)
    return cells


def _dock_order(normal):
    if any(x._values["dock"] == "fill" for x in normal[:-1]):
        raise ValueError("A dock layout accepts at most one fill child, placed last")


def _track(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value >= 0 and isfinite(value):
            return "fixed", float(value)
    elif value == "auto":
        return "auto", 0.0
    elif isinstance(value, str) and value.endswith("*"):
        try:
            weight = float(value[:-1] or "1")
        except ValueError:
            weight = 0
        if weight > 0 and isfinite(weight):
            return "star", weight
    raise ValueError("Grid tracks accept nonnegative pixels, 'auto', '*' or '2*'")


def _distribute(total, constraints):
    """Solve sum(clamp(weight * unit, minimum, maximum)) with explicit overflow."""
    minimum_total = sum(minimum for weight, minimum, maximum in constraints)
    if not isfinite(minimum_total):
        raise ValueError("Combined flex minimums must remain finite")
    if total <= minimum_total:
        return [minimum for weight, minimum, maximum in constraints]
    weights = _normalized_weights([weight for weight, minimum, maximum in constraints])
    constraints = [
        (weight, minimum, maximum)
        for weight, (_, minimum, maximum) in zip(weights, constraints)
    ]
    events = []
    for weight, minimum, maximum in constraints:
        lower = minimum / weight
        upper = maximum / weight if maximum is not None else None
        if not isfinite(lower) or (upper is not None and not isfinite(upper)):
            raise ValueError("Flex constraint ratios exceed the supported finite range")
        events.append((lower, weight))
        if maximum is not None:
            events.append((upper, -weight))
    used, slope, unit = minimum_total, 0.0, 0.0
    for boundary, change in sorted(events):
        projected = used + slope * (boundary - unit)
        if slope > 0 and projected >= total:
            unit += (total - used) / slope
            break
        used, unit = projected, boundary
        slope += change
    else:
        if slope > 0:
            unit += (total - used) / slope
    return [
        _clamp(unit * weight, minimum, maximum)
        for weight, minimum, maximum in constraints
    ]


def _normalized_weights(weights):
    scale = max(weights, default=1.0)
    result = [weight / scale for weight in weights]
    if any(weight == 0 for weight in result):
        raise ValueError("Layout weight ratios exceed the supported finite range")
    return result


def _tracks(declared, count, demands, spacing, available=None):
    specs = [_track(value) for value in declared] + [("star", 1.0)] * (
        count - len(declared)
    )
    sizes = [value if kind == "fixed" else 0.0 for kind, value in specs]
    for start, span, desired in sorted(demands, key=lambda item: item[1]):
        indices = range(start, start + span)
        autos = [i for i in indices if specs[i][0] == "auto"]
        deficit = max(
            0, desired - sum(sizes[i] for i in indices) - spacing * (span - 1)
        )
        if autos:
            for i in autos:
                sizes[i] += deficit / len(autos)
    stars = [i for i, (kind, weight) in enumerate(specs) if kind == "star"]
    if stars:
        weights = _normalized_weights([specs[i][1] for i in stars])
        for i, value in zip(stars, weights):
            specs[i] = "star", value
        weight = sum(specs[i][1] for i in stars)
        if available is not None:
            unit = max(0, available - sum(sizes) - spacing * max(0, count - 1)) / weight
        else:
            unit = 0.0
            for start, span, desired in demands:
                indices = range(start, start + span)
                span_weight = sum(specs[i][1] for i in indices if specs[i][0] == "star")
                if span_weight:
                    unit = max(
                        unit,
                        (
                            desired
                            - sum(sizes[i] for i in indices)
                            - spacing * (span - 1)
                        )
                        / span_weight,
                    )
        for i in stars:
            sizes[i] = unit * specs[i][1]
    return sizes


def _grid_tracks(c, normal, measure, area=None, *, width=None, width_limit=None):
    cells = _grid_cells(c, normal)
    parent = c._values
    column_tracks, row_tracks = parent["column_tracks"], parent["row_tracks"]
    spacing = parent["spacing"]
    columns = len(column_tracks) or parent["columns"]
    rows = max(
        len(row_tracks),
        max((r + x._values["grid_row_span"] for x, (r, col) in cells.items()), default=1),
    )
    # Allocated fixed/star tracks depend only on the available rectangle. Child
    # measurements matter for auto tracks and for the grid's intrinsic size.
    available_width = area.width if area else width
    needs_columns = available_width is None or "auto" in column_tracks
    needs_rows = area is None or "auto" in row_tracks
    sizes = {x: measure(x) for x in normal} if needs_columns else {}
    column_demands = [
        (col, x._values["grid_col_span"], sizes[x][0] + x._values["margin"] * 2)
        for x, (r, col) in cells.items()
    ] if needs_columns else ()
    widths = _tracks(
        column_tracks, columns, column_demands, spacing, available_width,
    )
    if (width_limit is not None
            and sum(widths) + spacing * max(0, columns - 1) > width_limit):
        # A maximum bounds intrinsic content; it does not request that width.
        # Reallocate star tracks only when the natural grid exceeds the bound.
        available_width = width_limit
        widths = _tracks(
            column_tracks, columns, column_demands, spacing, available_width,
        )
    if needs_rows:
        # Row demands depend on the allocated column span, including explicit
        # dimensions and limits that can make a child narrower than its tracks.
        for x, (r, col) in cells.items():
            child = x._values
            child_width = _clamp(
                child["width"] if child["width"] is not None else max(
                    0, sum(widths[col:col + child["grid_col_span"]])
                    + spacing * (child["grid_col_span"] - 1) - child["margin"] * 2
                ), child["min_width"], child["max_width"],
            )
            # Fixed tracks can narrow intrinsic content too. Remeasure only
            # width-sensitive children whose allocated width actually differs.
            if x not in sizes:
                sizes[x] = measure(x, width=child_width)
            elif (x._measure_uses_available()
                  and child_width != sizes[x][0]):
                sizes[x] = measure(x, width=child_width)
    heights = _tracks(
        row_tracks,
        rows,
        [
            (r, x._values["grid_row_span"], sizes[x][1] + x._values["margin"] * 2)
            for x, (r, col) in cells.items()
        ]
        if needs_rows
        else (),
        spacing,
        area.height if area else None,
    )
    return cells, widths, heights


def _flow_boxes(c, normal, area, measure):
    parent = c._values
    vertical = parent["direction"] == "vertical"
    spacing = parent["spacing"]
    available = area.height if vertical else area.width
    main, cross, line_cross = 0.0, 0.0, 0.0
    boxes = {}
    for x in normal:
        w, h = measure(x)
        margin = x._values["margin"]
        length, breadth = (h, w) if vertical else (w, h)
        length += margin * 2
        breadth += margin * 2
        if main and main + length > available:
            main, cross, line_cross = 0.0, cross + line_cross + spacing, 0.0
        left, top = (cross, main) if vertical else (main, cross)
        boxes[x] = Rect(area.x + left + margin, area.y + top + margin, w, h)
        main += length + spacing
        line_cross = max(line_cross, breadth)
    return boxes


def _natural_size(c, measure, *, width=None, height=None):
    normal = [
        x for x in c._children
        if x._values["visible"] and not x._disposed and not x._overlay
    ]
    parent = c._values
    layout = parent["layout"]
    if not normal and not (
        layout == "grid" and (parent["column_tracks"] or parent["row_tracks"])
    ):
        return 100, c._fast_theme().tokens.control_height
    area = Rect(
        0,
        0,
        max(
            0,
            _clamp(
                parent["width"]
                if parent["width"] is not None
                else width
                if width is not None
                else float("inf"),
                parent["min_width"],
                parent["max_width"],
            )
            - parent["padding"] * 2,
        ),
        max(
            0,
            _clamp(
                parent["height"]
                if parent["height"] is not None
                else height
                if height is not None
                else float("inf"),
                parent["min_height"],
                parent["max_height"],
            )
            - parent["padding"] * 2,
        ),
    )
    sizes = {}
    spacing = parent["spacing"]
    if layout == "stack":
        sizes = _stack_sizes(c, normal, area, measure)
    elif layout not in ("grid", "dock"):
        sizes = {x: measure(x) for x in normal}
    gap = spacing * (len(normal) - 1)
    if layout == "stack":
        vertical = parent["direction"] == "vertical"
        main = sum(
            sizes[x][1 if vertical else 0] + x._values["margin"] * 2 for x in normal
        ) + gap
        cross = max(
            sizes[x][0 if vertical else 1] + x._values["margin"] * 2 for x in normal
        )
        w, h = (cross, main) if vertical else (main, cross)
    elif layout == "grid":
        allocated = parent["width"] is not None or width is not None
        cells, widths, heights = _grid_tracks(
            c, normal, measure,
            width=area.width if allocated and isfinite(area.width) else None,
            width_limit=area.width if not allocated and isfinite(area.width) else None,
        )
        w, h = (
            sum(widths) + spacing * (len(widths) - 1),
            sum(heights) + spacing * (len(heights) - 1),
        )
    elif layout == "dock":
        _dock_order(normal)
        sizes = _dock_sizes(c, normal, area, measure, stretch=False)
        w, h = 0.0, 0.0
        for i, x in reversed(list(enumerate(normal))):
            margin = x._values["margin"]
            cw, ch = sizes[x][0] + margin * 2, sizes[x][1] + margin * 2
            space = spacing if i < len(normal) - 1 else 0
            if x._values["dock"] in ("left", "right"):
                w, h = w + cw + space, max(h, ch)
            elif x._values["dock"] in ("top", "bottom"):
                w, h = max(w, cw), h + ch + space
            else:
                w, h = max(w, cw), max(h, ch)
    else:
        boxes = (
            _flow_boxes(c, normal, area, sizes.__getitem__)
            if layout == "flow"
            else {x: Rect(x._values["left"], x._values["top"], *sizes[x]) for x in normal}
        )
        w = max(
            box.right + (x._values["margin"] if layout == "flow" else 0)
            for x, box in boxes.items()
        )
        h = max(
            box.bottom + (x._values["margin"] if layout == "flow" else 0)
            for x, box in boxes.items()
        )
    return max(0, w) + parent["padding"] * 2, max(0, h) + parent["padding"] * 2


def _stack_sizes(c, children, area, measure):
    vertical = c._values["direction"] == "vertical"
    cross = area.width if vertical else area.height
    if not isfinite(cross):
        return {child: measure(child) for child in children}
    axis = "width" if vertical else "height"
    return {
        child: measure(child, **{axis: max(0, cross - child._values["margin"] * 2)})
        for child in children
    }


def _dock_sizes(c, children, area, measure, *, stretch=True):
    """Measure each child at the cross axis left by preceding docked children."""
    remaining_width, remaining_height = area.width, area.height
    spacing = c._values["spacing"]
    sizes = {}
    for i, child in enumerate(children):
        values = child._values
        dock, margin = values["dock"], values["margin"]
        constraints = {}
        if dock in ("top", "bottom", "fill") and isfinite(remaining_width):
            constraints["width"] = _clamp(
                values["width"] if values["width"] is not None else max(
                    0, remaining_width - margin * 2
                ), values["min_width"], values["max_width"],
            )
        if dock in ("left", "right", "fill") and isfinite(remaining_height):
            constraints["height"] = _clamp(
                values["height"] if values["height"] is not None else max(
                    0, remaining_height - margin * 2
                ), values["min_height"], values["max_height"],
            )
        w, h = measure(child, **constraints)
        if stretch:
            w, h = constraints.get("width", w), constraints.get("height", h)
        sizes[child] = w, h
        gap = spacing if i < len(children) - 1 else 0
        if dock in ("left", "right"):
            remaining_width = max(0, remaining_width - w - margin * 2 - gap)
        elif dock in ("top", "bottom"):
            remaining_height = max(0, remaining_height - h - margin * 2 - gap)
    return sizes


@ui_method
def validate(root):
    """Validate structural layout constraints without a host or measurement."""
    children = [
        c for c in getattr(root, "_children", ())
        if c._values["visible"] and not c._disposed
    ]
    layout = root._values.get("layout") if hasattr(root, "_values") else None
    if layout == "grid":
        _grid_cells(root, [c for c in children if not c._overlay])
    elif layout == "dock":
        _dock_order([c for c in children if not c._overlay])
    for child in children:
        validate(child)


def _absolute_box(x, area, measure):
    values = x._values
    w, h = measure(x)
    left, top = values["left"], values["top"]
    anchors = {p.strip() for p in values["anchor"].split(",")}
    if not anchors <= {"left", "right", "top", "bottom"}:
        raise ValueError("Unknown anchor")
    if x._anchor_base is None:
        x._anchor_base = (
            area.width, area.height, left, top, w, h, values["width"], values["height"]
        )
    bw, bh, bl, bt, bwidth, bheight, declared_width, declared_height = x._anchor_base
    dx, dy = area.width - bw, area.height - bh
    if (declared_width, declared_height) != (values["width"], values["height"]):
        # An explicit size replaces the current stretched size on that axis.
        # A single pinned edge instead keeps its original baseline edge.
        if declared_width != values["width"] and {"left", "right"} <= anchors:
            bwidth = w - dx
        if declared_height != values["height"] and {"top", "bottom"} <= anchors:
            bheight = h - dy
        x._anchor_base = (
            bw, bh, bl, bt, bwidth, bheight, values["width"], values["height"]
        )
    if "right" in anchors:
        if "left" in anchors:
            w = _clamp(bwidth + dx, values["min_width"], values["max_width"])
        else:
            left = bl + bwidth + dx - w
    if "bottom" in anchors:
        if "top" in anchors:
            h = _clamp(bheight + dy, values["min_height"], values["max_height"])
        else:
            top = bt + bheight + dy - h
    return Rect(area.x + left, area.y + top, w, h)


def _declared_boxes(c, area, measure):
    """Compute ordinary child boxes before scrolling, using the declared layout."""
    normal = [
        x for x in c._children
        if x._values["visible"] and not x._disposed and not x._overlay
    ]
    boxes = {}
    parent = c._values
    layout = parent["layout"]
    spacing = parent["spacing"]
    if layout == "stack":
        vertical = parent["direction"] == "vertical"
        main = area.height if vertical else area.width
        # Flex allocation and cross-axis stretching replace both measured axes.
        # Intrinsic stack measurement still includes every child's content.
        sizes = _stack_sizes(
            c, [x for x in normal if not x._values["flex"]], area, measure
        )
        fixed = sum(
            (sizes[x][1 if vertical else 0] if not x._values["flex"] else 0)
            + x._values["margin"] * 2
            for x in normal
        )
        free = max(0, main - fixed - spacing * max(0, len(normal) - 1))
        flexible = [x for x in normal if x._values["flex"]]
        allocations = dict(
            zip(
                flexible,
                _distribute(
                    free,
                    [
                        (
                            x._values["flex"],
                            x._values["min_height"] if vertical else x._values["min_width"],
                            x._values["max_height"] if vertical else x._values["max_width"],
                        )
                        for x in flexible
                    ],
                ),
            )
        )
        cursor = 0
        for x in normal:
            child = x._values
            margin = child["margin"]
            length = allocations[x] if child["flex"] else sizes[x][1 if vertical else 0]
            if vertical:
                w = _clamp(
                    child["width"]
                    if child["width"] is not None
                    else max(0, area.width - margin * 2),
                    child["min_width"],
                    child["max_width"],
                )
                h = _clamp(length, child["min_height"], child["max_height"])
                boxes[x] = Rect(area.x + margin, area.y + cursor + margin, w, h)
            else:
                h = _clamp(
                    child["height"]
                    if child["height"] is not None
                    else max(0, area.height - margin * 2),
                    child["min_height"],
                    child["max_height"],
                )
                w = _clamp(length, child["min_width"], child["max_width"])
                boxes[x] = Rect(area.x + cursor + margin, area.y + margin, w, h)
            cursor += (h if vertical else w) + margin * 2 + spacing
    elif layout == "grid":
        cells, widths, heights = _grid_tracks(c, normal, measure, area)
        lefts, tops = [area.x], [area.y]
        for width in widths:
            lefts.append(lefts[-1] + width + spacing)
        for height in heights:
            tops.append(tops[-1] + height + spacing)
        for x, (r, col) in cells.items():
            child = x._values
            margin = child["margin"]
            col_span, row_span = child["grid_col_span"], child["grid_row_span"]
            w = (
                child["width"]
                if child["width"] is not None
                else sum(widths[col : col + col_span])
                + spacing * (col_span - 1)
                - margin * 2
            )
            h = (
                child["height"]
                if child["height"] is not None
                else sum(heights[r : r + row_span])
                + spacing * (row_span - 1)
                - margin * 2
            )
            boxes[x] = Rect(
                lefts[col] + margin,
                tops[r] + margin,
                _clamp(max(0, w), child["min_width"], child["max_width"]),
                _clamp(max(0, h), child["min_height"], child["max_height"]),
            )
    elif layout == "flow":
        boxes = _flow_boxes(c, normal, area, measure)
    elif layout == "dock":
        _dock_order(normal)
        sizes = _dock_sizes(c, normal, area, measure)
        remaining = area
        for i, x in enumerate(normal):
            child = x._values
            margin, dock = child["margin"], child["dock"]
            w, h = sizes[x]
            left = (
                remaining.right - w - margin
                if dock == "right"
                else remaining.x + margin
            )
            top = (
                remaining.bottom - h - margin
                if dock == "bottom"
                else remaining.y + margin
            )
            boxes[x] = Rect(left, top, w, h)
            gap = spacing if i < len(normal) - 1 else 0
            if dock in ("left", "right"):
                consumed = min(remaining.width, w + margin * 2 + gap)
                remaining = Rect(
                    remaining.x + (consumed if dock == "left" else 0),
                    remaining.y,
                    remaining.width - consumed,
                    remaining.height,
                )
            elif dock in ("top", "bottom"):
                consumed = min(remaining.height, h + margin * 2 + gap)
                remaining = Rect(
                    remaining.x,
                    remaining.y + (consumed if dock == "top" else 0),
                    remaining.width,
                    remaining.height - consumed,
                )
    for x in normal:
        if x not in boxes:
            boxes[x] = _absolute_box(x, area, measure)
    return boxes


@ui_method
def arrange(root, host):
    environment = _measure_environment(host)
    if (getattr(root, "_layout_measure_host", None) is not host
            or getattr(root, "_layout_measure_environment", None) != environment):
        _clear_measurements(root)
        root._layout_measure_host = host
        root._layout_measure_environment = environment
    row_height = text_row_height(host, 0.0)
    root._root()._fixed_row_height = row_height
    runtime = getattr(root._root(), "_runtime", None)
    retained = getattr(runtime, "_retained_paint", None)
    router = getattr(runtime, "router", None)
    if retained is not None:
        retained.layout_started()

    def place(c, rect, clip, effect_clip):
        intersection = rect.intersect(clip)
        theme = c._fast_theme()
        if (
            getattr(c, "_layout_valid", False)
            and c._rect == rect
            and c._clip == intersection
            and c._paint_clip == effect_clip
            and c._layout_theme == theme
            and c._layout_row_height == row_height
            and not int(c._dirty) & _LAYOUT_MASK
        ):
            return
        if retained is not None:
            retained.layout_changed(c)
        if router is not None and hasattr(router, "layout_control"):
            router.layout_control(c)
        c._rect, c._clip = rect, intersection
        if c._layout_row_height != row_height:
            c._paint_revision += 1
        c._layout_row_height = row_height
        c._paint_clip = effect_clip
        c._layout_valid, c._layout_theme = True, theme
        c._dirty = 0
        children = [
            x for x in getattr(c, "_children", ())
            if x._values["visible"] and not x._disposed
        ]
        if not hasattr(c, "_children"):
            return
        area = c.content_bounds(rect)
        normal = [x for x in children if not x._overlay]
        custom = c.arrange_children(
            area, lambda child, **limits: _size(child, host, **limits)
        )
        # A custom arranger may settle its scrollbar gutters while measuring.
        # Clip children to that final viewport in the same layout pass.
        children_clip = c.content_bounds(rect).intersect(c._clip)
        children_effect_clip = c._child_effect_clip(rect, children_clip, effect_clip)
        boxes = custom if custom is not None else {}
        if custom is not None:
            if set(custom) != set(normal):
                raise ValueError(
                    "arrange_children must provide every visible non-overlay child"
                )
        else:
            boxes = _declared_boxes(
                c, area, lambda child, **limits: _size(child, host, **limits)
            )
        for x in children:
            if x not in boxes:
                boxes[x] = _absolute_box(x, area, lambda child: _size(child, host))
            b = boxes[x]
            if not x._overlay:
                parent = c._values
                b = Rect(
                    b.x - (parent["scroll_x"] if "scroll_x" in parent else 0),
                    b.y - (parent["scroll_y"] if "scroll_y" in parent else 0),
                    b.width,
                    b.height,
                )
            place(x, b, children_clip, children_effect_clip)

    viewport = Rect(0, 0, *host.size)
    place(root, viewport, viewport, viewport)
    runtime = getattr(root._root(), "_runtime", None)
    router = getattr(runtime, "router", None)
    if router is not None:
        router.layout_changed(observed=True)
