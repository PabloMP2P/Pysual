"""Composed press gestures work without inheriting a built-in button."""

import threading

from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Button, ColorPicker, Control, Dropdown, NumericInput, PressBehavior
from pysual.errors import LifecycleError
from pysual.host import Input
from pysual.layout import arrange


class _PressControl(Control):
    _press_keys = ("Enter", "Space")

    def _initialize(self):
        super()._initialize()
        self._press = PressBehavior(keys=self._press_keys)
        self._actions = []

    def _hit_part(self, event):
        return None

    def handle_input(self, event, /):
        part = self._hit_part(event)
        if self._press.handle_input(self, event, part=part):
            self._actions.append((part, self._pressed))

    def destroy(self):
        self._press.reset(self)
        super().destroy()


class _SplitPressControl(_PressControl):
    def _hit_part(self, event):
        return "left" if event.x < self.bounds.x + self.bounds.width / 2 else "right"


class _PointerPressControl(_PressControl):
    _press_keys = ()


class _CustomKeyPressControl(_PressControl):
    _press_keys = ("F6",)


class _CountingButton(Button):
    def _initialize(self):
        super()._initialize()
        self._actions = []

    def activate(self):
        self._actions.append(self._pressed)


class _CountingDropdown(Dropdown):
    def _initialize(self):
        super()._initialize()
        self._actions = []

    def open(self):
        self._actions.append(self._pressed)


class _CountingColorPicker(ColorPicker):
    def _initialize(self):
        super()._initialize()
        self._actions = []

    def edit(self):
        self._actions.append(self._pressed)


class _CountingNumericInput(NumericInput):
    def _initialize(self):
        super()._initialize()
        self._actions = []

    def edit(self):
        self._actions.append(self._pressed)


class PressBehaviorTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=400, height=240)
        self.host = RecordingHost()
        self.addCleanup(self.app.destroy)

    def control(self, kind=_PressControl, **properties):
        owner = kind(parent=self.app, left=20, top=20, width=180, height=40,
                     **properties)
        arrange(self.app, self.host)
        return owner

    def pointer(self, owner, kind, *, x=90, y=20, button=1, pointer_id=7):
        owner.handle_input(Input(kind, owner.bounds.x + x, owner.bounds.y + y,
                                 button=button, pointer_id=pointer_id))

    def test_custom_control_activates_once_after_release_and_clears_visual_state(self):
        owner = self.control()
        self.assertNotIsInstance(owner, Button)
        self.pointer(owner, "pointer_up")
        self.assertEqual(owner._actions, [])
        self.pointer(owner, "pointer_down")
        self.assertTrue(owner._pressed)
        self.assertEqual(owner._actions, [])
        self.pointer(owner, "pointer_up")
        self.assertEqual(owner._actions, [(None, False)])
        self.pointer(owner, "pointer_up")
        self.assertEqual(owner._actions, [(None, False)])

    def test_gesture_state_is_independent_for_each_control(self):
        first, second = self.control(), self.control()
        self.pointer(first, "pointer_down")
        second.handle_input(Input("key_down", key="Space"))
        self.pointer(second, "pointer_up")
        self.assertEqual(second._actions, [])
        self.assertTrue(first._pressed)
        second.handle_input(Input("key_up", key="Space"))
        self.assertEqual(second._actions, [(None, False)])
        self.assertTrue(first._pressed)
        self.pointer(first, "pointer_up")
        self.assertEqual(first._actions, [(None, False)])

    def test_visual_transitions_invalidate_paint_without_changing_model_or_measure(self):
        owner = self.control()
        properties = owner._values.copy()
        measured = owner._measure_revision
        revision = owner._paint_revision
        self.pointer(owner, "pointer_down")
        self.assertGreater(owner._paint_revision, revision)
        revision = owner._paint_revision
        self.pointer(owner, "pointer_move")
        self.assertEqual(owner._paint_revision, revision)
        self.pointer(owner, "pointer_move", x=-10)
        self.assertFalse(owner._pressed)
        self.assertGreater(owner._paint_revision, revision)
        revision = owner._paint_revision
        self.pointer(owner, "pointer_move")
        self.assertTrue(owner._pressed)
        self.assertGreater(owner._paint_revision, revision)
        self.pointer(owner, "pointer_up")
        self.assertEqual(owner._actions, [(None, False)])
        self.assertEqual(owner._values, properties)
        self.assertEqual(owner._measure_revision, measured)

    def test_secondary_button_cannot_start_or_complete_a_primary_gesture(self):
        owner = self.control()
        self.pointer(owner, "pointer_down", button=3)
        self.pointer(owner, "pointer_up")
        self.assertFalse(owner._pressed)
        self.assertEqual(owner._actions, [])
        self.pointer(owner, "pointer_down")
        self.pointer(owner, "pointer_up", button=3)
        self.assertTrue(owner._pressed)
        self.assertEqual(owner._actions, [])
        self.pointer(owner, "pointer_up")
        self.assertEqual(owner._actions, [(None, False)])

    def test_other_pointer_cannot_replace_cancel_move_or_release_an_active_gesture(self):
        owner = self.control()
        self.pointer(owner, "pointer_down")
        for kind in ("pointer_down", "pointer_move", "pointer_cancel", "pointer_up"):
            with self.subTest(kind=kind):
                self.pointer(owner, kind, pointer_id=8, x=-10 if kind == "pointer_move" else 90)
                self.assertTrue(owner._pressed)
                self.assertEqual(owner._actions, [])
        self.pointer(owner, "pointer_up")
        self.assertEqual(owner._actions, [(None, False)])

    def test_outside_press_and_outside_release_do_not_activate(self):
        owner = self.control()
        self.pointer(owner, "pointer_down", x=-10)
        self.pointer(owner, "pointer_up")
        self.assertEqual(owner._actions, [])
        self.pointer(owner, "pointer_down")
        self.pointer(owner, "pointer_up", y=50)
        self.assertFalse(owner._pressed)
        self.pointer(owner, "pointer_up")
        self.assertEqual(owner._actions, [])

    def test_different_hit_part_cancels_release_but_moving_back_can_complete(self):
        owner = self.control(_SplitPressControl)
        self.pointer(owner, "pointer_down", x=10)
        self.pointer(owner, "pointer_move", x=150)
        self.assertFalse(owner._pressed)
        self.pointer(owner, "pointer_up", x=150)
        self.pointer(owner, "pointer_up", x=10)
        self.assertEqual(owner._actions, [])
        self.pointer(owner, "pointer_down", x=10)
        self.pointer(owner, "pointer_move", x=150)
        self.pointer(owner, "pointer_move", x=10)
        self.assertTrue(owner._pressed)
        self.pointer(owner, "pointer_up", x=10)
        self.assertEqual(owner._actions, [("left", False)])

    def test_blur_and_pointer_cancel_require_a_fresh_press(self):
        for kind in ("blur", "pointer_cancel"):
            with self.subTest(kind=kind):
                owner = self.control()
                self.pointer(owner, "pointer_down")
                self.pointer(owner, kind)
                self.assertFalse(owner._pressed)
                self.pointer(owner, "pointer_up")
                self.assertEqual(owner._actions, [])
                self.pointer(owner, "pointer_down")
                self.pointer(owner, "pointer_up")
                self.assertEqual(owner._actions, [(None, False)])

    def test_keyboard_repeat_and_other_key_release_cannot_double_activate(self):
        for key, other in (("Enter", "Space"), ("Space", "Enter")):
            with self.subTest(key=key):
                owner = self.control()
                owner.handle_input(Input("key_up", key=key))
                owner.handle_input(Input("key_down", key=key))
                revision = owner._paint_revision
                for _ in range(3):
                    owner.handle_input(Input("key_down", key=key))
                owner.handle_input(Input("key_down", key=other))
                owner.handle_input(Input("key_up", key=other))
                self.assertTrue(owner._pressed)
                self.assertEqual(owner._paint_revision, revision)
                self.assertEqual(owner._actions, [])
                owner.handle_input(Input("key_up", key=key))
                owner.handle_input(Input("key_up", key=key))
                self.assertEqual(owner._actions, [(None, False)])

    def test_blur_cancels_keyboard_activation(self):
        owner = self.control()
        owner.handle_input(Input("key_down", key="Space"))
        owner.handle_input(Input("blur"))
        owner.handle_input(Input("key_up", key="Space"))
        self.assertFalse(owner._pressed)
        self.assertEqual(owner._actions, [])

    def test_overlapping_pointer_and_keyboard_presses_complete_independently(self):
        for first in ("pointer", "keyboard"):
            with self.subTest(first=first):
                owner = self.control()
                self.pointer(owner, "pointer_down")
                owner.handle_input(Input("key_down", key="Space"))

                def release(source):
                    if source == "pointer":
                        self.pointer(owner, "pointer_up")
                    else:
                        owner.handle_input(Input("key_up", key="Space"))

                release(first)
                release(first)
                self.assertTrue(owner._pressed)
                self.assertEqual(owner._actions, [(None, True)])
                release("keyboard" if first == "pointer" else "pointer")
                self.assertFalse(owner._pressed)
                self.assertEqual(owner._actions, [(None, True), (None, False)])

    def test_keyboard_policy_can_be_disabled_or_configured(self):
        pointer_only = self.control(_PointerPressControl)
        custom_key = self.control(_CustomKeyPressControl)
        for owner in (pointer_only, custom_key):
            for key in ("Enter", "Space"):
                owner.handle_input(Input("key_down", key=key))
                owner.handle_input(Input("key_up", key=key))
            self.assertFalse(owner._pressed)
            self.assertEqual(owner._actions, [])
        custom_key.handle_input(Input("key_down", key="F6"))
        custom_key.handle_input(Input("key_up", key="F6"))
        self.assertEqual(custom_key._actions, [(None, False)])
        self.pointer(pointer_only, "pointer_down")
        self.pointer(pointer_only, "pointer_up")
        self.assertEqual(pointer_only._actions, [(None, False)])

    def test_destroy_resets_state_and_disposed_owner_rejects_input(self):
        owner = self.control()
        self.pointer(owner, "pointer_down")
        owner.handle_input(Input("key_down", key="Space"))
        owner.destroy()
        self.assertFalse(owner._pressed)
        owner.destroy()
        with self.assertRaises(LifecycleError):
            owner.handle_input(Input("key_up", key="Space"))
        self.assertEqual(owner._actions, [])

    def test_reset_from_another_thread_rejects_before_clearing_active_gesture(self):
        owner = self.control()
        self.pointer(owner, "pointer_down")
        revision = owner._paint_revision
        behavior = owner._press
        errors = []

        def reset():
            try:
                behavior.reset(owner)
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=reset, daemon=True)
        worker.start()
        # A regression must fail promptly, even if it tries to dispatch back
        # to this owner while the test is waiting for the worker.
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive(), "Reset attempted to dispatch to the UI owner")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], LifecycleError)
        self.assertIn("UI owner", str(errors[0]))
        self.assertTrue(owner._pressed)
        self.assertEqual(owner._paint_revision, revision)
        self.pointer(owner, "pointer_up")
        self.assertEqual(owner._actions, [(None, False)])

    def test_all_migrated_controls_cancel_and_release_before_invoking_action(self):
        for kind in (_CountingButton, _CountingDropdown, _CountingColorPicker,
                     _CountingNumericInput):
            with self.subTest(control=kind.__name__):
                owner = self.control(kind)
                for cancel in ("blur", "pointer_cancel"):
                    self.pointer(owner, "pointer_down")
                    self.assertTrue(owner._pressed)
                    self.pointer(owner, cancel)
                    self.pointer(owner, "pointer_up")
                    self.assertFalse(owner._pressed)
                    self.assertEqual(owner._actions, [])
                self.pointer(owner, "pointer_down")
                self.pointer(owner, "pointer_up", pointer_id=8)
                self.assertEqual(owner._actions, [])
                self.pointer(owner, "pointer_up")
                self.assertEqual(owner._actions, [False])
                self.pointer(owner, "pointer_down")
                owner.destroy()
                self.assertFalse(owner._pressed)

    def test_button_uses_release_activation_while_popup_controls_keep_key_down_commands(self):
        for kind in (_CountingButton, _CountingDropdown, _CountingColorPicker,
                     _CountingNumericInput):
            with self.subTest(control=kind.__name__):
                owner = self.control(kind)
                owner.handle_input(Input("key_down", key="Enter"))
                expected = [] if kind is _CountingButton else [False]
                self.assertEqual(owner._actions, expected)
                owner.handle_input(Input("key_up", key="Enter"))
                self.assertEqual(owner._actions, [False])

    def test_numeric_hit_parts_keep_value_edits_and_increment_commands_separate(self):
        owner = self.control(_CountingNumericInput, value=5, minimum=0, maximum=10)
        for press, release in ((10, 170), (170, 90), (90, 10)):
            self.pointer(owner, "pointer_down", x=press)
            self.pointer(owner, "pointer_up", x=release)
        self.assertEqual(owner.value, 5)
        self.assertEqual(owner._actions, [])
        for x, expected in ((10, 4), (170, 5)):
            self.pointer(owner, "pointer_down", x=x)
            self.pointer(owner, "pointer_up", x=x)
            self.assertEqual(owner.value, expected)
        self.pointer(owner, "pointer_down", x=90)
        self.pointer(owner, "pointer_up", x=90)
        self.assertEqual(owner._actions, [False])
        owner.handle_input(Input("key_down", key="ArrowUp", shift=True))
        self.assertEqual(owner.value, 10)
        owner.handle_input(Input("key_down", key="Home"))
        self.assertEqual(owner.value, 0)
        owner.handle_input(Input("key_down", key="End"))
        self.assertEqual(owner.value, 10)
