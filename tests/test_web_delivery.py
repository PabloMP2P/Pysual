"""Delivered scenes remain usable when rendering outruns a live client."""

import http.client
import json
import re
import threading
from unittest.mock import patch
from urllib.parse import urlsplit

from pysual._png import encode_png
from pysual.backends._web_native import LiveSVGHost, _FrameCursor, _Handler, _MAX_CONNECTIONS
from pysual.backends._web_svg import SVGRenderer
from pysual.geometry import Rect
from pysual.image_resources import image_source


def draw(scene, value):
    scene.begin("#ffffff")
    scene.rect(Rect(0, 0, 10, 10), f"#{value:06x}")
    scene.rect(Rect(20, 0, 10, 10), "#123456")
    scene.publish_scene()


def host():
    result = LiveSVGHost(open_browser=False)
    result.reset_scene("Delivery", 100, 100)
    result._closed = False
    return result


def test_stream_base_survives_arbitrarily_many_unobserved_publications():
    scene = host()
    cursor = _FrameCursor()
    draw(scene, 1)
    first = scene._frame_packet(-1, cursor=cursor)
    saved = json.dumps(cursor.snapshot.nodes)
    for value in range(2, 202):
        draw(scene, value)
    assert first["revision"] not in scene._frames
    assert json.dumps(cursor.snapshot.nodes) == saved
    packet = scene._frame_packet(first["revision"], cursor=cursor)
    assert packet["reset"] is False
    assert [index for index, _ in packet["updates"]] == [1]
    assert len(scene._frames) == 4
    assert not scene._delivered_frames
    assert cursor.snapshot.revision == packet["revision"]


def test_long_poll_retains_delivered_bases_instead_of_unseen_revisions():
    scene = host()
    draw(scene, 1)
    first = scene._frame_packet(-1)
    for value in range(2, 102):
        draw(scene, value)
    packet = scene._frame_packet(first["revision"])
    assert packet["reset"] is False
    assert len(packet["updates"]) == 1
    for value in range(102, 202):
        draw(scene, value)
        packet = scene._frame_packet(packet["revision"])
    assert len(scene._delivered_frames) == _MAX_CONNECTIONS
    assert len(scene._frames) == 4
    # A client older than both bounded histories still gets a complete recovery.
    assert scene._frame_packet(first["revision"])["reset"] is True
    scene.close()
    assert not scene._delivered_frames


def test_retained_base_tracks_definition_and_image_removal():
    scene = SVGRenderer()
    scene.reset_scene("Resources", 100, 100)
    # Published scene nodes are immutable. Minimal image nodes exercise the
    # resource protocol without depending on an external file or image decoder.
    image = {"tag": "image", "attrs": {
        "href": "data:image/png;base64,AA==", "data-pysual-image": "7",
    }}
    scene.begin("#ffffff")
    scene._nodes.append(image)
    scene.gradient_rect(Rect(0, 0, 10, 10), "#000000", "#ffffff", "vertical", 0)
    scene.publish_scene()
    base = scene._frame_snapshot()
    for value in range(10):
        draw(scene, value)
    packet = scene.frame_packet(base.revision, _base=base)
    assert packet["reset"] is False
    assert packet["remove_images"] == ["7"]
    assert packet["remove_definitions"]
    assert packet["image_sources"] == {}
    assert base.images == {"7": "data:image/png;base64,AA=="}


def test_snapshot_from_previous_opening_cannot_alias_reused_revision():
    scene = SVGRenderer()
    draw(scene, 1)
    base = scene._frame_snapshot()
    scene.reset_scene("New opening", 100, 100)
    draw(scene, 2)
    assert scene._revision == base.revision
    assert scene.frame_packet(base.revision, _base=base) == scene.frame_packet(-1)
    for value in range(3, 8):
        draw(scene, value)
    packet = scene.frame_packet(base.revision, _base=base)
    assert packet["reset"] is True
    assert len(packet["updates"]) == 3


def test_mismatched_revision_in_same_epoch_uses_available_history():
    scene = SVGRenderer()
    draw(scene, 1)
    requested = scene._revision
    draw(scene, 2)
    other_base = scene._frame_snapshot()
    draw(scene, 3)
    packet = scene.frame_packet(requested, _base=other_base)
    assert packet == scene.frame_packet(requested)
    assert packet["reset"] is False


def test_shared_groups_skip_comparison_without_losing_equal_or_changed_groups():
    class CountedNode(dict):
        comparisons = 0

        def __ne__(self, other):
            self.comparisons += 1
            return super().__ne__(other)

    def node(name, fill):
        return CountedNode(tag="rect", attrs={"id": name, "fill": fill})

    scene = SVGRenderer()
    shared = node("shared", "red")
    scene.begin("#ffffff")
    scene._nodes.extend((shared, node("equal", "blue"), node("changed", "green")))
    scene.publish_scene()
    base = scene._frame_snapshot()

    equal, changed = node("equal", "blue"), node("changed", "black")
    scene.begin("#ffffff")
    scene._nodes.extend((shared, equal, changed))
    scene.publish_scene()
    # Reset still includes every group, including a shared immutable node.
    expected = scene.frame_packet(-1)
    assert [index for index, _ in expected["updates"]] == [0, 1, 2, 3]
    expected.update(reset=False, updates=[[3, dict(changed)]])
    expected_json = json.dumps(expected, separators=(",", ":"))
    for retained in (None, base):
        shared.comparisons = equal.comparisons = changed.comparisons = 0
        packet = scene.frame_packet(base.revision, _base=retained)
        assert json.dumps(packet, separators=(",", ":")) == expected_json
        assert shared.comparisons == 0
        assert equal.comparisons == changed.comparisons == 1


def test_each_stream_observes_metadata_independently():
    scene = host()
    draw(scene, 1)
    first, second = _FrameCursor(), _FrameCursor()
    packet = scene._frame_packet(-1, cursor=first)
    scene._frame_packet(-1, cursor=second)
    scene.set_title("Changed")
    assert scene._frame_packet(packet["revision"], cursor=first)["title"] == "Changed"
    result = []
    worker = threading.Thread(
        target=lambda: result.append(scene._frame_packet(packet["revision"], cursor=second)),
        daemon=True,
    )
    worker.start()
    worker.join(1)
    try:
        assert not worker.is_alive(), "Another reader consumed this stream's metadata wakeup"
        assert result[0]["title"] == "Changed"
        assert result[0]["updates"] == []
    finally:
        scene.close()
        worker.join(1)


class _BrowserScene:
    """Apply the wire protocol independently of the server's snapshot storage."""

    def __init__(self):
        self.nodes = []
        self.definitions = {}
        self.images = {}

    def apply(self, packet):
        # Network serialization must sever every reference to renderer objects.
        packet = json.loads(json.dumps(packet))
        if packet["reset"]:
            self.images.clear()
            self.definitions.clear()
        self.images.update(packet["image_sources"])
        for node in packet["definitions"]:
            self.definitions[node["attrs"]["id"]] = node
        self.nodes.extend([None] * max(0, packet["length"] - len(self.nodes)))
        for index, node in packet["updates"]:
            self.nodes[index] = node
        del self.nodes[packet["length"]:]
        for identity in packet["remove_definitions"]:
            self.definitions.pop(identity, None)
        for identity in packet["remove_images"]:
            self.images.pop(identity, None)
        self._check_references()

    def _check_references(self):
        identities, references = set(self.definitions), []

        def visit(node):
            assert node is not None, "Missing top-level transport node"
            if "image" in node:
                assert node["image"] in self.images, "Unresolved image resource"
            attrs = node["attrs"]
            if "id" in attrs:
                identities.add(attrs["id"])
            for value in attrs.values():
                if isinstance(value, str):
                    references.extend(re.findall(r"url\(#([^)]*)\)", value))
            for child in node.get("children", ()):
                visit(child)

        for node in [*self.nodes, *self.definitions.values()]:
            visit(node)
        assert set(references) <= identities, "Unresolved clip or gradient resource"

    def matches_snapshot(self, packet):
        expected = _BrowserScene()
        expected.apply(packet)
        assert self.nodes == expected.nodes
        assert self.definitions == expected.definitions
        assert self.images == expected.images


def _resource_sources():
    return {
        name: image_source(encode_png(1, 1, bytes(color)))
        for name, color in (
            ("red", (255, 0, 0, 255)),
            ("blue", (0, 0, 255, 255)),
            ("green", (0, 255, 0, 255)),
        )
    }


def _publish_resources(scene, generation, sources):
    # Exercise removal/reappearance, positional reorder, complete shrinkage,
    # inline clips, separately keyed gradients, and actual decoded PNG sources.
    orders = (
        ("red", "blue", "gradient"),
        ("gradient", "red"),
        ("green", "red"),
        ("red", "green", "blue"),
        ("gradient", "blue"),
        (),
    )
    order = orders[generation % len(orders)]
    removed = set(scene._segments) - set(order)
    scene.begin_scene(f"#{0x111100 + generation:06x}")
    for index, identity in enumerate(order):
        bounds = Rect(index * 25, generation % 5, 20, 20)
        scene.begin_segment(identity, bounds)
        scene.clip(bounds)
        if identity == "gradient":
            scene.gradient_rect(bounds, "#123456", "#abcdef", "vertical", 0)
        else:
            scene.image(sources[identity], bounds)
        scene.end_segment()
    scene.end_scene(order=order, remove=removed)


def _exercise_resource_lag(cursor):
    scene = host()
    sources = _resource_sources()
    browser = _BrowserScene()
    try:
        _publish_resources(scene, 0, sources)
        packet = scene._frame_packet(-1, cursor=cursor)
        browser.apply(packet)
        browser.matches_snapshot(scene.frame_packet(-1))
        saw_image_removal = saw_definition_removal = False
        for delivered in range(1, _MAX_CONNECTIONS * 2 + 1):
            revision = packet["revision"]
            saved = cursor.snapshot if cursor else scene._delivered_frames[revision]
            old_scene = json.dumps([saved.nodes, saved.images], sort_keys=True)
            # Thirteen publications between reads expire every ordinary frame
            # while cycling through all six resource/ordering configurations.
            for generation in range((delivered - 1) * 13 + 1, delivered * 13 + 1):
                _publish_resources(scene, generation, sources)
            assert revision not in scene._frames
            assert json.dumps([saved.nodes, saved.images], sort_keys=True) == old_scene
            packet = scene._frame_packet(revision, cursor=cursor)
            assert packet["reset"] is False
            saw_image_removal |= bool(packet["remove_images"])
            saw_definition_removal |= bool(packet["remove_definitions"])
            browser.apply(packet)
            browser.matches_snapshot(scene.frame_packet(-1))
            assert len(scene._frames) == 4
            assert set(scene._frame_image_sources) == set(scene._frames)
            assert len(scene._delivered_frames) <= _MAX_CONNECTIONS
            if cursor:
                assert not scene._delivered_frames
                assert cursor.snapshot.revision == packet["revision"]
        assert saw_image_removal and saw_definition_removal
        if cursor is None:
            assert len(scene._delivered_frames) == _MAX_CONNECTIONS
    finally:
        scene.close()
    assert not scene._delivered_frames


def test_lagging_stream_reconstructs_resources_and_order_without_reset():
    _exercise_resource_lag(_FrameCursor())


def test_lagging_poll_reconstructs_resources_with_bounded_delivered_history():
    _exercise_resource_lag(None)


def test_http_poll_base_survives_new_connections_and_unobserved_publications():
    scene = LiveSVGHost(open_browser=False)
    scene.open("Polling", 100, 100, True, None)
    endpoint = urlsplit(scene.url)
    sources = _resource_sources()
    browser = _BrowserScene()

    def poll(revision):
        # Every response deliberately uses a fresh TCP connection, as can occur
        # with fetch fallback or browser connection-pool reuse.
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        try:
            connection.request("GET", f"/frame?revision={revision}", headers={
                "X-Pysual-Token": endpoint.fragment,
                "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            assert response.status == 200
            return json.loads(response.read())
        finally:
            connection.close()

    try:
        _publish_resources(scene, 0, sources)
        packet = poll(-1)
        browser.apply(packet)
        for delivered in range(1, 5):
            for generation in range((delivered - 1) * 13 + 1, delivered * 13 + 1):
                _publish_resources(scene, generation, sources)
            assert packet["revision"] not in scene._frames
            packet = poll(packet["revision"])
            assert packet["reset"] is False
            browser.apply(packet)
            browser.matches_snapshot(scene.frame_packet(-1))
        assert len(scene._delivered_frames) <= _MAX_CONNECTIONS
    finally:
        scene.close()


def test_stalled_stream_write_preserves_base_for_next_resource_delta():
    scene = LiveSVGHost(open_browser=False)
    scene.open("Blocked stream", 100, 100, True, None)
    endpoint = urlsplit(scene.url)
    sources = _resource_sources()
    blocked, release = threading.Event(), threading.Event()
    original = _Handler._write_chunk

    def delay_first_write(handler, payload):
        if handler.adapter is scene and not blocked.is_set():
            blocked.set()
            if not release.wait(3):
                raise AssertionError("Test did not release the blocked stream write")
        return original(handler, payload)

    connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
    response = None
    try:
        _publish_resources(scene, 0, sources)
        with patch.object(_Handler, "_write_chunk", delay_first_write):
            connection.request("GET", "/stream?token=" + endpoint.fragment,
                               headers={"Accept-Encoding": "identity"})
            response = connection.getresponse()
            assert response.status == 200
            assert blocked.wait(1), "The first scene did not reach the writer"
            # The handler has captured its first packet/base but cannot finish
            # delivery. Rendering keeps advancing, like socket backpressure.
            for generation in range(1, 14):
                _publish_resources(scene, generation, sources)
            release.set()

            def packet():
                while True:
                    line = response.readline()
                    assert line, "Stream closed before its next scene"
                    if line.startswith(b"data: "):
                        return json.loads(line[6:])

            browser = _BrowserScene()
            first = packet()
            assert first["reset"] is True
            assert first["revision"] not in scene._frames
            browser.apply(first)
            latest = packet()
            assert latest["reset"] is False
            browser.apply(latest)
            browser.matches_snapshot(scene.frame_packet(-1))
            assert not scene._delivered_frames
            assert len(scene._frames) == 4
    finally:
        release.set()
        if response is not None:
            response.close()
        connection.close()
        scene.close()
