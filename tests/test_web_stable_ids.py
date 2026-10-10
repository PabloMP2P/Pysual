"""Retained SVG resources stay stable without aliasing another recording."""

import copy
import unittest
import xml.etree.ElementTree as ET

from _ui_testcase import UIOwnerTestCase

from pysual import (
    App, Button, DataGrid, GridColumn, GridRow, Rect, get_theme, theme_names,
)
from pysual.backends._web_native import LiveSVGHost
from pysual.backends._web_svg import SVGRenderer
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.runtime import Runtime


def drawing(scene, bounds, *, color="#123456", extra_clip=False):
    scene.clip(bounds)
    scene.gradient_rect(bounds, color, "#abcdef", "vertical", 4)
    scene.clip(bounds.inset(2))
    scene.text("Stable", bounds.x + 3, bounds.y + 3, "#112233", 12)
    if extra_clip:
        scene.clip(bounds.inset(4))
        scene.rect(bounds.inset(4), "#445566")


def resources(nodes):
    identifiers, references = [], []
    pending = list(nodes)
    while pending:
        node = pending.pop()
        attrs = node["attrs"]
        if "id" in attrs:
            identifiers.append(attrs["id"])
        for name, value in attrs.items():
            if value.startswith("url(#") and value.endswith(")"):
                references.append(value[5:-1])
            elif name == "href" and value.startswith("#"):
                references.append(value[1:])
        pending.extend(node.get("children", ()))
    return identifiers, references


class StableSVGIdentityTests(unittest.TestCase):
    def setUp(self):
        self.scene = SVGRenderer()
        self.scene.reset_scene("Stable resources", 320, 240)
        self.addCleanup(self.scene.close_scene)

    def segment(self, identity, bounds=Rect(0, 0, 80, 30), **options):
        self.scene.begin_segment(identity, bounds)
        drawing(self.scene, bounds, **options)
        self.scene.end_segment()

    def assert_resources_resolve(self):
        ids, refs = resources(self.scene._scene_nodes())
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)), "Duplicate SVG resource ID")
        self.assertTrue(set(refs).issubset(ids), "Unresolved SVG resource reference")
        # The same definitions and references must survive self-contained export.
        exported = ET.fromstring(self.scene.export_svg())
        exported_ids = [node.attrib["id"] for node in exported.iter()
                        if "id" in node.attrib]
        self.assertCountEqual(exported_ids, ids)

    def test_identical_clipped_gradient_recording_keeps_revision_and_export(self):
        scene = self.scene
        scene.begin_scene("#ffffff")
        self.segment("control")
        scene.end_scene(order=["control"])
        revision = scene._revision
        original = copy.deepcopy(scene.frame_packet(-1))
        exported = scene.export_svg()
        for _ in range(5):
            scene.begin_scene("#ffffff")
            self.segment("control")
            scene.end_scene()
            self.assertEqual(scene._revision, revision)
            self.assertEqual(scene.frame_packet(-1), original)
            self.assertEqual(scene.frame_packet(revision)["updates"], [])
            self.assertEqual(scene.export_svg(), exported)
        self.assert_resources_resolve()

    def test_arbitrary_identities_have_distinct_safe_namespaces(self):
        identities = ["", "s1", "p1", "g1", "control:1", "control 1",
                      'quoted"<&>\\/#', "雪\n\x00", 27, ("tuple", 1)]
        scene = self.scene
        scene.begin_scene("#ffffff")
        for identity in identities:
            self.segment(identity)
        scene.end_scene(order=identities)
        self.assert_resources_resolve()
        namespaces = dict(scene._segment_ids)
        self.assertEqual(len(set(namespaces.values())), len(identities))
        for group, _ in scene._segments.values():
            for identity in resources([group])[0]:
                self.assertRegex(identity, r"^s[0-9]+_[0-9]+$")
        # Reordering or recording in another order cannot reassign namespaces.
        scene.begin_scene("#ffffff")
        for identity in reversed(identities):
            self.segment(identity)
        scene.end_scene(order=reversed(identities))
        self.assertEqual(scene._segment_ids, namespaces)
        self.assert_resources_resolve()

    def test_changed_geometry_keeps_ids_without_mutating_old_frame(self):
        scene = self.scene
        scene.begin_scene("#ffffff")
        self.segment("changed")
        self.segment("unchanged", Rect(100, 0, 80, 30))
        scene.end_scene(order=["changed", "unchanged"])
        revision = scene._revision
        previous = copy.deepcopy(scene._frames[revision])
        old_ids = resources([scene._segments["changed"][0]])[0]
        unchanged = scene._segments["unchanged"]
        scene.begin_scene("#ffffff")
        self.segment("changed", Rect(2, 4, 80, 30), color="#654321")
        scene.end_scene()
        self.assertEqual([index for index, _ in scene.frame_packet(revision)["updates"]],
                         [1])
        self.assertEqual(resources([scene._segments["changed"][0]])[0], old_ids)
        self.assertIs(scene._segments["unchanged"], unchanged)
        self.assertEqual(scene._frames[revision], previous)
        self.assert_resources_resolve()

    def test_topology_changes_resolve_all_references_and_keep_other_segments(self):
        scene = self.scene
        scene.begin_scene("#ffffff")
        self.segment("first")
        self.segment("second", Rect(100, 0, 80, 30))
        scene.end_scene(order=["first", "second"])
        second = scene._segments["second"]
        for extra_clip in (True, False):
            scene.begin_scene("#ffffff")
            self.segment("first", extra_clip=extra_clip)
            scene.end_scene()
            self.assertIs(scene._segments["second"], second)
            self.assert_resources_resolve()

    def test_surface_recordings_inside_segments_keep_independent_ids(self):
        scene = self.scene
        first = scene.surface_create(Rect(0, 0, 80, 30))
        second = scene.surface_create(Rect(100, 0, 80, 30))
        scene.begin_scene("#ffffff")
        scene.begin_segment("surface owner", Rect(0, 0, 200, 100))
        drawing(scene, Rect(0, 40, 80, 30))
        for surface in (first, second):
            scene.surface_begin(surface)
            drawing(scene, surface.rect)
            scene.surface_end()
            scene.surface_blit(surface)
        scene.end_segment()
        self.segment("other", Rect(100, 40, 80, 30))
        scene.end_scene(order=["surface owner", "other"])
        self.assert_resources_resolve()
        ids, _ = resources(scene._scene_nodes())
        self.assertTrue(any(identity.startswith("p") for identity in ids))
        self.assertTrue(any(identity.startswith("s") for identity in ids))
        revision = scene._revision
        scene.begin_scene("#ffffff")
        scene.begin_segment("surface owner", Rect(0, 0, 200, 100))
        drawing(scene, Rect(0, 40, 80, 30))
        for surface in (first, second):
            scene.surface_blit(surface)
        scene.end_segment()
        scene.end_scene()
        self.assertEqual(scene._revision, revision)
        self.assert_resources_resolve()

    def test_removed_identities_release_namespaces_without_reusing_retired_ids(self):
        scene = self.scene
        scene.begin_scene("#ffffff")
        self.segment("keeper")
        scene.end_scene(order=["keeper"])
        keeper = scene._segment_ids["keeper"]
        retired = set()
        for index in range(100):
            identity = f"temporary:{index % 3}"
            scene.begin_scene("#ffffff")
            self.segment(identity)
            scene.end_scene(order=["keeper", identity])
            assigned = scene._segment_ids[identity]
            self.assertNotIn(assigned, retired)
            retired.add(assigned)
            scene.begin_scene("#ffffff")
            scene.end_scene(order=["keeper"], remove=[identity, "missing"])
            self.assertEqual(scene._segment_ids, {"keeper": keeper})
            self.assertEqual(set(scene._segments), {"keeper"})
        self.assert_resources_resolve()

    def test_close_and_reset_clear_identity_mappings_and_surface_handles(self):
        scene = self.scene
        for operation in (scene.close_scene,
                          lambda: scene.reset_scene("Reopened", 320, 240)):
            scene.begin_scene("#ffffff")
            self.segment("control")
            scene.end_scene(order=["control"])
            surface = scene.surface_create(Rect(0, 0, 10, 10))
            assigned = scene._segment_ids["control"]
            operation()
            self.assertEqual(scene._segment_ids, {})
            self.assertEqual(scene._segments, {})
            self.assertTrue(surface.released)
            scene.begin_scene("#ffffff")
            self.segment("control")
            scene.end_scene(order=["control"])
            self.assertGreater(scene._segment_ids["control"], assigned)
            self.assert_resources_resolve()


class StableControlSceneTests(UIOwnerTestCase):
    def scene(self, theme="modern"):
        app = App(width=640, height=400, theme=get_theme(theme), reduce_motion=True)
        host = LiveSVGHost(open_browser=False)
        runtime = Runtime(app, host)
        app._runtime = runtime
        retained = runtime._retained_paint = RetainedPaintTree(host)

        def cleanup():
            app._runtime = None
            app.destroy()
            host.close_scene()

        self.addCleanup(cleanup)
        return app, host, retained

    def test_hidden_grid_cell_edit_does_not_publish_a_visual_change(self):
        app, host, retained = self.scene()
        grid = DataGrid(parent=app, width=560, height=300,
                        columns=(GridColumn("name", "Name"),
                                 GridColumn("value", "Value", kind="number")),
                        rows=tuple(GridRow(str(i), (f"Row {i}", i))
                                   for i in range(100)))
        arrange(app, host)
        paint_tree(app, host, retained=retained)
        revision, before = host._revision, host.export_svg()
        grid.set_cell("99", "name", "Updated offscreen")
        arrange(app, host)
        paint_tree(app, host, retained=retained)
        self.assertEqual(host._revision, revision)
        self.assertEqual(host.frame_packet(revision)["updates"], [])
        self.assertEqual(host.export_svg(), before)
        # A visible edit still reaches the scene normally.
        grid.set_cell("0", "name", "Visible edit")
        arrange(app, host)
        paint_tree(app, host, retained=retained)
        self.assertGreater(host._revision, revision)
        self.assertIn("Visible edit", host.export_svg())

    def test_identical_button_invalidation_is_unchanged_across_catalog_themes(self):
        for theme in theme_names():
            with self.subTest(theme=theme):
                app, host, retained = self.scene(theme)
                button = Button(parent=app, width=160, height=40, text="Stable")
                arrange(app, host)
                paint_tree(app, host, retained=retained)
                revision = host._revision
                initial = copy.deepcopy(host.frame_packet(-1))
                button.invalidate()
                paint_tree(app, host, retained=retained)
                self.assertEqual(host._revision, revision)
                self.assertEqual(host.frame_packet(-1), initial)
