"""Mixed built-in types share the ordinary explicit invalidation contract."""

from collections import Counter
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase
from test_retained_paint import PatchRecordingHost

from pysual import App, Label, Slider
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.runtime import Runtime


class SharedInvalidationTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=400, height=200, reduce_motion=True)
        self.addCleanup(self.app.destroy)
        self.host = PatchRecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.addCleanup(setattr, self.app, "_runtime", None)
        self.retained = self.runtime._retained_paint = RetainedPaintTree(self.host)
        self.calls = Counter()
        for kind in (Label, Slider):
            original = kind.paint

            def counted(control, painter, original=original):
                self.calls[control] += 1
                original(control, painter)
                for text in getattr(control, "_private_captions", ()):
                    painter.text(text, 0, 0)

            patcher = patch.object(kind, "paint", counted)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.label = Label(parent=self.app, text="Caption", width=180, height=35)
        self.slider = Slider(parent=self.app, value=25, top=60, width=180, height=35)
        arrange(self.app, self.host)
        self.frame()

    def frame(self):
        paint_tree(self.app, self.host, retained=self.retained)

    def test_explicit_invalidation_repaints_only_the_affected_cached_body(self):
        before = self.calls.copy()
        self.frame()
        self.assertEqual(self.calls, before)
        self.label.invalidate()
        self.frame()
        self.assertEqual(self.calls[self.label], before[self.label] + 1)
        self.assertEqual(self.calls[self.slider], before[self.slider])
        self.assertIn(self.retained._records[self.label].identity, self.host.updates)
        self.assertNotIn(self.retained._records[self.slider].identity, self.host.updates)

    def test_new_mutable_private_state_uses_the_same_invalidation_contract(self):
        self.label._private_captions = ["Before"]
        self.label.invalidate()
        self.frame()
        before = self.host.snapshot()
        slider_calls = self.calls[self.slider]
        self.label._private_captions.append("After")
        self.label.invalidate()
        self.frame()
        actual = self.host.snapshot()
        self.assertNotEqual(actual, before)
        self.assertEqual(self.calls[self.slider], slider_calls)
        paint_tree(self.app, self.host)
        self.assertEqual(actual, tuple(self.host._drawing))

    def test_uncached_builtin_runs_on_every_requested_frame(self):
        self.label.cache_paint = False
        before = self.calls.copy()
        for _ in range(3):
            self.frame()
        self.assertEqual(self.calls[self.label], before[self.label] + 3)
        self.assertEqual(self.calls[self.slider], before[self.slider])
