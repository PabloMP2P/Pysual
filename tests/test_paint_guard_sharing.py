"""Mixed built-in types share audits without keeping checks beyond a pass."""

from collections import Counter
from unittest.mock import patch

from _ui_testcase import UIOwnerTestCase

from pysual import Control, Label, Slider
from pysual import _paint_state
from pysual.schema import SchemaObject


class SharedGuardTests(UIOwnerTestCase):
    def controls(self):
        controls = Label(text="Caption"), Slider(value=25)
        for control in controls:
            self.addCleanup(control.destroy)
        return controls

    def assert_trusted(self, controls, trusted=True):
        for control in controls:
            self.assertEqual(_paint_state.custom_paint(control), not trusted)

    def test_shared_bases_are_audited_once_per_scope(self):
        controls = self.controls()
        with patch.object(_paint_state, "_namespace", wraps=_paint_state._namespace) as audit:
            with _paint_state.painting_scope():
                self.assert_trusted(controls)
                self.assert_trusted(controls)
            counts = Counter(call.args[0] for call in audit.call_args_list)
            self.assertEqual(counts[Control], 1)
            self.assertEqual(counts[SchemaObject], 1)
            self.assertTrue(all(count == 1 for count in counts.values()))

            with _paint_state.painting_scope():
                self.assert_trusted(controls)
            counts = Counter(call.args[0] for call in audit.call_args_list)
            self.assertTrue(all(count == 2 for count in counts.values()))

    def test_class_mutation_refreshes_shared_audits_within_the_scope(self):
        controls = self.controls()
        with patch.object(_paint_state, "_namespace", wraps=_paint_state._namespace) as audit:
            with _paint_state.painting_scope():
                self.assert_trusted(controls)
                with patch.object(Control, "paint", lambda self, painter: None):
                    self.assert_trusted(controls, trusted=False)
                    self.assert_trusted(controls, trusted=False)
                self.assert_trusted(controls)
            counts = Counter(call.args[0] for call in audit.call_args_list)
            # Original, replacement and restored implementations each get one
            # audit, shared by both concrete control types.
            self.assertEqual(counts[Control], 3)

    def test_plain_base_mutation_is_seen_by_the_next_scope(self):
        controls = self.controls()
        with _paint_state.painting_scope():
            self.assert_trusted(controls)
        # SchemaObject uses ordinary type, so this does not advance the control
        # metaclass epoch. A new scope must still observe it and its restoration.
        with patch.object(SchemaObject, "_drawing_constant", 7, create=True):
            with _paint_state.painting_scope():
                self.assert_trusted(controls, trusted=False)
        with _paint_state.painting_scope():
            self.assert_trusted(controls)
