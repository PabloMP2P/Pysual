"""Scene behavior shared by the native and Pyodide delivery adapters."""

import json
import unittest
import xml.etree.ElementTree as ET
from math import ceil, floor
from unittest.mock import patch

from pysual import App, Button, Dropdown, Rect, get_theme
from pysual._engine import ui_method
from pysual._png import encode_png
from pysual.backends._gradient import gradient_colors
from pysual.backends._web_svg import SVGRenderer, _gradient_stops, _image_sources
from pysual.geometry import Rect
from pysual.host import Input, Viewport
from pysual.image_resources import image_source
from pysual.painting import Painter
from pysual.runtime import Runtime


class RecordingRenderer(SVGRenderer):
    capabilities = frozenset({"images", "shadow_masks", "render_surfaces"})

    def __init__(self):
        super().__init__()
        self.errors = []

    def _resource_error(self, message):
        self.errors.append(message)

    def present(self):
        self.publish_scene()


class SVGRendererTests(unittest.TestCase):
    def setUp(self):
        self.scene = RecordingRenderer()
        self.scene.reset_scene("Shared scene", 320, 240)

    def test_sparse_updates_only_inspect_changed_groups_for_image_dependencies(self):
        scene = self.scene
        source = image_source(encode_png(1, 1, bytes((255, 0, 0, 255))))

        def control(index, color):
            bounds = Rect(index, 0, 20, 20)
            scene.begin_segment(index, bounds)
            scene.clip(bounds)
            scene.gradient_rect(bounds, "#123456", "#abcdef", "vertical", 4)
            scene.rect(bounds.inset(2), color, 2)
            if index % 10 == 0:
                scene.image(source, bounds.inset(4))
            scene.end_segment()

        scene.begin_scene("#ffffff")
        for index in range(200):
            control(index, "#112233")
        scene.end_scene(order=range(200))
        initial = scene.frame_packet(-1)
        identifier = next(iter(initial["image_sources"]))
        with patch("pysual.backends._web_svg._image_sources", wraps=_image_sources) as inspect:
            scene.begin_scene("#ffffff")
            control(37, "#445566")
            scene.end_scene()
            self.assertEqual(inspect.call_count, 1)
            changed = scene.frame_packet(initial["revision"])
            self.assertEqual([index for index, _ in changed["updates"]], [38])
            self.assertEqual(changed["image_sources"], {})
            self.assertEqual(changed["remove_images"], [])
            self.assertEqual(scene.frame_packet(-1)["image_sources"], {identifier: source})
            inspect.reset_mock()
            # Reordering stable groups changes transport slots, not dependencies.
            scene.begin_scene("#ffffff")
            scene.end_scene(order=reversed(range(200)))
            inspect.assert_not_called()
            self.assertEqual(scene.frame_packet(-1)["image_sources"], {identifier: source})

    def test_group_image_dependencies_follow_surface_rebuild_removal_and_reset(self):
        scene = self.scene
        first = image_source(encode_png(1, 1, bytes((255, 0, 0, 255))))
        second = image_source(encode_png(1, 1, bytes((0, 0, 255, 255))))
        bounds = Rect(0, 0, 20, 20)
        surface = scene.surface_create(bounds)

        def rebuild(source):
            scene.surface_begin(surface)
            scene.image(source, bounds)
            scene.surface_end()

        def blit():
            scene.begin_segment("image", bounds)
            scene.surface_blit(surface)
            scene.end_segment()

        rebuild(first)
        scene.begin_scene("#ffffff")
        blit()
        scene.end_scene(order=["image"])
        initial = scene.frame_packet(-1)
        first_id = next(iter(initial["image_sources"]))
        rebuild(second)
        # A rebuilt surface must not change its previously published instances.
        scene.begin_scene("#eeeeee")
        scene.end_scene()
        self.assertEqual(scene.frame_packet(-1)["image_sources"], {first_id: first})
        scene.begin_scene("#eeeeee")
        blit()
        scene.end_scene()
        changed = scene.frame_packet(initial["revision"])
        self.assertEqual(changed["remove_images"], [first_id])
        self.assertEqual(list(changed["image_sources"].values()), [second])
        second_id = next(iter(changed["image_sources"]))
        self.assertEqual(scene._frame_image_sources[initial["revision"]], {first_id: first})
        scene.surface_release(surface)
        self.assertEqual(scene.frame_packet(-1)["image_sources"], {second_id: second})
        scene.begin_scene("#eeeeee")
        scene.end_scene(order=[], remove=["image"])
        removed = scene.frame_packet(changed["revision"])
        self.assertEqual(removed["remove_images"], [second_id])
        self.assertEqual(removed["image_sources"], {})
        self.assertEqual(len(scene._node_image_sources), 1)
        scene.reset_scene("Reopened", 320, 240)
        self.assertEqual(scene._node_image_sources, {})

    def test_repeated_images_send_source_once_and_keep_fit_and_snapshot_hrefs(self):
        scene = self.scene
        source = image_source(encode_png(2, 1, bytes((255, 0, 0, 255)) * 2))

        def paint(offset):
            scene.begin_scene("#ffffff")
            for index, fit in enumerate(("stretch", "contain", "cover")):
                rect = Rect(index * 70 + offset, 0, 60, 40)
                scene.begin_segment(str(index), rect)
                scene.clip(rect)
                scene.image(source, rect, fit=fit)
                scene.end_segment()
            scene.end_scene(order=["0", "1", "2"])

        paint(0)
        initial = scene.frame_packet(-1)
        identifier = str(scene._images[source][1])
        self.assertEqual(initial["image_sources"], {identifier: source})
        self.assertNotIn(source, json.dumps(initial["updates"]))
        self.assertEqual(json.dumps(initial).count(source), 1)
        paint(1)
        moved = scene.frame_packet(initial["revision"])
        self.assertEqual(moved["image_sources"], {})
        self.assertEqual(moved["remove_images"], [])
        self.assertNotIn(source, json.dumps(moved))
        self.assertEqual(len(moved["updates"]), 3)
        root = ET.fromstring(scene.export_svg())
        ns = {"s": "http://www.w3.org/2000/svg"}
        images = root.findall(".//s:image", ns)
        self.assertEqual([node.get("href") for node in images], [source] * 3)
        self.assertEqual([node.get("preserveAspectRatio") for node in images],
                         ["none", "xMidYMid meet", "xMidYMid slice"])
        self.assertEqual(len(root.findall(".//s:clipPath", ns)), 3)
        self.assertIn(source, scene.export_html())

    def test_image_resources_follow_frame_history_removal_and_reappearance(self):
        scene = self.scene
        source = image_source(encode_png(1, 1, bytes((255, 0, 0, 255))))
        scene.begin("#ffffff")
        scene.image(source, Rect(0, 0, 10, 10))
        scene.present()
        first = scene.frame_packet(-1)
        identifier = next(iter(first["image_sources"]))
        scene.begin("#ffffff")
        scene.present()
        removed = scene.frame_packet(first["revision"])
        self.assertEqual(removed["remove_images"], [identifier])
        self.assertEqual(removed["image_sources"], {})
        for x in range(6):
            scene.begin("#ffffff")
            scene.image(source, Rect(x, 0, 10, 10))
            scene.present()
        reappeared = scene.frame_packet(removed["revision"])
        self.assertTrue(reappeared["reset"])
        self.assertEqual(reappeared["image_sources"], {identifier: source})
        self.assertEqual(set(scene._frame_image_sources), set(scene._frames))
        self.assertEqual(len(scene._frame_image_sources), 4)
        scene.reset_scene("Reopened", 320, 240)
        self.assertEqual(scene._frame_image_sources, {})

    def test_cached_surface_images_use_references_without_losing_decode_diagnostics(self):
        scene = self.scene
        source = "data:image/gif;base64,Y29ycnVwdA=="
        surface = scene.surface_create(Rect(0, 0, 20, 20))
        scene.surface_begin(surface)
        scene.image(source, Rect(0, 0, 20, 20), fit="cover")
        scene.surface_end()
        scene.begin("#ffffff")
        scene.surface_blit(surface)
        scene.present()
        packet = scene.frame_packet(-1)
        identifier = next(iter(packet["image_sources"]))
        self.assertEqual(packet["image_sources"], {identifier: source})
        self.assertNotIn(source, json.dumps(packet["updates"]))
        scene.report_image_error(int(identifier))
        self.assertIn("Browser could not decode", scene.errors[0])

    def test_disconnected_strokes_keep_separate_svg_overlap_composition(self):
        scene = self.scene
        segments = ((10, 20, 30, 40), (70, 80, 90, 100))
        painter = Painter(scene, Rect(0, 0, 320, 240), get_theme("modern_dark"))
        for color in ("#abcdef", "#abcdef80"):
            with self.subTest(color=color):
                scene.begin("#000000")
                painter.segments(segments, color, 3)
                actual = list(scene._nodes)
                scene.begin("#000000")
                for stroke in segments:
                    scene.line(*stroke, color, 3)
                # One opaque path can also change antialias overlap coverage.
                self.assertEqual(actual, scene._nodes)

    def test_retained_background_tracks_viewport_without_repainting_segments(self):
        scene = self.scene
        scene.begin_scene("#f1f4fa")
        scene.begin_segment("label", Rect(0, 0, 80, 20))
        scene.text("stable", 4, 4, "#111111", 14)
        scene.end_segment()
        scene.end_scene(order=["label"])
        segment = scene._nodes[1]
        for width, height in ((800, 600), (200, 100)):
            with self.subTest(size=(width, height)):
                previous = scene._nodes[0]
                revision = scene._revision
                scene.size = (width, height)
                scene._viewport = Viewport(width, height)
                scene.begin_scene("#f1f4fa")
                scene.end_scene()
                packet = scene.frame_packet(revision)
                background = scene._nodes[0]
                self.assertEqual((packet["width"], packet["height"]), (width, height))
                self.assertEqual(background["attrs"]["width"], str(width))
                self.assertEqual(background["attrs"]["height"], str(height))
                self.assertIsNot(background, previous)
                self.assertIs(scene._nodes[1], segment)
                self.assertEqual([index for index, _ in packet["updates"]], [0])
                revision = scene._revision
                scene.begin_scene("#f1f4fa")
                scene.end_scene()
                self.assertIs(scene._nodes[0], background)
                self.assertEqual(scene._revision, revision)

    def test_retained_segments_keep_the_revision_until_one_group_changes(self):
        scene = self.scene

        def paint(text):
            scene.begin_scene("#101418")
            scene.begin_segment("label", Rect(0, 0, 80, 20))
            scene.text(text, 4, 4, "#ffffff", 14)
            scene.end_segment()
            scene.begin_segment("other", Rect(0, 30, 80, 20))
            scene.text("stable", 4, 4, "#ffffff", 14)
            scene.end_segment()
            scene.end_scene(order=["label", "other"])

        paint("one")
        revision = scene._revision
        paint("one")
        self.assertEqual(scene._revision, revision)
        self.assertEqual(scene.frame_packet(revision)["updates"], [])
        paint("two")
        changed = scene.frame_packet(revision)
        self.assertEqual(scene._revision, revision + 1)
        self.assertEqual([index for index, _node in changed["updates"]], [1])

    def test_scene_packets_recover_after_missing_history_and_skip_unchanged_frames(self):
        scene = self.scene
        scene.begin("#ffffff")
        scene.text("First", 4, 8, "#111111", 16)
        self.assertTrue(scene.publish_scene())
        first = scene.frame_packet(-1)
        self.assertTrue(first["reset"])
        self.assertEqual(first["length"], 2)
        self.assertEqual(first["updates"][1][1]["text"], "First")
        self.assertFalse(scene.publish_scene())
        self.assertEqual(scene.frame_packet(first["revision"])["updates"], [])

        for value in range(5):
            scene.begin("#ffffff")
            scene.text(str(value), 4, 8, "#111111", 16)
            scene.publish_scene()
        recovered = scene.frame_packet(first["revision"])
        self.assertTrue(recovered["reset"])
        self.assertEqual(recovered["updates"][1][1]["text"], "4")

    def test_cached_scene_survives_surface_rebuild_and_reset_invalidates_old_handles(self):
        scene = self.scene
        surface = scene.surface_create(Rect(1, 2, 40, 30))
        scene.surface_begin(surface)
        scene.clip(Rect(1, 2, 20, 20))
        scene.rect(Rect(1, 2, 40, 30), "#ff0000")
        scene.surface_end()
        scene.begin("#ffffff")
        scene.surface_blit(surface)
        scene.publish_scene()
        saved = json.dumps(scene.frame_packet(-1))

        scene.surface_begin(surface)
        scene.rect(Rect(1, 2, 40, 30), "#0000ff")
        scene.surface_end()
        self.assertEqual(json.dumps(scene.frame_packet(-1)), saved)
        before = scene.resource_revision
        scene.reset_scene("Second opening", 200, 100)
        self.assertGreater(scene.resource_revision, before)
        with self.assertRaisesRegex(ValueError, "released"):
            scene.surface_blit(surface)
        packet = scene.frame_packet(-1)
        self.assertEqual(packet["length"], 0)
        self.assertEqual((packet["width"], packet["height"]), (200, 100))

    def test_metadata_and_decode_failures_do_not_require_a_transport(self):
        scene = self.scene
        scene.set_title("Renamed")
        scene.text_input(Rect(3, 4, 5, 6))
        packet = scene.frame_packet(-1)
        self.assertEqual(packet["title"], "Renamed")
        self.assertEqual(packet["text_input"], [3, 4, 5, 6])
        source = image_source(encode_png(1, 1, bytes((255, 0, 0, 255))))
        scene.image(source, Rect(0, 0, 10, 10))
        identifier = int(scene._nodes[-1]["attrs"]["data-pysual-image"])
        scene.report_image_error(identifier)
        scene.report_image_error(identifier)
        scene.report_image_error(identifier + 1)
        self.assertEqual(len(scene.errors), 1)
        self.assertIn("embedded image", scene.errors[0])
        self.assertNotIn(source, scene.errors[0])

    def test_surface_bounds_clip_without_adding_a_fractional_viewport(self):
        scene = self.scene
        bounds = Rect(2.25, 3.5, 20, 30)
        surface = scene.surface_create(bounds)
        scene.surface_begin(surface)
        self.assertEqual(scene._clip, bounds)
        scene.clip(Rect(0, 0, 10, 10))
        self.assertEqual(scene._clip, Rect(2.25, 3.5, 7.75, 6.5))
        scene.clip(None)
        self.assertEqual(scene._clip, bounds)
        scene.rect(Rect(0, 0, 100, 100), "#ff000080")
        scene.surface_end()
        scene.begin("#ffffff")
        scene.surface_blit(surface)
        scene.publish_scene()
        root = ET.fromstring(scene.export_svg())
        namespace = {"s": "http://www.w3.org/2000/svg"}
        self.assertEqual(root.findall(".//s:svg", namespace), [])
        clip = root.find(".//s:clipPath/s:rect", namespace)
        self.assertEqual(clip.attrib, {"x": "2.25", "y": "3.5", "width": "20", "height": "30"})

    def test_gradient_stops_preserve_exact_device_bands_and_alpha_at_each_scale(self):
        first, last = "#11223344", "#ddeeaa99"
        rect = Rect(-0.25, 1.5, 97.5, 65.25)
        namespace = {"s": "http://www.w3.org/2000/svg"}
        for scale in (1, 1.25, 2):
            self.scene._viewport = Viewport(320, 240, scale)
            for axis in ("horizontal", "vertical"):
                with self.subTest(scale=scale, axis=axis):
                    self.scene.begin("#ffffff")
                    self.scene.gradient_rect(rect, first, last, axis, 4)
                    self.scene.publish_scene()
                    root = ET.fromstring(self.scene.export_svg())
                    stops = root.findall(".//s:linearGradient/s:stop", namespace)
                    start = rect.x if axis == "horizontal" else rect.y
                    length = rect.width if axis == "horizontal" else rect.height
                    span = ceil((start + length) * scale) - floor(start * scale)
                    count = min(96, span)
                    expected = [
                        {"offset": str(floor(span * edge / count) / span),
                         "stop-color": "#" + "".join(f"{value:02x}" for value in color)}
                        for index, color in enumerate(gradient_colors(first, last, count))
                        for edge in (index, index + 1)
                    ]
                    self.assertEqual([stop.attrib for stop in stops], expected)

    def test_gradient_stop_reuse_is_bounded_and_published_frames_survive_eviction(self):
        _gradient_stops.cache_clear()
        self.addCleanup(_gradient_stops.cache_clear)
        first, last = "#11223344", "#ddeeaa99"
        stops = _gradient_stops(first, last, 65)
        self.assertIs(_gradient_stops(first, last, 65), stops)
        self.assertEqual(_gradient_stops.cache_info().hits, 1)
        self.scene.gradient_rect(Rect(0, 0, 10, 65), first, last, "vertical", 0)
        self.scene.publish_scene()
        saved = json.dumps(self.scene.frame_packet(-1))
        for span in range(100, 180):
            _gradient_stops(first, last, span)
        info = _gradient_stops.cache_info()
        self.assertEqual((info.maxsize, info.currsize), (64, 64))
        self.assertEqual(json.dumps(self.scene.frame_packet(-1)), saved)
        self.assertEqual(_gradient_stops(first, last, 65), stops)

    def test_shared_gradient_stops_keep_each_shape_coordinates_and_distinct_resources(self):
        scene = self.scene
        first, last = "#11223344", "#ddeeaa99"

        def draw():
            scene.begin("#ffffff")
            for rect, axis, border in (
                (Rect(0, 0, 100, 10), "horizontal", 0),
                (Rect(5, 20, 100, 10), "horizontal", 2),
                (Rect(20, 7, 10, 100), "vertical", 0),
            ):
                scene.gradient_rect(rect, first, last, axis, 4, border)
            scene.gradient_rect(Rect(0, 40, 101, 10), first, last, "horizontal", 4)
            scene.gradient_rect(Rect(0, 60, 100, 10), first, "#ddeeaa55", "horizontal", 4)

        draw()
        scene.publish_scene()
        revision = scene._revision
        draw()
        self.assertFalse(scene.publish_scene())
        scene.set_title("Changed metadata")
        scene.text_input(Rect(1, 2, 30, 20))
        packet = scene.frame_packet(revision)
        self.assertEqual(packet["updates"], [])
        self.assertEqual(packet["title"], "Changed metadata")
        self.assertEqual(packet["text_input"], [1, 2, 30, 20])
        root = ET.fromstring(scene.export_svg())
        ns = {"s": "http://www.w3.org/2000/svg"}
        bases = root.findall("s:defs/s:linearGradient", ns)
        local = root.findall(".//s:linearGradient[@href]", ns)
        self.assertEqual((len(bases), len(local)), (3, 5))
        self.assertEqual(len({gradient.get("href") for gradient in local[:3]}), 1)
        self.assertEqual(len({gradient.get("href") for gradient in local}), 3)
        self.assertEqual(
            [[gradient.get(attr) for attr in ("x1", "y1", "x2", "y2")]
             for gradient in local[:3]],
            [["0.0", "0", "100.0", "0"],
             ["5.0", "0", "105.0", "0"],
             ["0", "7.0", "0", "107.0"]],
        )
        self.assertTrue(all(gradient.get("gradientUnits") == "userSpaceOnUse" for gradient in local))
        definitions = {f"#{gradient.get('id')}" for gradient in bases}
        self.assertTrue(all(gradient.get("href") in definitions for gradient in local))
        self.assertTrue(all(not list(gradient) for gradient in local))
        self.assertEqual(len({node.get("id") for node in (*bases, *local)}), 8)

    def test_unblitted_surface_does_not_leak_gradient_definitions_into_a_frame(self):
        scene = self.scene
        scene.begin("#ffffff")
        scene.gradient_rect(Rect(0, 0, 40, 20), "#112233", "#445566", "horizontal", 0)
        surface = scene.surface_create(Rect(0, 0, 100, 100))
        scene.surface_begin(surface)
        scene.gradient_rect(Rect(0, 0, 60, 20), "#aabbcc", "#ddeeff", "horizontal", 0)
        scene.surface_end()
        scene.gradient_rect(Rect(60, 0, 40, 20), "#112233", "#445566", "horizontal", 0)
        scene.publish_scene()
        ns = {"s": "http://www.w3.org/2000/svg"}
        root = ET.fromstring(scene.export_svg())
        definitions = root.findall("s:defs/s:linearGradient", ns)
        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0].find("s:stop", ns).get("stop-color"), "#112233ff")
        self.assertEqual(len(root.findall(".//s:linearGradient[@href]", ns)), 2)

        scene.begin("#ffffff")
        scene.surface_blit(surface)
        scene.publish_scene()
        root = ET.fromstring(scene.export_svg())
        definitions = root.findall("s:defs/s:linearGradient", ns)
        self.assertEqual(len(definitions), 1)
        self.assertEqual(definitions[0].find("s:stop", ns).get("stop-color"), "#aabbccff")

    def test_cached_gradient_dependencies_survive_eviction_rebuild_and_release(self):
        scene = self.scene
        _gradient_stops.cache_clear()
        self.addCleanup(_gradient_stops.cache_clear)
        surface = scene.surface_create(Rect(0, 0, 100, 100))
        scene.surface_begin(surface)
        scene.gradient_rect(Rect(0, 0, 65, 20), "#112233", "#445566", "horizontal", 0)
        scene.surface_end()
        scene.begin("#ffffff")
        scene.surface_blit(surface)
        scene.publish_scene()
        revision = scene._revision
        old_frame = json.dumps(scene._frames[revision])
        old_svg = scene.export_svg()

        for span in range(100, 180):
            _gradient_stops("#112233", "#445566", span)
        self.assertEqual(_gradient_stops.cache_info().currsize, 64)
        scene.begin("#ffffff")
        scene.surface_blit(surface)
        self.assertFalse(scene.publish_scene())
        self.assertEqual(scene.frame_packet(revision)["updates"], [])
        self.assertEqual(scene.export_svg(), old_svg)

        scene.surface_begin(surface)
        scene.gradient_rect(Rect(0, 0, 65, 20), "#aabbcc", "#ddeeff", "horizontal", 0)
        scene.surface_end()
        scene.begin("#ffffff")
        scene.surface_blit(surface)
        self.assertEqual(scene.export_svg(), old_svg)
        scene.publish_scene()
        self.assertEqual(json.dumps(scene._frames[revision]), old_frame)
        new_svg = scene.export_svg()
        ns = {"s": "http://www.w3.org/2000/svg"}
        root = ET.fromstring(new_svg)
        definition = root.find("s:defs/s:linearGradient", ns)
        local = root.find(".//s:linearGradient[@href]", ns)
        self.assertEqual(local.get("href"), "#" + definition.get("id"))
        self.assertEqual(definition.find("s:stop", ns).get("stop-color"), "#aabbccff")
        scene.surface_release(surface)
        self.assertEqual(scene.export_svg(), new_svg)

    def test_pending_gradient_export_and_solid_frame_diffs_remain_self_contained(self):
        scene = self.scene
        ns = {"s": "http://www.w3.org/2000/svg"}
        scene.begin("#ffffff")
        scene.gradient_rect(Rect(0, 0, 40, 20), "#112233", "#445566", "horizontal", 0)
        pending = scene.export_svg()
        self.assertEqual(len(ET.fromstring(pending).findall("s:defs/s:linearGradient", ns)), 1)
        scene.publish_scene()
        revision = scene._revision
        self.assertEqual(scene.export_svg(), pending)
        scene.begin("#000000")
        self.assertEqual(scene.export_svg(), pending)
        scene.publish_scene()
        packet = scene.frame_packet(revision)
        self.assertFalse(packet["reset"])
        self.assertEqual(packet["length"], 1)
        self.assertEqual(packet["definitions"], [])
        self.assertEqual(len(packet["remove_definitions"]), 1)
        self.assertEqual(ET.fromstring(scene.export_svg()).findall(".//s:linearGradient", ns), [])
        scene.reset_scene("Reopened", 320, 240)
        self.assertEqual(ET.fromstring(scene.export_svg()).findall(".//s:linearGradient", ns), [])

    def test_definition_deltas_do_not_resend_unchanged_banks_or_shift_body_indices(self):
        scene = self.scene

        def draw(colors):
            scene.begin("#ffffff")
            for index, color in enumerate(colors):
                scene.gradient_rect(Rect(index * 100, 0, 100, 30), color, "#ffffff", "horizontal", 0)
            scene.publish_scene()

        draw(["#112233"])
        first = scene.frame_packet(-1)
        original = first["definitions"][0]
        original_id = original["attrs"]["id"]
        self.assertEqual(first["length"], 2)
        self.assertEqual([node["tag"] for _, node in first["updates"]], ["rect", "g"])
        saved_frame = json.dumps(scene._frames[first["revision"]])

        draw(["#112233", "#445566"])
        added = scene.frame_packet(first["revision"])
        added_id = added["definitions"][0]["attrs"]["id"]
        self.assertEqual([index for index, _ in added["updates"]], [2])
        self.assertEqual(len(added["definitions"]), 1)
        self.assertNotEqual(added_id, original_id)
        self.assertEqual(added["remove_definitions"], [])
        self.assertEqual(json.dumps(scene._frames[first["revision"]]), saved_frame)

        draw(["#112233"])
        removed = scene.frame_packet(added["revision"])
        self.assertEqual(removed["updates"], [])
        self.assertEqual(removed["definitions"], [])
        self.assertEqual(removed["remove_definitions"], [added_id])
        self.assertEqual(removed["length"], 2)
        # A skipped intermediate frame must compare against the client's bank.
        skipped = scene.frame_packet(first["revision"])
        self.assertEqual(skipped["definitions"], [])
        self.assertEqual(skipped["remove_definitions"], [])
        recovered = scene.frame_packet(-1)
        self.assertTrue(recovered["reset"])
        self.assertEqual(recovered["definitions"], [original])
        self.assertEqual(len(recovered["updates"]), recovered["length"])
        exported = ET.fromstring(scene.export_svg())
        ns = {"s": "http://www.w3.org/2000/svg"}
        self.assertEqual(exported.find("s:defs/s:linearGradient", ns).get("id"), original_id)

    def test_paint_groups_scope_ids_and_clip_groups_without_changing_paint_order(self):
        scene = self.scene

        def draw(extra):
            scene.begin("#ffffff")
            scene._paint_group_begin()
            scene.clip(Rect(0, 0, 40, 40))
            scene.rect(Rect(0, 0, 40, 40), "#ff0000")
            if extra:
                scene.clip(Rect(1, 1, 30, 30))
                scene.gradient_rect(Rect(1, 1, 30, 30), "#112233", "#445566", "vertical", 0)
            scene._paint_group_end()
            scene._paint_group_begin()
            scene.clip(Rect(50, 0, 40, 40))
            scene.gradient_rect(Rect(50, 0, 40, 40), "#112233", "#445566", "vertical", 0)
            scene._paint_group_end()
            scene.clip(None)
            scene.text("After groups", 0, 60, "#000000", 16)
            scene.publish_scene()

        draw(False)
        first = scene.frame_packet(-1)
        draw(True)
        changed = scene.frame_packet(first["revision"])
        self.assertEqual(changed["length"], first["length"])
        self.assertEqual([index for index, _ in changed["updates"]], [1])
        root = ET.fromstring(scene.export_svg())
        namespace = {"s": "http://www.w3.org/2000/svg"}
        identities = [element.get("id") for element in root.iter() if element.get("id")]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(root[-1].text, "After groups")
        self.assertEqual(len(root.findall("s:g", namespace)), 2)

    @ui_method
    def test_dropdown_focus_and_cache_admission_keep_other_controls_out_of_packets(self):
        app = App(width=800, height=600, theme=get_theme("neon"))
        dropdown = Dropdown(parent=app, left=10, top=10, width=140, height=30,
                            items=("First", "Second", "Third"))
        for index in range(100):
            Button(parent=app, left=10 + index % 10 * 75, top=60 + index // 10 * 48,
                   width=65, height=36, text=str(index))
        scene = self.scene
        scene.reset_scene("Sparse interaction", 800, 600)
        runtime = Runtime(app, scene)
        app._runtime = runtime
        app._state = "RUNNING"
        try:
            for _ in range(4):
                runtime._paint_frame()
            initial = scene.frame_packet(-1)
            # Background, application and dropdown precede the 100 button bodies.
            unchanged = initial["updates"][-100:]
            x, y = dropdown.bounds.x + 20, dropdown.bounds.y + 15
            for cycle in range(2):
                for kind in ("pointer_move", "pointer_down", "pointer_up", None, None):
                    if kind is not None:
                        runtime.router.process(Input(kind, x=x, y=y, button=1))
                    revision = scene._revision
                    runtime._paint_frame()
                    packet = scene.frame_packet(revision)
                    with self.subTest(cycle=cycle, event=kind):
                        self.assertLessEqual(len(packet["updates"]), 3)
                        self.assertLess(len(json.dumps(packet)), 100_000)
                        current = dict(scene.frame_packet(-1)["updates"])
                        for index, node in unchanged:
                            self.assertEqual(current[index], node)
                self.assertIsNotNone(runtime.popup)
                runtime.popup.dismiss()
                for _ in range(3):
                    runtime._paint_frame()
            self.assertGreater(runtime.render_cache.stats.hits, 1000)
        finally:
            runtime.render_cache.clear()
            app._runtime = None
            app._state = "CLOSED"
            app.destroy()


if __name__ == "__main__":
    unittest.main()
