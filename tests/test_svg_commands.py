"""Pure SVG output caches preserve eager drawing and callback behavior."""

import gc
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch
from weakref import ref

from pysual import Rect, get_theme
from pysual._png import encode_png
from pysual.backends import _font
from pysual.backends._web_svg import SVGRenderer, _clip_definition, _node, _text_node
from pysual.image_resources import image_source
from pysual.painting import Painter
from test_web_svg import RecordingRenderer


class SVGOutputCacheTests(unittest.TestCase):
    bounds = Rect(0, 0, 200, 80)

    def setUp(self):
        _clip_definition.cache_clear()
        _text_node.cache_clear()

    def scene(self, cls=RecordingRenderer):
        scene = cls()
        scene.reset_scene("Output cache", 320, 240)
        return scene

    def draw(self, scene, callback, *, cached=True):
        with ExitStack() as stack:
            if not cached:
                # Same eager renderer with only its pure memoization disabled.
                def text_node(*args):
                    return _text_node.__wrapped__(*args)
                text_node.__wrapped__ = _text_node.__wrapped__
                stack.enter_context(patch("pysual.backends._web_svg._text_node", text_node))
                stack.enter_context(patch("pysual.backends._web_svg._clip_definition",
                                          _clip_definition.__wrapped__))
            scene.begin_scene("#ffffff")
            scene.begin_segment("body", self.bounds)
            try:
                callback(scene)
            finally:
                scene.end_segment()
            scene.end_scene(order=["body"])

    def caption(self, scene, text="A long caption that needs truncating"):
        scene.clip(self.bounds)
        Painter(scene, self.bounds, get_theme("modern")).caption(
            text, Rect(3, 4, 95, 28), align="center", color="#123456", size=14)

    def test_same_drawing_reuses_immutable_leaves_but_runs_the_painter(self):
        scene = self.scene()
        calls = []
        def draw(host):
            calls.append("paint")
            self.caption(host)
        self.draw(scene, draw)
        previous = scene._frame_snapshot()
        group = scene._segments["body"]
        text_hits = _text_node.cache_info().hits
        clip_hits = _clip_definition.cache_info().hits
        self.draw(scene, draw)
        self.assertEqual(calls, ["paint", "paint"])
        self.assertIs(scene._segments["body"], group)
        self.assertEqual(scene._frame_snapshot(), previous)
        self.assertGreater(_clip_definition.cache_info().hits, clip_hits)
        self.assertGreater(_text_node.cache_info().hits, text_hits)

    def test_clipping_keeps_transforms_and_existing_clip_intersections_nested(self):
        scene = self.scene()
        nested = _node("g", {"transform": "translate(17 3)", "clip-path": "url(#inner)"}, [
            _node("defs", children=[_node("clipPath", {"id": "inner"}, [
                _node("rect", {"x": "0", "y": "0", "width": "10", "height": "10"}),
            ])]),
            _node("rect", {"x": "0", "y": "0", "width": "30", "height": "30"}),
        ])
        def draw(host):
            host.clip(Rect(2, 3, 40, 50))
            host._append(nested)
        self.draw(scene, draw)
        children = scene._segments["body"][0]["children"]
        self.assertEqual([node["tag"] for node in children], ["defs", "g"])
        clip_group = children[1]
        self.assertNotIn("transform", clip_group["attrs"])
        self.assertEqual(clip_group["children"], [nested])
        self.assertEqual(nested["attrs"], {"transform": "translate(17 3)", "clip-path": "url(#inner)"})
        self.assertEqual(children[0]["children"][0]["children"][0]["attrs"],
                         {"x": "2", "y": "3", "width": "40", "height": "50"})

    def test_rerecording_equal_neighbors_preserves_their_group_identity(self):
        scene = self.scene()
        def frame(second):
            scene.begin_scene("#ffffff")
            for identity, text in (("first", "Stable"), ("second", second)):
                scene.begin_segment(identity, self.bounds)
                scene.clip(self.bounds)
                scene.text(text, 2, 3, "#123456", 14)
                scene.end_segment()
            scene.end_scene(order=["first", "second"])
        frame("Before")
        first = scene._segments["first"]
        second = scene._segments["second"]
        revision = scene._revision
        frame("After")
        self.assertIs(scene._segments["first"], first)
        self.assertIsNot(scene._segments["second"], second)
        self.assertEqual([index for index, node in scene.frame_packet(revision)["updates"]], [2])

    def test_changed_caption_reuses_clip_definition_and_matches_uncached_svg(self):
        scene, eager = self.scene(), self.scene()
        self.draw(scene, self.caption)
        previous = scene._frame_snapshot()
        old_nodes = repr(previous.nodes)
        hits = _clip_definition.cache_info().hits
        self.draw(scene, lambda host: self.caption(host, "Changed"))
        self.draw(eager, lambda host: self.caption(host, "Changed"), cached=False)
        self.assertGreater(_clip_definition.cache_info().hits, hits)
        self.assertEqual(scene.export_svg(), eager.export_svg())
        self.assertEqual(repr(previous.nodes), old_nodes)
        self.assertNotEqual(scene._frame_snapshot().nodes, previous.nodes)

    def test_changed_clip_and_multiple_texts_do_not_mutate_published_nodes(self):
        scene, eager = self.scene(), self.scene()
        snapshots = []
        for offset in (0, 10, 0):
            def draw(host):
                host.clip(Rect(offset, 2, 100, 30))
                host.text("Repeated", 3, 4, "#123456", 14)
                host.text("Second", 30, 5, "#abcdef", 12)
                host.clip(None)
                host.text("Repeated", 3, 4, "#123456", 14)
            self.draw(scene, draw)
            self.draw(eager, draw, cached=False)
            self.assertEqual(scene.export_svg(), eager.export_svg())
            snapshots.append((scene._frame_snapshot(), repr(scene._frame_snapshot().nodes)))
        for snapshot, contents in snapshots:
            self.assertEqual(repr(snapshot.nodes), contents)

    def test_mutable_scalar_and_geometry_inputs_are_stringified_eagerly(self):
        class Color:
            value = "#123456"
            def __str__(self):
                return self.value
        color = Color()
        bounds = SimpleNamespace(x=2, y=3, width=20, height=30)
        scene, eager = self.scene(), self.scene()
        def draw(host):
            host.clip(bounds)
            host.text("Text", 2, 3, color, 14)
            bounds.x = 90
            color.value = "#abcdef"
        for host, cached in ((scene, True), (eager, False)):
            bounds.x, color.value = 2, "#123456"
            self.draw(host, draw, cached=cached)
        self.assertEqual(scene.export_svg(), eager.export_svg())
        self.assertIn('fill="#123456"', scene.export_svg())
        self.assertIn('x="2"', scene.export_svg())
        self.draw(scene, lambda host: host.text("Text", 2, 3, color, 14))
        self.assertIn('fill="#abcdef"', scene.export_svg())

    def test_string_subclass_converts_once_and_cannot_retain_renderer_in_cache(self):
        class Color(str):
            calls = 0
            def __str__(self):
                self.calls += 1
                return self
        scene = self.scene()
        color = Color("#123456")
        color.owner = scene
        for _ in range(2):
            self.draw(scene, lambda host: host.text("Text", 2, 3, color, 14))
        self.assertEqual(color.calls, 2)
        reference = ref(scene)
        scene.close_scene()
        del scene, color
        gc.collect()
        self.assertIsNone(reference())
        self.assertGreater(_text_node.cache_info().currsize, 0)

    def test_equal_numeric_values_with_different_svg_spellings_do_not_alias(self):
        scene, eager = self.scene(), self.scene()
        for value in (0, False, 0.0, True, 1, 1.0):
            def draw(host):
                host.clip(Rect(value, 0, 30, 30))
                host.text("Text", value, 3, "#123456", 14)
            self.draw(scene, draw)
            self.draw(eager, draw, cached=False)
            self.assertEqual(scene.export_svg(), eager.export_svg())
            self.assertIn(f'x="{value}"', scene.export_svg())

    def test_custom_services_and_base_wrappers_execute_eagerly_each_time(self):
        class Custom(RecordingRenderer):
            def __init__(self):
                super().__init__()
                self.measures = self.texts = 0
            def measure(self, *args):
                self.measures += 1
                return super().measure(*args)
            def text(self, *args):
                self.texts += 1
                super().text(*args)
        scene = self.scene(Custom)
        original = SVGRenderer.text
        calls = []
        def wrapped(host, *args):
            calls.append(args)
            original(host, *args)
        with patch.object(SVGRenderer, "text", wrapped):
            self.draw(scene, self.caption)
            measured = scene.measures
            self.draw(scene, self.caption)
        self.assertGreater(scene.measures, measured)
        self.assertEqual(scene.texts, 2)
        self.assertEqual(len(calls), 2)

    def test_errors_remain_at_call_site_and_failed_painter_keeps_published_frame(self):
        scene = self.scene()
        self.draw(scene, self.caption)
        previous = scene._frame_snapshot()
        class BrokenColor:
            def __str__(self):
                raise RuntimeError("conversion failed")
        def draw(host):
            with self.assertRaisesRegex(RuntimeError, "conversion failed"):
                host.text("Text", 0, 0, BrokenColor(), 14)
            with self.assertRaises(ValueError):
                host.text("Multiline\ntext", 0, 0, "#123456", -1)
            raise RuntimeError("painter failed")
        with self.assertRaisesRegex(RuntimeError, "painter failed"):
            self.draw(scene, draw)
        self.assertEqual(scene._frame_snapshot(), previous)
        self.draw(scene, self.caption)
        self.assertEqual(scene._frame_snapshot(), previous)

    def test_gradients_images_surfaces_and_resource_callbacks_keep_eager_parity(self):
        source = image_source(encode_png(1, 1, bytes((255, 0, 0, 255))))
        scene, eager = self.scene(), self.scene()
        def draw(host):
            self.caption(host, "Before")
            host.gradient_rect(self.bounds, "#123456", "#abcdef", "vertical", 3)
            surface = host.surface_create(Rect(2, 3, 40, 30))
            host.surface_begin(surface)
            host.image(source, Rect(2, 3, 20, 20), fit="cover")
            host.text("Surface", 2, 3, "#123456", 14)
            host.surface_end()
            host.surface_blit(surface)
            host.surface_release(surface)
            host.image("missing-image-file.png", self.bounds)
        self.draw(scene, draw)
        self.draw(eager, draw, cached=False)
        self.assertEqual(scene.export_svg(), eager.export_svg())
        self.assertEqual(scene.frame_packet(-1), eager.frame_packet(-1))
        self.assertEqual(scene.errors, eager.errors)

    def test_cache_keys_include_actual_font_baseline_and_long_text_is_not_retained(self):
        scene, eager = self.scene(), self.scene()
        for ascent in (3, 10):
            with patch.object(_font, "ascent", return_value=ascent):
                draw = lambda host: host.text("Font", 2, 5, "#123456", 14)
                self.draw(scene, draw)
                self.draw(eager, draw, cached=False)
            self.assertEqual(scene.export_svg(), eager.export_svg())
            self.assertIn(f'y="{5 + ascent}"', scene.export_svg())
        before = _text_node.cache_info()
        self.draw(scene, lambda host: host.text(
            "x" * (_font.MEASURE_CACHE_TEXT_LIMIT + 1), 2, 5, "#123456", 14))
        self.assertEqual(_text_node.cache_info(), before)

    def test_oversized_scalar_strings_do_not_enter_pure_caches(self):
        scene = self.scene()
        text_before, clip_before = _text_node.cache_info(), _clip_definition.cache_info()
        self.draw(scene, lambda host: host.text("Text", 2, 3, "x" * 2048, 14))
        self.assertEqual(_text_node.cache_info(), text_before)
        class Coordinate(float):
            def __str__(self):
                return "1" * 2048
        def draw(host):
            host.clip(Rect(Coordinate(1), 0, 30, 30))
            host.rect(Rect(0, 0, 20, 20), "#123456")
        self.draw(scene, draw)
        self.assertEqual(_clip_definition.cache_info(), clip_before)

    def test_pure_caches_are_bounded_and_do_not_keep_renderer_alive(self):
        for index in range(4100):
            _clip_definition(str(index), "0", "0", "10", "10")
            _text_node("0", "0", "#123456", "14", "Pysual Sans", str(index))
        self.assertEqual(_clip_definition.cache_info().currsize, 4096)
        self.assertEqual(_text_node.cache_info().currsize, 4096)
        scene = self.scene()
        self.draw(scene, self.caption)
        reference = ref(scene)
        scene.close_scene()
        del scene
        gc.collect()
        self.assertIsNone(reference())

    def test_closing_and_reopening_preserves_previous_immutable_snapshot(self):
        scene = self.scene()
        self.draw(scene, self.caption)
        previous = scene._frame_snapshot()
        contents = repr(previous.nodes)
        scene.close_scene()
        scene.reset_scene("Again", 320, 240)
        self.draw(scene, lambda host: self.caption(host, "After reopen"))
        self.assertEqual(repr(previous.nodes), contents)
        self.assertIn("After", scene.export_svg())


if __name__ == "__main__":
    unittest.main()
