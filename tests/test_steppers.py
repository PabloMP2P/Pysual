"""Step navigation, content ownership, portable layout, and runtime integration."""

import asyncio
from math import isfinite
from typing import get_type_hints
from unittest.mock import patch

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost, eventually
from test_retained_paint import PatchRecordingHost
from test_theme_catalog import contrast

from pysual import (
    App, Button, Container, Label, Step, Stepper, Style, SubWindow, Theme, blueprint,
    get_theme, registered_controls, theme_names,
)
from pysual.errors import LifecycleError
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, paint_tree
from pysual.runtime import Runtime


class StepHost(RecordingHost):
    def __init__(self):
        super().__init__()
        self.positions = []
        self.rectangles = []
        self.lines = []

    def text(self, text, x, y, *args, **kwargs):
        self.positions.append((text, x, y))
        super().text(text, x, y, *args, **kwargs)

    def rect(self, rect, *args, **kwargs):
        self.rectangles.append(rect)

    def line(self, x1, y1, x2, y2, *args, **kwargs):
        self.lines.append((x1, y1, x2, y2))


class StepperForm(Stepper):
    account = blueprint(Step, title="Account")
    review = blueprint(Step, title="Review")


class StepperTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=900, height=700)
        self.host = StepHost()
        self.host.size = (900, 700)
        self.addCleanup(self.app.destroy)

    def stepper(self, **properties):
        return Stepper(parent=self.app, left=10, top=10, width=720,
                       height=500, padding=0, **properties)

    def paint(self):
        arrange(self.app, self.host)
        self.host.texts.clear()
        self.host.positions.clear()
        paint_tree(self.app, self.host)

    def point(self, step):
        self.paint()
        _, x, y = next(item for item in self.host.positions if item[0] == step.title)
        return x + 2, y + 2

    def pointer(self, owner, kind, point, *, pointer_id=7, button=1):
        owner.handle_input(Input(kind, *point, pointer_id=pointer_id, button=button))

    def test_linear_progress_requires_explicit_completion_and_skips_optional_steps(self):
        owner = self.stepper()
        first = Step(parent=owner, title="Account")
        optional = Step(parent=owner, title="Preferences", optional=True)
        last = Step(parent=owner, title="Review")
        self.assertEqual(owner.steps, (first, optional, last))
        self.assertIs(owner.selected_step, first)
        self.assertFalse(owner.can_select(1))
        self.assertFalse(owner.next())
        self.assertFalse(first.completed)
        first.completed = True
        self.assertTrue(owner.next())
        self.assertIs(owner.selected_step, optional)
        self.assertTrue(owner.next())
        self.assertIs(owner.selected_step, last)
        self.assertFalse(optional.completed)
        self.assertFalse(owner.next())
        self.assertFalse(last.completed)
        self.assertTrue(owner.previous())
        self.assertIs(owner.selected_step, optional)

    def test_validation_result_updates_feedback_and_linear_navigation(self):
        owner = self.stepper()
        account = Step(parent=owner, title="Account")
        Step(parent=owner, title="Review")
        self.assertIs(account.set_validation("Enter a name", show_error=False), False)
        self.assertEqual((account.completed, account.error), (False, ""))
        self.assertFalse(owner.can_select(1))

        self.assertIs(account.set_validation("Enter a name"), False)
        self.assertEqual(account.error, "Enter a name")
        self.assertIs(account.set_validation("Use at least two letters", show_error=False), False)
        self.assertEqual(account.error, "Use at least two letters")
        self.assertFalse(owner.can_select(1))

        self.assertIs(account.set_validation("", show_error=False), True)
        self.assertEqual((account.completed, account.error), (True, ""))
        self.assertTrue(owner.can_select(1))
        self.assertIs(account.set_validation("Enter a name", show_error=False), False)
        self.assertEqual((account.completed, account.error), (False, ""))
        self.assertFalse(owner.can_select(1))
        self.assertIs(account.set_validation(""), True)
        self.assertTrue(owner.can_select(1))
        self.assertIs(owner.selected_step, account)

    def test_validation_commits_both_fields_before_hooks_and_rejects_bad_arguments(self):
        observed = []

        class ObservedStep(Step):
            def _changed(self, field, old, value):
                if field.name in ("completed", "error"):
                    observed.append((self.completed, self.error))
                super()._changed(field, old, value)

        account = ObservedStep(parent=self.stepper(), error="Enter a name")
        observed.clear()
        self.assertTrue(account.set_validation(""))
        self.assertTrue(observed)
        self.assertEqual(set(observed), {(True, "")})
        observed.clear()
        for error, show_error in (
            (None, True), (None, False), (False, True), (False, False),
            (1, True), (1, False), ("Invalid", None), ("Invalid", 0), ("", "yes"),
        ):
            with self.subTest(error=error, show_error=show_error):
                with self.assertRaises(TypeError):
                    account.set_validation(error, show_error=show_error)
                self.assertEqual((account.completed, account.error), (True, ""))
                self.assertEqual(observed, [])

    def test_validation_rejects_a_destroyed_step(self):
        account = Step(parent=self.stepper())
        account.destroy()
        with self.assertRaises(LifecycleError):
            account.set_validation("")

    def test_error_disabled_and_noneditable_states_control_navigation(self):
        owner = self.stepper()
        first = Step(parent=owner, title="Account", completed=True, error="Fix the name")
        middle = Step(parent=owner, title="Details", completed=True)
        last = Step(parent=owner, title="Review")
        self.assertFalse(owner.select(2))
        first.error = ""
        self.assertTrue(owner.select(2))
        first.editable = False
        self.assertFalse(owner.can_select(0))
        middle.enabled = False
        self.assertFalse(owner.can_select(1))
        owner.selected_index = 0
        first.completed = False
        first.enabled = False
        self.assertFalse(owner.select(2))
        owner.linear = False
        self.assertTrue(owner.select(2))
        self.assertIs(owner.selected_step, last)
        self.app.enabled = False
        self.assertFalse(owner.select(0))
        self.assertIs(owner.selected_step, last)

    def test_atomic_updates_validate_bounds_literals_and_boolean_types(self):
        owner = self.stepper()
        first = Step(parent=owner)
        second = Step(parent=owner)
        for properties, error in (
            ({"orientation": "diagonal", "selected_index": 1}, ValueError),
            ({"orientation": "vertical", "selected_index": 2}, ValueError),
            ({"selected_index": -1}, ValueError),
            ({"selected_index": True}, TypeError),
            ({"linear": 1}, TypeError),
            ({"header_height": 0}, ValueError),
        ):
            with self.subTest(properties=properties), self.assertRaises(error):
                owner.update(**properties)
            self.assertEqual((owner.orientation, owner.selected_index), ("horizontal", 0))
            self.assertTrue(first.visible)
            self.assertFalse(second.visible)
        owner.update(orientation="vertical", selected_index=1)
        self.assertIs(owner.selected_step, second)
        with self.assertRaises(TypeError):
            first.update(title="Changed", completed=1)
        self.assertEqual(first.title, "Step")
        self.assertFalse(first.completed)

    def test_owned_visibility_preserves_child_state_and_only_paints_active_content(self):
        owner = self.stepper(linear=False)
        first = Step(parent=owner, title="First")
        second = Step(parent=owner, title="Second")
        original = Label(parent=first, text="Account content")
        hidden = Label(parent=first, text="Hidden child", visible=False)
        Label(parent=second, text="Review content")
        self.paint()
        self.assertIn("Account content", self.host.texts)
        self.assertNotIn("Review content", self.host.texts)
        with self.assertRaises(ValueError):
            second.visible = True
        owner.select(1)
        self.paint()
        self.assertNotIn("Account content", self.host.texts)
        self.assertIn("Review content", self.host.texts)
        self.assertTrue(original.visible)
        self.assertFalse(hidden.visible)
        owner.select(0)
        self.paint()
        self.assertIn("Account content", self.host.texts)
        self.assertNotIn("Hidden child", self.host.texts)

    def test_empty_add_remove_preserve_selection_identity_and_dispose_content(self):
        owner = self.stepper(linear=False)
        self.assertEqual(owner.steps, ())
        self.assertIsNone(owner.selected_step)
        self.assertFalse(owner.next())
        self.assertFalse(owner.previous())
        self.assertFalse(owner.can_select(0))
        self.assertFalse(owner.can_select(-1))
        self.assertFalse(owner.select(10))
        for index in (True, "0", 0.0):
            with self.assertRaises(TypeError):
                owner.can_select(index)
        first, middle, last = (Step(parent=owner, title=title)
                               for title in ("First", "Middle", "Last"))
        child = Button(parent=middle, text="Keep")
        owner.select(1)
        first.destroy()
        self.assertIs(owner.selected_step, middle)
        self.assertEqual(owner.selected_index, 0)
        self.assertTrue(middle.visible)
        middle.destroy()
        self.assertTrue(child._disposed)
        self.assertIs(owner.selected_step, last)
        self.assertTrue(last.visible)
        last.destroy()
        self.assertIsNone(owner.selected_step)
        self.assertEqual(owner.selected_index, 0)
        replacement = Step(parent=owner)
        self.assertIs(owner.selected_step, replacement)
        owner.destroy()
        self.assertTrue(replacement._disposed)
        with self.assertRaises(LifecycleError):
            owner.next()

    def test_rejected_children_and_blueprints_keep_ownership_independent(self):
        owner = self.stepper()
        borrowed = Label(text="Borrowed")
        self.addCleanup(borrowed.destroy)
        with self.assertRaises(TypeError):
            owner.add(borrowed)
        self.assertIsNone(borrowed.parent)
        self.assertFalse(borrowed._disposed)
        first, second = StepperForm(), StepperForm()
        self.addCleanup(first.destroy)
        self.addCleanup(second.destroy)
        self.assertEqual(first.steps, (first.account, first.review))
        first.account.completed = True
        first.next()
        self.assertIs(first.selected_step, first.review)
        self.assertIs(second.selected_step, second.account)
        self.assertFalse(second.account.completed)
        self.assertIsNot(first._press, second._press)
        account = first.account
        first.destroy()
        self.assertTrue(account._disposed)
        self.assertFalse(second.account._disposed)

    def test_pointer_selection_requires_matching_primary_release(self):
        owner = self.stepper(linear=False)
        steps = [Step(parent=owner, title=title) for title in ("First", "Middle", "Last")]
        middle, last = self.point(steps[1]), self.point(steps[2])
        self.pointer(owner, "pointer_up", last)
        self.pointer(owner, "pointer_down", last, button=3)
        self.pointer(owner, "pointer_up", last)
        self.assertEqual(owner.selected_index, 0)
        self.pointer(owner, "pointer_down", last)
        self.pointer(owner, "pointer_up", middle)
        self.assertEqual(owner.selected_index, 0)
        self.pointer(owner, "pointer_down", last)
        for kind in ("pointer_move", "pointer_down", "pointer_up", "pointer_cancel"):
            self.pointer(owner, kind, middle, pointer_id=8)
            self.assertTrue(owner._pressed)
            self.assertEqual(owner.selected_index, 0)
        self.pointer(owner, "pointer_up", last, button=3)
        self.assertTrue(owner._pressed)
        self.pointer(owner, "pointer_up", last)
        self.assertEqual(owner.selected_index, 2)
        self.assertFalse(owner._pressed)

    def test_cancel_blur_and_model_changes_cannot_retarget_a_press(self):
        owner = self.stepper(linear=False)
        first = Step(parent=owner, title="First")
        middle = Step(parent=owner, title="Middle")
        last = Step(parent=owner, title="Last")
        point = self.point(middle)
        for cancellation in ("blur", "pointer_cancel"):
            self.pointer(owner, "pointer_down", point)
            owner.handle_input(Input(cancellation, pointer_id=7))
            self.pointer(owner, "pointer_up", point)
            self.assertIs(owner.selected_step, first)
        self.pointer(owner, "pointer_down", point)
        middle.enabled = False
        self.pointer(owner, "pointer_up", point)
        self.assertIs(owner.selected_step, first)
        middle.enabled = True
        self.pointer(owner, "pointer_down", point)
        middle.destroy()
        self.pointer(owner, "pointer_up", self.point(last))
        self.assertIs(owner.selected_step, first)

    def test_keyboard_navigation_obeys_orientation_and_reachability(self):
        owner = self.stepper(linear=False)
        steps = [Step(parent=owner, title=str(index)) for index in range(4)]
        steps[1].enabled = False
        steps[2].update(completed=True, editable=False)
        owner.handle_input(Input("key_down", key="ArrowRight"))
        self.assertEqual(owner.selected_index, 3)
        owner.handle_input(Input("key_down", key="Home"))
        self.assertEqual(owner.selected_index, 0)
        owner.orientation = "vertical"
        owner.handle_input(Input("key_down", key="ArrowDown"))
        self.assertEqual(owner.selected_index, 3)
        owner.handle_input(Input("key_down", key="ArrowUp"))
        self.assertEqual(owner.selected_index, 0)
        owner.linear = True
        owner.handle_input(Input("key_down", key="End"))
        self.assertEqual(owner.selected_index, 0)
        owner.enabled = False
        owner.linear = False
        owner.handle_input(Input("key_down", key="End"))
        self.assertEqual(owner.selected_index, 0)

    def test_horizontal_and_vertical_headers_surround_only_active_content(self):
        owner = self.stepper(linear=False)
        steps = [Step(parent=owner, title=title, padding=0)
                 for title in ("First", "Middle", "Last")]
        for index, step in enumerate(steps):
            Label(parent=step, text=f"Content {index}", height=80)
        self.paint()
        headings = {text: (x, y) for text, x, y in self.host.positions
                    if text in ("First", "Middle", "Last")}
        self.assertEqual(headings["First"][1], headings["Middle"][1])
        self.assertLess(headings["First"][0], headings["Middle"][0])
        self.assertLess(headings["Middle"][0], headings["Last"][0])
        self.assertGreaterEqual(steps[0].bounds.y, owner.bounds.y + owner.header_height)
        owner.update(orientation="vertical", selected_index=1)
        self.paint()
        headings = {text: (x, y) for text, x, y in self.host.positions
                    if text in ("First", "Middle", "Last")}
        self.assertEqual(headings["First"][0], headings["Middle"][0])
        self.assertLess(headings["Middle"][1], steps[1].bounds.y)
        self.assertLessEqual(steps[1].bounds.bottom, headings["Last"][1])
        self.assertIn("Content 1", self.host.texts)
        self.assertNotIn("Content 0", self.host.texts)
        self.assertNotIn("Content 2", self.host.texts)

    def test_portable_paint_handles_optional_error_long_and_tiny_labels(self):
        owner = self.stepper(linear=False)
        first = Step(parent=owner, title="Account\nname")
        second = Step(parent=owner, title="Optional item", optional=True)
        third = Step(parent=owner, title="Review", error="Please fix this")
        self.paint()
        self.assertIn("Optional", self.host.texts)
        self.assertIn("Please fix this", self.host.texts)
        for orientation in ("horizontal", "vertical"):
            owner.orientation = orientation
            first.title = "Long\tlabel " * 100
            for width, height in ((0, 0), (1, 1), (10, 5), (180, 120)):
                owner.update(width=width, height=height)
                self.paint()
                self.assertTrue(all("\n" not in text for text in self.host.texts))
                self.assertTrue(all(rect.width >= 0 and rect.height >= 0
                                    for rect in self.host.rectangles))
                self.assertTrue(all(isfinite(value) for line in self.host.lines for value in line))

    def test_paint_checks_availability_once_per_header_including_active_error(self):
        for count in (3, 6):
            owner = self.stepper()
            steps = [Step(parent=owner, completed=True) for _ in range(count)]
            for orientation in ("horizontal", "vertical"):
                for error in ("", "Fix the name"):
                    with self.subTest(count=count, orientation=orientation, error=error):
                        owner.orientation = orientation
                        steps[0].error = error
                        arrange(self.app, self.host)
                        with patch.object(owner, "_can_request", wraps=owner._can_request) as check:
                            paint_tree(self.app, self.host)
                        self.assertEqual([call.args for call in check.call_args_list],
                                         [(index,) for index in range(count)])
            owner.destroy()

    def test_header_parts_and_active_error_follow_current_interaction_state(self):
        class ColorHost(RecordingHost):
            def __init__(self):
                super().__init__()
                self.ink = []
                self.surfaces = []
                self.circles = []

            def styled_rect(self, rect, style):
                self.surfaces.append((rect, style.fill))
                return True

            def rect(self, rect, fill, *args):
                self.circles.append((rect, fill))

            def text(self, text, x, y, color, size, mono):
                self.ink.append((text, color))

        states = ("normal", "hover", "pressed", "disabled")
        parts = ("header", "indicator", "error", "label", "description")
        colors = {(state, part): f"#{s + 1}{p + 1}4455"
                  for s, state in enumerate(states) for p, part in enumerate(parts)}
        theme = Theme()
        for (state, part), color in colors.items():
            theme = theme.styled(Stepper, Style(fill=color, foreground=color),
                                 part=part, state=state)
        owner = self.stepper(linear=False, theme_override=theme)
        first = Step(parent=owner, title="Account", optional=True)
        Step(parent=owner, title="Review")
        host = ColorHost()
        host.size = (900, 700)
        for orientation in ("horizontal", "vertical"):
            for error in ("", "Fix the name"):
                for state in states:
                    with self.subTest(orientation=orientation, error=error, state=state):
                        owner.orientation = orientation
                        first.update(error=error, enabled=state != "disabled")
                        owner._hover = state in ("hover", "pressed", "disabled")
                        owner._hot_step = first
                        owner._pressed = state in ("pressed", "disabled")
                        owner._armed_step = first
                        arrange(self.app, host)
                        host.ink.clear()
                        host.surfaces.clear()
                        host.circles.clear()
                        paint_tree(self.app, host)
                        headers, _ = owner._geometry(owner.content_bounds(owner.bounds))
                        self.assertIn((headers[0], colors[state, "header"]), host.surfaces)
                        indicator = colors[state, "error" if error else "indicator"]
                        self.assertTrue(any(rect.width == rect.height == 24 and fill == indicator
                                            and headers[0].contains(rect.x, rect.y)
                                            for rect, fill in host.circles))
                        self.assertIn(("Account", colors[state, "error" if error else "label"]),
                                      host.ink)
                        self.assertIn((error or "Optional",
                                       colors[state, "error" if error else "description"]), host.ink)

    def test_narrow_horizontal_active_error_is_readable_and_reserves_content_space(self):
        owner = self.stepper()
        owner.width = 375
        account = Step(parent=owner, title="Account")
        field = Label(parent=account, text="Name input", height=40)
        Step(parent=owner, title="Preferences", optional=True)
        Step(parent=owner, title="Review")
        self.paint()
        before_top = account.bounds.y
        before_height = owner.measure_available(self.host, width=375)[1]
        account.error = "Enter a workspace name"
        self.paint()
        feedback = [item for item in self.host.positions if item[0] == account.error]
        self.assertEqual(len(feedback), 1)
        _, error_x, error_y = feedback[0]
        self.assertEqual(account.bounds.y - before_top, 24)
        self.assertEqual(owner.measure_available(self.host, width=375)[1] - before_height, 24)
        self.assertGreaterEqual(error_y, owner.bounds.y + owner.header_height)
        self.assertLess(error_y, account.bounds.y)
        self.assertGreaterEqual(field.bounds.y, account.bounds.y)
        self.assertGreaterEqual(error_x, owner.bounds.x)
        self.assertLess(error_x, owner.bounds.right)
        owner.orientation = "vertical"
        self.paint()
        self.assertEqual(account.bounds.y, before_top)
        self.assertIn(account.error, self.host.texts)
        owner.orientation = "horizontal"
        account.error = ""
        self.paint()
        self.assertEqual(account.bounds.y, before_top)

    def test_public_exports_and_catalog_do_not_add_factory_aliases(self):
        catalog = registered_controls()
        aliases = registered_controls(factories_only=True)
        for name, kind in (("step", Step), ("stepper", Stepper)):
            self.assertIs(catalog[name], kind)
            self.assertNotIn(name, aliases)
            self.assertNotIn(name, vars(Container))
            self.assertIs(get_type_hints(kind)["visible"], bool)

    def test_hidden_step_metadata_refreshes_retained_parent_headers(self):
        host = PatchRecordingHost()
        runtime = Runtime(self.app, host)
        self.app._runtime = runtime
        self.addCleanup(setattr, self.app, "_runtime", None)
        retained = runtime._retained_paint = RetainedPaintTree(host)
        owner = self.stepper(linear=False)
        Step(parent=owner, title="Active")
        hidden = Step(parent=owner, title="Hidden")
        arrange(self.app, host)
        paint_tree(self.app, host, retained=retained, layout_changed=True)
        previous = host.snapshot()
        for properties in ({"title": "Renamed"}, {"optional": True},
                           {"completed": True}, {"error": "Fix details"}):
            hidden.update(**properties)
            arrange(self.app, host)
            paint_tree(self.app, host, retained=retained, layout_changed=True)
            current = host.snapshot()
            self.assertNotEqual(previous, current)
            paint_tree(self.app, host)
            self.assertEqual(current, tuple(host._drawing))
            self.assertFalse(hidden.visible)
            previous = current

    def test_error_glyph_contrast_and_label_size_across_themes_and_states(self):
        class ColorHost(RecordingHost):
            def __init__(self):
                super().__init__()
                self.ink = []
                self.surfaces = []

            def text(self, text, x, y, color, size, mono):
                self.ink.append((text, x, y, color, size))

            def rect(self, rect, fill, *args):
                self.surfaces.append((rect, fill))

        owner = self.stepper(linear=False)
        first = Step(parent=owner, title="Account")
        Step(parent=owner, title="Review")
        host = ColorHost()
        host.size = (900, 700)
        for theme in theme_names():
            owner.theme_override = get_theme(theme)
            for selected, enabled in ((0, True), (1, True), (1, False)):
                with self.subTest(theme=theme, selected=selected, enabled=enabled):
                    owner.selected_index = selected
                    first.update(enabled=enabled, error="")
                    arrange(self.app, host)
                    host.ink.clear()
                    paint_tree(self.app, host)
                    normal_size = next(size for text, _, _, _, size in host.ink
                                       if text == "Account")
                    first.error = "Fix the name"
                    arrange(self.app, host)
                    host.ink.clear()
                    host.surfaces.clear()
                    paint_tree(self.app, host)
                    _, x, y, ink, _ = next(item for item in host.ink if item[0] == "!")
                    circle_fill = next(fill for rect, fill in host.surfaces
                                       if rect.width == rect.height == 24 and rect.contains(x, y))
                    self.assertGreaterEqual(contrast(ink, circle_fill), 4.5,
                                            f"Error glyph {ink} on {circle_fill}")
                    error_size = next(size for text, _, _, _, size in host.ink
                                      if text == "Account")
                    self.assertEqual(error_size, normal_size)


class StepperRuntimeTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=640, height=400)
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.task = None
        self.owner = Stepper(parent=self.app, width=600, height=320, linear=False)
        self.first = Step(parent=self.owner, title="First")
        self.second = Step(parent=self.owner, title="Second")
        self.child = Button(parent=self.first, text="Nested action")

    async def asyncTearDown(self):
        if self.task is not None:
            self.app.close()
            await asyncio.wait_for(self.task, 2)
        elif not self.app._disposed:
            self.app.destroy()

    async def start(self):
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def test_selection_moves_hidden_focus_and_reports_user_origin(self):
        changes = []
        self.owner.changed.connect(lambda event: changes.append(
            (event.source, event.old_value, event.new_value, event.origin)))
        await self.start()
        self.child.focus()
        self.owner.selected_index = 1
        self.assertIs(self.runtime.router.focus, self.owner)
        self.runtime.router._tab(False)
        self.assertIsNot(self.runtime.router.focus, self.child)
        self.owner.focus()
        self.host.events = [Input("key_down", key="Home")]
        await eventually(lambda: self.owner.selected_index == 0)
        await eventually(lambda: len(changes) == 2)
        self.assertEqual(changes[-1], (self.owner, 1, 0, "user"))

    async def test_linear_indicator_clicks_validate_without_manual_completion(self):
        changes = []
        valid = False
        self.first.validating.connect(lambda event: valid)
        self.owner.changed.connect(lambda event: changes.append(
            (event.source, event.old_value, event.new_value, event.origin)))
        self.owner.linear = True
        await self.start()

        async def click_indicator(index, expected):
            previous = self.owner.selected_index
            count = len(changes)
            headers, _ = self.owner._geometry(self.owner.content_bounds(self.owner.bounds))
            header = headers[index]
            # The numbered circle is 24px wide with a 16px leading inset.
            x, y = header.x + 28, header.y + header.height / 2
            self.host.events.extend([
                Input("pointer_down", x, y), Input("pointer_up", x, y),
            ])
            await eventually(lambda: not self.host.events)
            await asyncio.sleep(0)
            self.assertEqual(self.owner.selected_index, expected)
            if expected != previous:
                await eventually(lambda: len(changes) > count)
                self.assertEqual(changes[-1], (self.owner, previous, expected, "user"))
            else:
                self.assertEqual(len(changes), count)

        for orientation in ("horizontal", "vertical"):
            with self.subTest(orientation=orientation):
                frame = self.host.frames
                self.owner.update(orientation=orientation, selected_index=0)
                self.first.update(completed=False, error="")
                self.owner.invalidate()
                await eventually(lambda: self.host.frames > frame)
                await click_indicator(1, 0)

                valid = True
                self.child.focus()
                await click_indicator(1, 1)
                self.assertTrue(self.first.completed)
                self.assertIs(self.runtime.router.focus, self.owner)
                self.assertFalse(self.first.visible)
                self.assertTrue(self.second.visible)
                await click_indicator(0, 0)

                valid = False
                await click_indicator(1, 0)
                self.assertEqual(self.first.error, "Check this step")
                valid = True
                self.second.enabled = False
                await click_indicator(1, 0)
                self.second.enabled = True
                self.first.completed = False
                valid = False
                await click_indicator(1, 0)

                valid = True
                await click_indicator(1, 1)
                valid = False

    async def test_modal_step_selection_rejection_is_atomic(self):
        await self.start()
        for parent in (self.app, self.first):
            dialog = SubWindow(parent=parent, visible=False)
            Button(parent=dialog, text="Dialog action")
            dialog.show(modal=True, owner=self.child)
            original_focus = self.runtime.router.focus
            original_revision = self.owner._paint_revision
            with self.assertRaises(LifecycleError):
                self.owner.selected_index = 1
            with self.assertRaises(LifecycleError):
                self.owner.update(orientation="vertical", selected_index=1)
            self.assertEqual((self.owner.orientation, self.owner.selected_index), ("horizontal", 0))
            self.assertTrue(self.first.visible)
            self.assertFalse(self.second.visible)
            self.assertEqual(self.owner._paint_revision, original_revision)
            self.assertIs(self.runtime.router.focus, original_focus)
            await dialog.close_async()
            dialog.destroy()
        self.assertTrue(self.owner.select(1))
