"""Shared synchronous SVG scene construction for native and browser Python.

Transport, browser services and scheduling live in the execution adapters. This
module owns only drawing, bundled-font metrics and portable scene snapshots.
"""

import base64
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from html import escape
from importlib.resources import files
from math import ceil, floor
from pathlib import Path

from ..geometry import Rect
from ..host import Viewport
from ..image_resources import MAX_IMAGE_BYTES, image_source
from . import _font
from ._gradient import gradient_colors


def _node(tag, attrs=None, children=(), text=None):
    result = {"tag": tag, "attrs": {k: str(v) for k, v in (attrs or {}).items()}}
    if children:
        result["children"] = list(children)
    if text is not None:
        result["text"] = str(text)
    return result


def _xml(node):
    attributes = "".join(
        f' {key}="{escape(value, quote=True)}"' for key, value in node["attrs"].items()
    )
    content = escape(node.get("text", "")) + "".join(
        _xml(child) for child in node.get("children", ())
    )
    return f"<{node['tag']}{attributes}>{content}</{node['tag']}>"


def _rect_attrs(rect):
    return {
        "x": rect.x,
        "y": rect.y,
        "width": max(0, rect.width),
        "height": max(0, rect.height),
    }


def _image_sources(node):
    """Collect one immutable paint group's embedded image dependencies."""
    images = {}
    pending = [node]
    while pending:
        current = pending.pop()
        attrs = current["attrs"]
        if current["tag"] == "image" and attrs.get("href", "").startswith("data:image/"):
            images[attrs["data-pysual-image"]] = attrs["href"]
        pending.extend(current.get("children", ()))
    return images


@lru_cache(maxsize=64)
def _gradient_stops(first, last, span):
    """Reuse exact device-aligned stops; published nodes are never mutated."""
    count = min(96, span)
    stops = []
    for index, color in enumerate(gradient_colors(first, last, count)):
        color = "#" + "".join(f"{value:02x}" for value in color)
        for edge in (index, index + 1):
            stops.append(_node("stop", {
                "offset": floor(span * edge / count) / span,
                "stop-color": color,
            }))
    return tuple(stops)


@dataclass(eq=False)
class _Surface:
    serial: int
    rect: Rect
    nodes: list = field(default_factory=list)
    gradients: dict = field(default_factory=dict)
    released: bool = False


class SVGRenderer:
    """A local scene builder; subclasses supply services and frame delivery."""

    _error_prefix = "web"
    backend = "web"

    def __init__(self):
        self.size = (960.0, 640.0)
        self._viewport = Viewport(*self.size)
        self._nodes = []
        self._frame_gradients = {}
        self._frames = OrderedDict()
        self._frame_image_sources = {}
        self._node_image_sources = {}
        self._revision = 0
        self._resource_revision = 0
        self._surface_serial = 0
        self._surfaces = set()
        self._target = None
        self._saved_target = None
        self._clip = None
        self._clip_group = None
        self._serial = 0
        self._paint_group = None
        self._saved_group = None
        self._group_serial = 0
        self._segments = {}
        self._segment_order = []
        self._segment_open = False
        self._segment_id = None
        self._segment_serial = 0
        self._scene_recording = False
        self._saved_recording = None
        self._segment_background = None
        self._background_node = None
        self._background_key = None
        self._images = OrderedDict()
        self._image_serial = 0
        self._image_labels = OrderedDict()
        self._image_bytes = 0
        self._image_errors = set()
        self._title = "Pysual"
        self._text_rect = None
        self._metadata_revision = 0

    def reset_scene(self, title, width, height):
        """Start a fresh opening, invalidating resources from the previous one."""
        viewport = Viewport(width, height)
        self.close_scene()
        self._viewport = viewport
        self.size = (width, height)
        self._title = str(title)
        self._nodes = []
        self._frames.clear()
        self._frame_image_sources.clear()
        self._revision = 0
        self._clip = None
        self._serial = 0
        self._text_rect = None
        self._image_errors.clear()
        self._image_serial = 0
        self._metadata_revision += 1
        self._resource_revision += 1

    def close_scene(self):
        """Release cached resources, retaining the last frame for snapshots."""
        self._target = None
        self._saved_target = None
        self._clip_group = None
        self._paint_group = None
        self._saved_group = None
        self._segments = {}
        self._segment_order = []
        self._segment_open = False
        self._segment_id = None
        self._scene_recording = False
        self._saved_recording = None
        self._segment_background = None
        self._background_node = None
        self._background_key = None
        for surface in self._surfaces:
            surface.released = True
        self._surfaces.clear()
        self._frame_gradients.clear()
        self._node_image_sources.clear()
        self._images.clear()
        self._image_labels.clear()
        self._image_bytes = 0

    @property
    def viewport(self):
        return self._viewport

    @property
    def render_scale(self):
        return self._viewport.scale

    @property
    def resource_revision(self):
        return self._resource_revision

    def set_title(self, title):
        title = str(title)
        if title != self._title:
            self._title = title
            self._metadata_revision += 1

    def text_input(self, rect):
        value = [rect.x, rect.y, rect.width, rect.height] if rect is not None else None
        if value != self._text_rect:
            self._text_rect = value
            self._metadata_revision += 1

    def frame_packet(self, revision):
        """Send changed paint groups and independently retained resources."""
        def split(frame):
            if frame and frame[0]["tag"] == "defs":
                return frame[1:], {
                    node["attrs"]["id"]: node for node in frame[0]["children"]
                }
            return frame, {}

        current, definitions = split(self._frames.get(self._revision, ()))
        previous, old_definitions = split(self._frames.get(revision, ()))
        images = self._frame_image_sources.get(self._revision, {})
        old_images = self._frame_image_sources.get(revision, {})

        def references(node):
            if not images:
                return node
            identifier = node["attrs"].get("data-pysual-image")
            if node["tag"] == "image" and identifier in images:
                return {**node, "image": identifier, "attrs": {
                    key: value for key, value in node["attrs"].items() if key != "href"
                }}
            if "children" in node:
                return {**node, "children": [references(child) for child in node["children"]]}
            return node

        reset = revision not in self._frames
        updates = [
            [index, references(node)]
            for index, node in enumerate(current)
            if reset or index >= len(previous) or node != previous[index]
        ]
        return {
            "revision": self._revision,
            "reset": reset,
            "length": len(current),
            "updates": updates,
            "definitions": [
                references(node) for identity, node in definitions.items()
                if reset or node != old_definitions.get(identity)
            ],
            "remove_definitions": [
                identity for identity in old_definitions if identity not in definitions
            ],
            "image_sources": {
                identity: source for identity, source in images.items()
                if reset or identity not in old_images
            },
            "remove_images": [identity for identity in old_images if identity not in images],
            "width": self.size[0],
            "height": self.size[1],
            "title": self._title,
            "text_input": self._text_rect,
        }

    def report_image_error(self, identifier):
        """Resolve a browser decode failure without exposing an arbitrary path."""
        resource = self._image_labels.get(identifier)
        if resource is not None:
            label, source_key = resource
            self._image_error(f"Browser could not decode {label}", source_key)

    def _image_error(self, detail, source_key):
        message = f"{self._error_prefix} image unavailable: {str(detail)[:400]}"
        key = source_key, message
        if key not in self._image_errors and len(self._image_errors) < 32:
            self._image_errors.add(key)
            self._resource_error(message)

    def reload_image(self, source):
        source = str(source)
        previous = self._images.pop(source, None)
        if previous is not None:
            value, _ = previous
            self._image_bytes -= len(value)
        source_key = hash(source)
        for identifier, (_, key) in tuple(self._image_labels.items()):
            if key == source_key:
                del self._image_labels[identifier]
        self._image_errors = {
            key for key in self._image_errors if key[0] != source_key
        }
        self._resource_revision += 1

    def _resource_error(self, message):
        """Execution adapters enqueue a resource_error input event here."""
        raise NotImplementedError

    def begin_scene(self, background):
        """Start a retained patch. Unchanged controls keep their recorded nodes."""
        if self._target is not None or self._paint_group is not None or self._segment_open:
            raise RuntimeError("Cannot begin a retained scene inside another recording")
        self._scene_recording = True
        self._segment_background = background

    def begin_segment(self, identity, bounds):
        if not self._scene_recording or self._segment_open:
            raise RuntimeError("Invalid retained segment recording state")
        self._segment_serial += 1
        self._segment_open = True
        self._segment_id = str(identity)
        self._saved_recording = (
            self._nodes, self._frame_gradients, self._clip, self._serial,
            self._clip_group, self._paint_group,
        )
        self._nodes = []
        self._frame_gradients = {}
        self._clip = None
        self._clip_group = None
        self._serial = 0
        self._paint_group = None
        self._segment_bounds = bounds

    def end_segment(self):
        if not self._segment_open or self._saved_recording is None:
            raise RuntimeError("No retained segment is being recorded")
        group = _node("g", children=list(self._nodes))
        self._segments[self._segment_id] = (group, dict(self._frame_gradients))
        (
            self._nodes, self._frame_gradients, self._clip, self._serial,
            self._clip_group, self._paint_group,
        ) = self._saved_recording
        self._saved_recording = None
        self._segment_open = False
        self._segment_id = None

    def end_scene(self, order=None, remove=()):
        if not self._scene_recording or self._segment_open:
            raise RuntimeError("Invalid retained scene recording state")
        self._scene_recording = False
        for identity in remove:
            self._segments.pop(str(identity), None)
        if order is not None:
            self._segment_order = [str(identity) for identity in order]
        nodes = []
        gradients = {}
        background = self._segment_background
        background_key = (background, *self.size)
        if self._background_node is None or self._background_key != background_key:
            self._background_node = _node("rect", {
                **_rect_attrs(Rect(0, 0, *self.size)),
                "fill": background or "none",
                "rx": 0,
            })
            self._background_key = background_key
        nodes.append(self._background_node)
        for identity in self._segment_order:
            stored = self._segments.get(identity)
            if stored is None:
                continue
            group, segment_gradients = stored
            nodes.append(group)
            gradients.update(segment_gradients)
        self._nodes = nodes
        self._frame_gradients = gradients
        self._clip = None
        self._clip_group = None
        # Subclasses deliver the published scene. The bare renderer only records it.
        publisher = getattr(self, "present", None)
        if publisher is None:
            self.publish_scene()
        else:
            publisher()

    def begin(self, background):
        if self._target is not None:
            raise RuntimeError("Cannot begin a frame inside a render surface")
        if self._paint_group is not None:
            raise RuntimeError("Cannot begin a frame inside a paint group")
        self._nodes = []
        self._frame_gradients = {}
        self._clip = None
        self._clip_group = None
        self._serial = 0
        self._group_serial = 0
        self.rect(Rect(0, 0, *self.size), background)

    def _paint_group_begin(self):
        """Keep a body's direct, cached and focus drawing in one scene slot."""
        if self._target is not None or self._paint_group is not None:
            raise RuntimeError("Nested paint groups are unsupported")
        self._saved_group = (self._clip, self._serial)
        self._group_serial += 1
        self._serial = 0
        self._clip_group = None
        self._paint_group = _node("g")
        self._paint_group["children"] = []
        self._nodes.append(self._paint_group)

    def _paint_group_end(self):
        if self._target is not None or self._saved_group is None:
            raise RuntimeError("No finished paint group is active")
        self._clip, self._serial = self._saved_group
        self._saved_group = None
        self._paint_group = None
        # A following primitive must not extend a group earlier in paint order.
        self._clip_group = None

    def clip(self, rect):
        if self._target is not None:
            bounds = self._target.rect
            rect = bounds if rect is None else bounds.intersect(rect)
        if rect != self._clip:
            self._clip_group = None
        self._clip = rect

    def _id(self):
        self._serial += 1
        prefix = (
            f"s{self._segment_serial}" if self._segment_open
            else f"p{self._target.serial}" if self._target is not None
            else f"g{self._group_serial}" if self._paint_group is not None
            else "p0"
        )
        return f"{prefix}_{self._serial}"

    def _append(self, node):
        if self._clip is not None:
            if self._clip_group is not None:
                self._clip_group["children"].append(node)
                return
            identity = self._id()
            self._clip_group = _node("g", {"clip-path": f"url(#{identity})"}, [node])
            node = _node(
                "g",
                children=[
                    _node(
                        "defs",
                        children=[
                            _node(
                                "clipPath",
                                {"id": identity},
                                [_node("rect", _rect_attrs(self._clip))],
                            )
                        ],
                    ),
                    self._clip_group,
                ],
            )
        nodes = (
            self._target.nodes if self._target is not None
            else self._paint_group["children"] if self._paint_group is not None
            else self._nodes
        )
        nodes.append(node)

    def rect(self, rect, fill, radius: float = 0, border="", border_width: float = 1):
        if rect.width <= 0 or rect.height <= 0:
            return
        # Host borders occupy the inside of the rectangle, as on desktop.
        stroke = (
            min(max(0, border_width), rect.width / 2, rect.height / 2) if border else 0
        )
        inset = stroke / 2
        shape = Rect(
            rect.x + inset,
            rect.y + inset,
            max(0, rect.width - stroke),
            max(0, rect.height - stroke),
        )
        radius = max(0, min(radius, rect.width / 2, rect.height / 2))
        nodes = []
        if fill or not stroke:
            nodes.append(_node("rect", {
                **_rect_attrs(rect), "fill": fill or "none", "rx": radius,
            }))
        if stroke:
            nodes.append(_node("rect", {
                **_rect_attrs(shape), "fill": "none", "rx": max(0, radius - inset),
                "stroke": border, "stroke-width": stroke,
            }))
        # Keep the full fill underneath translucent inside borders. Grouping
        # preserves one frame primitive and applies the same clip to both nodes.
        self._append(nodes[0] if len(nodes) == 1 else _node("g", children=nodes))

    def gradient_rect(self, rect, first, last, axis, radius, border_width=0):
        if rect.width <= 0 or rect.height <= 0:
            return True
        identity = self._id()
        horizontal = axis == "horizontal"
        scale = self.render_scale
        start = rect.x if horizontal else rect.y
        length = rect.width if horizontal else rect.height
        low, high = floor(start * scale), ceil((start + length) * scale)
        span = high - low
        if span <= 0:
            return True
        # Positions and axes belong to each painted rectangle; device-aligned
        # color bands can be shared across the entire SVG scene. Canonical IDs
        # also let cached surfaces retain their dependencies after LRU eviction.
        shared = f"gradient-{span}-{first[1:]}-{last[1:]}"
        definitions = (
            self._target.gradients if self._target else self._frame_gradients
        )
        if shared not in definitions:
            definitions[shared] = _node(
                "linearGradient", {"id": shared}, _gradient_stops(first, last, span)
            )
        attrs = {
            "id": identity,
            "href": f"#{shared}",
            "gradientUnits": "userSpaceOnUse",
            "x1": low / scale if horizontal else 0,
            "y1": 0 if horizontal else low / scale,
            "x2": high / scale if horizontal else 0,
            "y2": 0 if horizontal else high / scale,
        }
        gradient = _node("linearGradient", attrs)
        stroke = min(max(0, border_width), rect.width / 2, rect.height / 2)
        shape = Rect(
            rect.x + stroke / 2,
            rect.y + stroke / 2,
            rect.width - stroke,
            rect.height - stroke,
        )
        paint = (
            {"fill": "none", "stroke": f"url(#{identity})", "stroke-width": stroke}
            if stroke
            else {"fill": f"url(#{identity})"}
        )
        self._append(
            _node(
                "g",
                children=[
                    _node("defs", children=[gradient]),
                    _node(
                        "rect",
                        {
                            **_rect_attrs(shape),
                            "rx": max(0, radius - stroke / 2),
                            **paint,
                        },
                    ),
                ],
            )
        )
        return True

    def text(self, text, x, y, color, size, mono=False):
        advance = _font.measure("", size, mono)[1] if "\n" in text else 0
        for index, line in enumerate(text.split("\n")):
            if not line:
                continue
            self._append(
                _node(
                    "text",
                    {
                        "x": x,
                        "y": y + index * advance + _font.ascent(size, mono),
                        "fill": color,
                        "font-size": size,
                        "font-family": "Pysual Mono" if mono else "Pysual Sans",
                        "xml:space": "preserve",
                        "style": "white-space:pre;font-variant-ligatures:none;font-kerning:normal",
                    },
                    text=line,
                )
            )

    def measure(self, text, size, mono=False):
        return _font.measure(text, size, mono)

    def line(self, x1, y1, x2, y2, color, width: float = 1):
        self._append(
            _node(
                "line",
                {
                    "x1": x1,
                    "y1": y1,
                    "x2": x2,
                    "y2": y2,
                    "stroke": color,
                    "stroke-width": width,
                },
            )
        )

    def _image_source(self, source):
        source = str(source)
        if source in self._images:
            self._images.move_to_end(source)
            return self._images[source]
        value = self._resolve_image_source(source)
        while self._images and (
            len(self._images) >= 128
            or self._image_bytes + len(value) > 44 * 1024 * 1024
        ):
            _, evicted = self._images.popitem(last=False)
            self._image_bytes -= len(evicted[0])
        self._image_serial += 1
        identifier = self._image_serial
        label = (
            f"embedded image {identifier}"
            if source.startswith("data:")
            else source[:300]
        )
        self._image_labels[identifier] = label, hash(source)
        while len(self._image_labels) > 256:
            self._image_labels.popitem(last=False)
        self._images[source] = (value, identifier)
        self._image_bytes += len(value)
        return value, identifier

    def _resolve_image_source(self, source):
        """Embed local assets; browser adapters may additionally resolve URLs."""
        if source.startswith("data:image/"):
            header, encoded = source.split(",", 1)
            if header not in {
                "data:image/png;base64",
                "data:image/jpeg;base64",
                "data:image/gif;base64",
                "data:image/webp;base64",
                "data:image/svg+xml;base64",
            }:
                raise ValueError("Images must be PNG, JPEG, GIF, WebP or SVG data")
            limit = (
                MAX_IMAGE_BYTES
                if header == "data:image/png;base64"
                else 32 * 1024 * 1024
            )
            if len(encoded) > ((limit + 2) // 3) * 4:
                raise ValueError(f"Image exceeds {limit // (1024 * 1024)} MiB")
            data = base64.b64decode(encoded, validate=True)
            value = image_source(data) if header == "data:image/png;base64" else source
        else:
            path = Path(source)
            with path.open("rb") as stream:
                data = stream.read(12)
                limit = (
                    MAX_IMAGE_BYTES
                    if data.startswith(b"\x89PNG\r\n\x1a\n")
                    else 32 * 1024 * 1024
                )
                data += stream.read(limit - len(data) + 1)
            if len(data) > limit:
                raise ValueError(f"Image exceeds {limit // (1024 * 1024)} MiB")
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                mime = "png"
            elif data.startswith(b"\xff\xd8\xff"):
                mime = "jpeg"
            elif data.startswith((b"GIF87a", b"GIF89a")):
                mime = "gif"
            elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
                mime = "webp"
            elif path.suffix.lower() == ".svg":
                # Keep SVG in an image resource, never insert its markup into
                # the page. The browser validates and decodes its contents.
                mime = "svg+xml"
            else:
                raise ValueError("Web images must be PNG, JPEG, GIF, WebP or SVG")
            # Use the portable PNG API's encoded/header/decoded bounds without
            # decoding the pixels twice. The browser reports decode failures.
            value = (
                image_source(data)
                if mime == "png"
                else "data:image/" + mime + ";base64," + base64.b64encode(data).decode("ascii")
            )
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("Image exceeds 32 MiB")
        return value

    def image(self, source, rect, *, fit="stretch"):
        aspects = {"stretch": "none", "contain": "xMidYMid meet", "cover": "xMidYMid slice"}
        if fit not in aspects:
            raise ValueError("Image fit must be stretch, contain or cover")
        try:
            value, identifier = self._image_source(source)
        except (OSError, ValueError) as exc:
            self._image_error(exc, hash(str(source)))
            return
        self._append(
            _node(
                "image",
                {
                    **_rect_attrs(rect),
                    "href": value,
                    "preserveAspectRatio": aspects[fit],
                    "data-pysual-image": identifier,
                },
            )
        )

    def surface_create(self, rect):
        if len(self._surfaces) >= 8192:
            raise MemoryError("Too many web render surfaces")
        self._surface_serial += 1
        surface = _Surface(self._surface_serial, rect)
        self._surfaces.add(surface)
        return surface

    def _check_surface(self, surface):
        if surface not in self._surfaces or surface.released:
            raise ValueError("Unknown or released web render surface")

    def surface_begin(self, surface):
        self._check_surface(surface)
        if self._target is not None:
            raise RuntimeError("Nested render surfaces are unsupported")
        self._saved_target = (self._clip, self._serial, self._clip_group)
        self._target = surface
        surface.nodes = []
        surface.gradients = {}
        self._clip = surface.rect
        self._clip_group = None
        self._serial = 0

    def surface_end(self):
        if self._target is None or self._saved_target is None:
            raise RuntimeError("No web surface is active")
        self._target = None
        self._clip, self._serial, self._clip_group = self._saved_target
        self._saved_target = None

    def surface_blit(self, surface):
        self._check_surface(surface)
        definitions = (
            self._target.gradients if self._target else self._frame_gradients
        )
        definitions.update(surface.gradients)
        # Keep the same coordinate system and single clip as direct drawing.
        # Nested SVG viewports snap fractional bounds differently in Firefox.
        self._append(_node("g", children=surface.nodes))

    def surface_release(self, surface):
        self._check_surface(surface)
        if self._target is surface:
            raise RuntimeError("Cannot release an active render surface")
        self._surfaces.remove(surface)
        surface.released = True
        surface.nodes = []
        surface.gradients = {}

    def _scene_nodes(self):
        if self._frame_gradients:
            return (_node("defs", children=self._frame_gradients.values()), *self._nodes)
        return tuple(self._nodes)

    def publish_scene(self):
        """Publish an immutable scene and return whether its contents changed."""
        if self._target is not None:
            raise RuntimeError("Finish the render surface before presenting")
        if self._paint_group is not None:
            raise RuntimeError("Finish the paint group before presenting")
        # Never extend a clip group referenced by a published frame.
        self._clip_group = None
        frame = self._scene_nodes()
        if frame == self._frames.get(self._revision):
            return False
        self._revision += 1
        self._frames[self._revision] = frame
        # Keep image data out of geometry-only transport deltas. The original
        # nodes retain their hrefs for self-contained SVG and HTML snapshots.
        images, resources = {}, {}
        for node in self._nodes:
            identity = id(node)
            cached = self._node_image_sources.get(identity)
            if cached is None or cached[0] is not node:
                cached = (node, _image_sources(node))
            resources[identity] = cached
            images.update(cached[1])
        # Only retain metadata for this frame's top-level paint groups. Holding
        # the node alongside its ID prevents object-ID reuse from aliasing a
        # replacement group. Published groups and their descendants are immutable,
        # so a local hover need not rescan thousands of clean controls. Gradient
        # definitions contain no images and need no dependency traversal.
        self._node_image_sources = resources
        self._frame_image_sources[self._revision] = images
        while len(self._frames) > 4:
            expired, _ = self._frames.popitem(last=False)
            self._frame_image_sources.pop(expired)
        return True

    def _font_css(self):
        return "".join(
            f"@font-face{{font-family:'Pysual {'Mono' if mono else 'Sans'}';src:url(data:font/ttf;base64,{base64.b64encode(_font.font_bytes(mono)).decode('ascii')}) format('truetype');}}"
            for mono in (False, True)
        )

    def export_svg(self, path=None):
        """Return/write a portable SVG of the last presented frame, with fonts."""
        nodes = self._frames.get(self._revision)
        if nodes is None:
            nodes = self._scene_nodes()
        width, height = self.size
        title = self._title
        license_text = (
            files("pysual")
            .joinpath("assets/DejaVu-LICENSE.txt")
            .read_text(encoding="utf-8")
        )
        content = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img"><title>{escape(title)}</title><metadata>{escape(license_text)}</metadata><defs><style>{self._font_css()}</style></defs>'
            + "".join(map(_xml, nodes))
            + "</svg>"
        )
        if path is not None:
            Path(path).write_text(content, encoding="utf-8")
        return content

    def export_html(self, path=None):
        """Return/write a self-contained snapshot; interaction needs a running application."""
        svg = self.export_svg()
        title = self._title
        content = (
            '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'
            + escape(title)
            + "</title><style>html,body{margin:0;background:#111}main>svg{display:block;width:100%;height:auto}footer{padding:8px 16px;background:#161a22;color:#b9c2d1;font:12px system-ui}</style><main>"
            + svg
            + "</main><footer>Saved Pysual frame · Live interaction requires the Python application.</footer></html>"
        )
        if path is not None:
            Path(path).write_text(content, encoding="utf-8")
        return content
