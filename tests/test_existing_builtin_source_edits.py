"""Existing controls may gain dependencies without coordinating cache adapters."""
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase
import test_builtin_cache_defaults as defaults
from test_builtin_cache_defaults import catalog_entries
from test_retained_paint import PatchRecordingHost

from pysual import Container, Label, TextBox
from pysual.layout import _size, arrange


class ExistingBuiltinSourceEdits(UIOwnerTestCase):
    fixture = defaults.BuiltinCacheDefaultTests.fixture

    def test_label_source_paint_gains_private_mutable_dependency(self):
        def paint(control, painter):
            painter.text(",".join(control._private_captions), 0, 0)

        with patch.object(Label, "paint", paint), catalog_entries():
            for mode in ("retained", "surface"):
                with self.subTest(mode=mode):
                    app, host, retained, frame, snapshot, assert_direct = self.fixture(mode)
                    label = Label(parent=app, text="Original public caption", width=200,
                                  height=30, cache_paint=True)
                    label._private_captions = ["Before"]
                    arrange(app, host)
                    for _ in range(3):
                        frame()
                    before = snapshot()
                    label._private_captions.append("After")
                    label.invalidate()
                    frame()
                    self.assertNotEqual(snapshot(), before)
                    assert_direct()

    def test_textbox_source_helper_gains_private_mutable_dependency(self):
        original = TextBox._text_left

        def text_left(control):
            return original(control) + control._private_offsets[0]

        with patch.object(TextBox, "_text_left", text_left), catalog_entries():
            for mode in ("retained", "surface"):
                with self.subTest(mode=mode):
                    app, host, retained, frame, snapshot, assert_direct = self.fixture(mode)
                    editor = TextBox(parent=app, text="Input", width=200, height=32,
                                     cache_paint=True)
                    editor._private_offsets = [0]
                    arrange(app, host)
                    for _ in range(3):
                        frame()
                    before = snapshot()
                    editor._private_offsets[0] = 25
                    editor.invalidate()
                    frame()
                    self.assertNotEqual(snapshot(), before)
                    assert_direct()

    def test_label_source_measure_gains_image_resource_dependency(self):
        def measure(control, host):
            return host.image_extent, 24

        with patch.object(Label, "measure", measure), catalog_entries():
            label = Label(text="Image caption")
            self.addCleanup(label.destroy)
            host = PatchRecordingHost()
            host.metrics_revision = 0
            host.image_extent = 40
            self.assertEqual(_size(label, host), (40, 24))
            host.image_extent = 80
            host.resource_revision += 1
            self.assertEqual(_size(label, host), (80, 24))

    def test_existing_container_source_paint_gains_descendant_dependency(self):
        def paint(control, painter):
            painter.text(control._children[0]._private_captions[0], 0, 0)

        with patch.object(Container, "paint", paint), catalog_entries():
            for mode in ("retained", "surface"):
                with self.subTest(mode=mode):
                    app, host, retained, frame, snapshot, assert_direct = self.fixture(mode)
                    parent = Container(parent=app, width=200, height=80, cache_paint=True)
                    child = Label(parent=parent, text="Child", width=120, height=30, top=40)
                    child._private_captions = ["Before"]
                    # App also inherits Container.paint, so keep its own body
                    # on its normal rendering path while changing this class.
                    object.__setattr__(app, "paint", lambda painter: None)
                    self.addCleanup(object.__delattr__, app, "paint")
                    arrange(app, host)
                    for _ in range(3):
                        frame()
                    before = snapshot()
                    child._private_captions[0] = "After"
                    child.invalidate()
                    frame()
                    self.assertNotEqual(snapshot(), before)
                    if retained is not None:
                        self.assertIn(retained._records[parent].identity, host.updates)
                    assert_direct()
