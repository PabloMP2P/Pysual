"""Step prerequisites and awaited validation preserve identity and current state."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase

from pysual import (
    App, CheckBox, Container, DataGrid, GridColumn, GridRow, Label, Step, Stepper,
    TextBox, blueprint,
)
from pysual.errors import LifecycleError
from pysual.events import Dispatcher
from pysual.host import Input
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


class StepRequirementsTests(UIOwnerTestCase):
    def step(self, **properties):
        control = Step(**properties)
        self.addCleanup(control.destroy)
        return control

    def stepper(self):
        control = Stepper()
        self.addCleanup(control.destroy)
        return control

    def test_list_is_snapshotted_without_adopting_prerequisites(self):
        account = self.step()
        supplied = [account]
        review = self.step(requires=supplied)
        supplied.clear()
        self.assertEqual(review.requires, (account,))
        self.assertIsNone(account.parent)
        self.assertIsNone(review.parent)
        self.assertEqual(review.children, ())
        review.requires = ()
        self.assertEqual(review.requires, ())

    def test_bad_references_leave_existing_requirements_unchanged(self):
        account = self.step()
        review = self.step(requires=[account])
        label = Label()
        self.addCleanup(label.destroy)
        for value, error in (
            (None, TypeError), ({account}, TypeError), ("account", TypeError),
            ("", TypeError), (b"", TypeError),
            ([label], TypeError), ([account, account], ValueError), ([review], ValueError),
        ):
            with self.subTest(value=value), self.assertRaises(error):
                review.requires = value
            self.assertEqual(review.requires, (account,))
        disposed = self.step()
        disposed.destroy()
        with self.assertRaises(LifecycleError):
            review.requires = [disposed]
        self.assertEqual(review.requires, (account,))

    def test_transitive_cycle_is_rejected_without_changing_graph(self):
        first = self.step()
        second = self.step(requires=[first])
        third = self.step(requires=[second])
        with self.assertRaisesRegex(ValueError, "cycle"):
            first.requires = [third]
        self.assertEqual(first.requires, ())
        self.assertEqual(second.requires, (first,))
        self.assertEqual(third.requires, (second,))

    def test_failed_constructor_does_not_destroy_borrowed_steps(self):
        account = self.step()
        with self.assertRaises(ValueError):
            Step(requires=[account, account])
        self.assertFalse(account._disposed)
        self.assertIsNone(account.parent)

    def test_attached_requirements_must_be_prior_attached_siblings(self):
        owner, other = self.stepper(), self.stepper()
        account = self.step(parent=owner)
        review = self.step(parent=owner, requires=[account])
        foreign = self.step(parent=other)
        detached = self.step()
        for wrong in (foreign, detached):
            with self.subTest(wrong=wrong), self.assertRaisesRegex(ValueError, "same Stepper"):
                review.requires = [wrong]
            self.assertEqual(review.requires, (account,))
        details = self.step(parent=owner)
        review.requires = [account, details]
        self.assertEqual(review.requires, (account, details))

    def test_detached_graph_can_attach_in_dependency_order(self):
        account = self.step()
        review = self.step(requires=[account])
        owner = self.stepper()
        with self.assertRaisesRegex(ValueError, "first"):
            owner.add(review)
        self.assertIsNone(review.parent)
        self.assertNotIn(review, owner.children)
        owner.add(account)
        owner.add(review)
        self.assertEqual(review.requires, (account,))
        self.assertEqual(owner.steps, (account, review))

    def test_non_stepper_parent_cannot_host_referenced_steps(self):
        owner = Container()
        self.addCleanup(owner.destroy)
        account = self.step()
        review = self.step(requires=[account])
        with self.assertRaisesRegex(ValueError, "same Stepper"):
            owner.add(review)
        self.assertIsNone(review.parent)
        self.assertIsNone(account.parent)

    def test_destroyed_dependency_is_retained_without_ownership_transfer(self):
        owner = self.stepper()
        account = self.step(parent=owner)
        review = self.step(parent=owner, requires=[account])
        account.destroy()
        self.assertEqual(review.requires, (account,))
        self.assertIs(review.parent, owner)
        self.assertFalse(review._disposed)

    def test_blueprint_instances_wire_independent_live_references(self):
        class Flow(Stepper):
            account = blueprint(Step, title="Account")
            review = blueprint(Step, title="Review")

        first, second = Flow(), Flow()
        self.addCleanup(first.destroy)
        self.addCleanup(second.destroy)
        first.review.requires = [first.account]
        second.review.requires = [second.account]
        self.assertEqual(first.review.requires, (first.account,))
        self.assertEqual(second.review.requires, (second.account,))
        self.assertIsNot(first.account, second.account)
        self.assertNotIn("requires", Step.properties())
        with self.assertRaises(TypeError):
            blueprint(Step, requires=[first.account])
        with self.assertRaisesRegex(ValueError, "same Stepper"):
            first.review.requires = [second.account]


class StepValidationTests(AsyncUIOwnerTestCase):
    def setUp(self):
        self.step = Step()
        self.addCleanup(self.step.destroy)

    async def pending_validation(self, result=True):
        started, release = asyncio.Event(), asyncio.Event()

        async def validate(event):
            started.set()
            await release.wait()
            return result

        self.step.validating.connect(validate)
        task = asyncio.create_task(self.step.validate())
        await started.wait()
        return task, release

    async def test_no_handler_accepts_default_and_preserves_manual_hidden_error(self):
        self.assertTrue(await self.step.validate())
        self.assertEqual((self.step.completed, self.step.error), (True, ""))
        self.step.set_validation("Enter a name", show_error=False)
        self.assertFalse(await self.step.validate(show_error=False))
        self.assertEqual((self.step.completed, self.step.error), (False, ""))
        self.assertFalse(await self.step.validate())
        self.assertEqual(self.step.error, "Enter a name")
        self.step.set_validation("")
        self.assertTrue(await self.step.validate())
        self.assertEqual((self.step.completed, self.step.error), (True, ""))

    async def test_false_uses_generic_error_and_none_accepts(self):
        subscription = self.step.validating.connect(lambda event: False)
        self.assertFalse(await self.step.validate())
        self.assertEqual((self.step.completed, self.step.error), (False, "Check this step"))
        subscription.disconnect()
        self.step.validating.connect(lambda event: None)
        self.assertTrue(await self.step.validate())
        self.assertEqual((self.step.completed, self.step.error), (True, ""))

    async def test_explicit_rejection_updates_visible_error_during_quiet_validation(self):
        message = "Enter a name"

        def validate(event):
            event.reject(message)

        self.step.validating.connect(validate)
        self.assertFalse(await self.step.validate(show_error=False))
        self.assertEqual(self.step.error, "")
        self.assertFalse(await self.step.validate())
        self.assertEqual(self.step.error, message)
        message = "Use two letters"
        self.assertFalse(await self.step.validate(show_error=False))
        self.assertEqual(self.step.error, message)

    async def test_async_handler_is_awaited_before_completion(self):
        task, release = await self.pending_validation()
        self.assertFalse(self.step.completed)
        self.assertFalse(task.done())
        release.set()
        self.assertTrue(await task)
        self.assertTrue(self.step.completed)

    async def test_newer_validation_wins_over_late_rejection(self):
        started, release = asyncio.Event(), asyncio.Event()
        count = 0

        async def validate(event):
            nonlocal count
            count += 1
            if count == 1:
                started.set()
                await release.wait()
                event.reject("Old result")

        self.step.validating.connect(validate)
        old = asyncio.create_task(self.step.validate())
        await started.wait()
        self.assertTrue(await self.step.validate())
        release.set()
        self.assertFalse(await old)
        self.assertEqual((self.step.completed, self.step.error), (True, ""))

    async def test_manual_result_supersedes_pending_validation(self):
        task, release = await self.pending_validation()
        self.step.set_validation("Changed while checking", show_error=False)
        release.set()
        self.assertFalse(await task)
        self.assertEqual((self.step.completed, self.step.error), (False, ""))
        self.assertEqual(self.step._validation_error, "Changed while checking")

    async def test_manual_result_from_handler_is_not_overwritten_by_return_value(self):
        def validate(event):
            self.step.set_validation("A newer manual result")
            return True

        self.step.validating.connect(validate)
        self.assertFalse(await self.step.validate())
        self.assertEqual((self.step.completed, self.step.error), (False, "A newer manual result"))

    async def test_direct_error_change_supersedes_pending_validation(self):
        task, release = await self.pending_validation()
        self.step.error = "Changed during validation"
        release.set()
        self.assertFalse(await task)
        self.assertFalse(self.step.completed)
        self.assertEqual(self.step.error, "Changed during validation")

    async def test_direct_completion_change_supersedes_pending_rejection(self):
        task, release = await self.pending_validation(False)
        self.step.completed = True
        release.set()
        self.assertFalse(await task)
        self.assertTrue(self.step.completed)
        self.assertEqual(self.step.error, "")

    async def test_requirement_change_supersedes_pending_validation(self):
        prerequisite = Step()
        self.addCleanup(prerequisite.destroy)
        task, release = await self.pending_validation()
        self.step.requires = [prerequisite]
        release.set()
        self.assertFalse(await task)
        self.assertFalse(self.step.completed)

    async def test_rejected_completion_update_preserves_cached_validation_result(self):
        class GuardedStep(Step):
            def _validate_update(self, name, value):
                super()._validate_update(name, value)
                if name == "completed" and value:
                    raise ValueError("Completion rejected")

        guarded = GuardedStep()
        self.addCleanup(guarded.destroy)
        guarded.set_validation("Still invalid", show_error=False)
        with self.assertRaisesRegex(ValueError, "Completion rejected"):
            guarded.set_validation("")
        self.assertEqual((guarded.completed, guarded.error), (False, ""))
        self.assertFalse(await guarded.validate())
        self.assertEqual(guarded.error, "Still invalid")

    async def test_reset_discards_pending_result_and_cached_error(self):
        self.step.set_validation("Old error")
        task, release = await self.pending_validation(False)
        self.step._reset_validation()
        release.set()
        self.assertFalse(await task)
        self.assertEqual((self.step.completed, self.step.error), (False, ""))
        self.assertIsNone(self.step._validation_error)

    async def test_disposal_discards_pending_result(self):
        task, release = await self.pending_validation()
        self.step.destroy()
        release.set()
        self.assertFalse(await task)
        with self.assertRaises(LifecycleError):
            await self.step.validate()

    async def test_bad_show_error_does_not_invoke_handler(self):
        calls = []
        self.step.validating.connect(lambda event: calls.append(event))
        for value in (None, 1, "yes"):
            with self.subTest(value=value), self.assertRaises(TypeError):
                await self.step.validate(show_error=value)
        self.assertEqual(calls, [])
        self.assertFalse(self.step.completed)

    async def test_handler_error_propagates_without_completing_step(self):
        def validate(event):
            raise ValueError("Validation failed to run")

        self.step.validating.connect(validate)
        with self.assertRaisesRegex(ValueError, "failed to run"):
            await self.step.validate()
        self.assertFalse(self.step.completed)
        self.assertEqual(self.step.error, "")


class ObservedLiveStep(Step):
    def _initialize(self):
        super()._initialize()
        self._observed_errors = []

    def _changed(self, field, old, value):
        super()._changed(field, old, value)
        if field.name == "error":
            self._observed_errors.append(value)


class StepLiveValidationTests(AsyncUIOwnerTestCase):
    def setUp(self):
        self.owner = Stepper()
        self.step = ObservedLiveStep(parent=self.owner)
        self.nested = Container(parent=self.step)
        self.entry = TextBox(parent=self.nested)
        self.dispatcher = Dispatcher()
        self.owner._attach_dispatcher(self.dispatcher)

    async def asyncTearDown(self):
        await self.dispatcher.shutdown()
        self.owner.destroy()

    async def test_pristine_edits_are_quiet_then_correction_and_later_errors_are_live(self):
        calls = []

        def validate(event):
            calls.append(self.entry.text)
            if not self.entry.text.strip():
                event.reject("Enter a name")

        self.step.validating.connect(validate)
        self.entry.text = "First edit"
        self.entry.text = ""
        await self.dispatcher.drain()
        self.assertEqual(calls, [])
        self.assertEqual((self.step.completed, self.step.error), (False, ""))
        self.assertFalse(await self.step.validate())
        self.entry.text = "Corrected"
        await self.dispatcher.drain()
        self.assertEqual((self.step.completed, self.step.error), (True, ""))
        self.entry.text = "   "
        await self.dispatcher.drain()
        self.assertEqual((self.step.completed, self.step.error), (False, "Enter a name"))
        self.assertEqual(calls, ["", "Corrected", "   "])

    async def test_new_nested_editor_controls_participate_without_registration(self):
        wrapper = Container(parent=Container(parent=self.nested))
        agreement = CheckBox(parent=wrapper)
        self.step.validating.connect(lambda event: agreement.checked)
        self.assertFalse(await self.step.validate())
        agreement.checked = True
        await self.dispatcher.drain()
        self.assertEqual((self.step.completed, self.step.error), (True, ""))
        replacement = CheckBox(parent=wrapper, checked=True)
        agreement.destroy()
        agreement = replacement
        agreement.checked = False
        await self.dispatcher.drain()
        self.assertEqual((self.step.completed, self.step.error), (False, "Check this step"))

    async def test_same_turn_edits_coalesce_and_layout_changes_do_not_validate(self):
        calls = []
        self.step.validating.connect(lambda event: calls.append(self.entry.text))
        self.step.set_validation("Already shown")
        self.entry.width = 220
        self.entry.visible = False
        self.entry.visible = True
        self.nested.padding = 5
        self.step.invalidate()
        await self.dispatcher.drain()
        self.assertEqual(calls, [])
        self.entry.text = "a"
        self.entry.text = "ab"
        self.entry.text = "abc"
        await self.dispatcher.drain()
        self.assertEqual(calls, ["abc"])
        self.assertEqual(self.step.error, "")

    async def test_edits_during_async_validation_discard_old_feedback_and_check_latest(self):
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def validate(event):
            value = self.entry.text
            calls.append(value)
            if value == "old":
                started.set()
                await release.wait()
                event.reject("Outdated response")

        self.step.validating.connect(validate)
        self.step.set_validation("Enter a name")
        self.entry.text = "old"
        await started.wait()
        self.entry.text = "latest"
        release.set()
        await self.dispatcher.drain()
        self.assertEqual(calls, ["old", "latest"])
        self.assertEqual((self.step.completed, self.step.error), (True, ""))
        self.assertNotIn("Outdated response", self.step._observed_errors)

    async def test_explicit_validation_supersedes_pending_live_check(self):
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def validate(event):
            if self.entry.text == "old":
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()

        self.step.validating.connect(validate)
        self.step.set_validation("Enter a name")
        self.entry.text = "old"
        await started.wait()
        self.entry.text = "latest"
        self.assertTrue(await self.step.validate())
        await cancelled.wait()
        await self.dispatcher.drain()
        self.assertEqual((self.step.completed, self.step.error), (True, ""))
        self.assertIsNone(self.step._live_token)

    async def test_validator_normalization_does_not_trigger_a_validation_loop(self):
        calls = []

        async def validate(event):
            calls.append(self.entry.text)
            await asyncio.sleep(0)
            self.entry.text = self.entry.text.strip()

        self.step.validating.connect(validate)
        self.step.set_validation("Enter a name")
        self.entry.text = "  Corrected  "
        await self.dispatcher.drain()
        self.assertEqual(calls, ["  Corrected  "])
        self.assertEqual(self.entry.text, "Corrected")
        self.assertEqual((self.step.completed, self.step.error), (True, ""))

    async def test_reset_cancels_queued_work_and_restores_pristine_behavior(self):
        calls = []
        self.step.validating.connect(lambda event: calls.append(self.entry.text))
        self.step.set_validation("Enter a name")
        self.entry.text = "Queued edit"
        self.owner.reset()
        await self.dispatcher.drain()
        self.entry.text = "Pristine edit"
        await self.dispatcher.drain()
        self.assertEqual(calls, [])
        self.assertEqual((self.step.completed, self.step.error), (False, ""))
        self.assertIsNone(self.step._live_token)
        self.assertIsNone(self.step._live_task)

    async def test_reset_and_dispose_cancel_running_live_validation(self):
        for action in ("reset", "dispose"):
            with self.subTest(action=action):
                started, cancelled = asyncio.Event(), asyncio.Event()

                async def validate(event):
                    started.set()
                    try:
                        await asyncio.Future()
                    finally:
                        cancelled.set()

                subscription = self.step.validating.connect(validate)
                self.step.set_validation("Enter a name")
                self.entry.text = action
                await started.wait()
                self.owner.reset() if action == "reset" else self.step.destroy()
                await cancelled.wait()
                await self.dispatcher.drain()
                self.assertIsNone(self.step._live_token)
                self.assertIsNone(self.step._live_task)
                subscription.disconnect()

    async def test_dispatcher_reattachment_clears_queued_task_but_keeps_shown_error_tracking(self):
        calls = []
        self.step.validating.connect(lambda event: calls.append(self.entry.text))
        self.step.set_validation("Enter a name")
        self.entry.text = "Cancelled edit"
        self.owner._attach_dispatcher(None)
        await self.dispatcher.drain()
        self.assertEqual(calls, [])
        self.owner._attach_dispatcher(self.dispatcher)
        self.entry.text = "Reopened edit"
        await self.dispatcher.drain()
        self.assertEqual(calls, ["Reopened edit"])
        self.assertEqual((self.step.completed, self.step.error), (True, ""))

    async def test_edit_during_first_validation_discards_result_without_enabling_live_mode(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def validate(event):
            value = self.entry.text
            started.set()
            await release.wait()
            if not value:
                event.reject("Old empty value")

        self.step.validating.connect(validate)
        task = asyncio.create_task(self.step.validate())
        await started.wait()
        self.entry.text = "Corrected before the first result"
        release.set()
        self.assertFalse(await task)
        self.assertEqual((self.step.completed, self.step.error), (False, ""))
        self.assertFalse(self.step._live_validation)
        self.assertTrue(await self.step.validate())

    async def test_live_handler_error_propagates_and_next_edit_can_retry(self):
        fail = True

        def validate(event):
            if fail:
                raise ValueError("Live validation service failed")

        self.step.validating.connect(validate)
        self.step.set_validation("Enter a name")
        self.entry.text = "First edit"
        with self.assertRaisesRegex(ValueError, "Live validation service failed"):
            await self.dispatcher.drain()
        self.assertIsNone(self.step._live_token)
        fail = False
        self.entry.text = "Retry"
        await self.dispatcher.drain()
        self.assertEqual((self.step.completed, self.step.error), (True, ""))

    async def test_internal_validation_preserves_navigation_origin(self):
        origins = []
        self.step.validating.connect(lambda event: origins.append(event.origin))
        self.assertTrue(await self.step._validate(show_error=True, origin="user"))
        self.assertEqual(origins, ["user"])


class ObservedGridStep(Step):
    def _initialize(self):
        super()._initialize()
        self.grid_changes = []

    def _descendant_changed(self, event):
        if isinstance(event.source, DataGrid):
            self.grid_changes.append((
                event, event.source.selected_row, tuple(event.source._order),
            ))
        super()._descendant_changed(event)


class StepGridValidationTests(AsyncUIOwnerTestCase):
    def setUp(self):
        self.owner = Stepper()
        self.step = ObservedGridStep(parent=self.owner)
        self.grid = DataGrid(
            parent=Container(parent=self.step),
            columns=(GridColumn("quantity", "Quantity", kind="number", editable=True),),
            rows=(GridRow("a", (0,)), GridRow("b", (2,))),
            selected_key="a", sort_key="quantity",
        )
        self.dispatcher = Dispatcher()
        self.owner._attach_dispatcher(self.dispatcher)
        self.calls = []

    async def asyncTearDown(self):
        await self.dispatcher.shutdown()
        self.owner.destroy()

    def validate_quantity(self, event):
        value = self.grid.rows[0].cells[0]
        self.calls.append((value, event.origin))
        if value <= 0:
            event.reject("Enter a positive quantity")

    async def test_set_cell_repairs_live_error_with_explicit_origin_and_current_indexes(self):
        self.step.validating.connect(self.validate_quantity)
        self.assertFalse(await self.step.validate())
        before = self.grid.rows
        self.grid.set_cell("a", "quantity", 3, origin="system")
        await self.dispatcher.drain()
        self.assertEqual((self.step.completed, self.step.error), (True, ""))
        self.assertEqual(self.calls, [(0, "program"), (3, "system")])
        self.assertEqual(len(self.step.grid_changes), 1)
        event, selected, ordered = self.step.grid_changes[0]
        self.assertEqual((event.property_name, event.origin), ("rows", "system"))
        self.assertIs(event.old_value, before)
        self.assertIs(event.new_value, self.grid.rows)
        self.assertEqual(selected, GridRow("a", (3,)))
        self.assertEqual(ordered, (GridRow("b", (2,)), GridRow("a", (3,))))

        self.grid.set_cell("a", "quantity", 0, origin="user")
        await self.dispatcher.drain()
        self.assertEqual((self.step.completed, self.step.error),
                         (False, "Enter a positive quantity"))
        self.assertEqual(self.calls[-1], (0, "user"))
        self.grid.set_cell("a", "quantity", 1)
        await self.dispatcher.drain()
        self.assertEqual(self.calls[-1], (1, "program"))
        self.assertTrue(self.step.completed)
        self.grid.rows = (GridRow("a", (4,)), self.grid.rows[1])
        await self.dispatcher.drain()
        self.assertEqual(self.calls[-1], (4, "program"))

    async def test_grid_edit_discards_pending_success_for_old_data(self):
        self.grid.set_cell("a", "quantity", 1)
        started, release = asyncio.Event(), asyncio.Event()

        async def validate(event):
            quantity = self.grid.rows[0].cells[0]
            started.set()
            await release.wait()
            if quantity <= 0:
                event.reject("Enter a positive quantity")

        self.step.validating.connect(validate)
        task = asyncio.create_task(self.step.validate())
        try:
            await started.wait()
            self.grid.set_cell("a", "quantity", 0)
        finally:
            release.set()
        self.assertFalse(await task)
        self.assertEqual((self.step.completed, self.step.error), (False, ""))
        self.assertFalse(await self.step.validate())
        self.assertEqual(self.step.error, "Enter a positive quantity")

    async def test_row_replacement_apis_each_revalidate_committed_data_once(self):
        self.step.validating.connect(self.validate_quantity)
        self.assertFalse(await self.step.validate())
        for method, value in (("rows", 1), ("set_data", 0), ("update_rows", 3)):
            with self.subTest(method=method):
                replacement = GridRow("a", (value,))
                rows = (replacement, self.grid.rows[1])
                self.step.grid_changes.clear()
                call_count = len(self.calls)
                if method == "rows":
                    self.grid.rows = rows
                elif method == "set_data":
                    self.grid.set_data(columns=self.grid.columns, rows=rows)
                else:
                    self.grid.update_rows((replacement,))
                await self.dispatcher.drain()
                self.assertEqual(self.step.completed, value > 0)
                self.assertEqual(len(self.calls), call_count + 1)
                self.assertEqual(len(self.step.grid_changes), 1)
                event, selected, ordered = self.step.grid_changes[0]
                self.assertEqual(event.property_name, "rows")
                self.assertEqual(event.new_value, rows)
                self.assertEqual(selected, replacement)
                self.assertEqual(ordered, tuple(sorted(rows, key=lambda row: row.cells[0])))

    async def test_invalid_and_unchanged_updates_are_quiet_and_selection_is_not_rows(self):
        self.step.validating.connect(self.validate_quantity)
        self.assertFalse(await self.step.validate())
        self.grid.set_cell("a", "quantity", 0)
        self.grid.rows = self.grid.rows
        self.grid.set_data(columns=self.grid.columns, rows=self.grid.rows)
        self.grid.update_rows((self.grid.rows[0],))
        with self.assertRaises(TypeError):
            self.grid.set_cell("a", "quantity", "invalid")
        with self.assertRaises(ValueError):
            self.grid.set_data(columns=(), rows=self.grid.rows)
        await self.dispatcher.drain()
        self.assertEqual(self.calls, [(0, "program")])
        self.assertEqual(self.step.grid_changes, [])

        self.grid.selected_key = "b"
        await self.dispatcher.drain()
        self.assertEqual(len(self.step.grid_changes), 1)
        self.assertEqual(self.step.grid_changes[0][0].property_name, "selected_key")
        self.assertEqual(self.calls, [(0, "program"), (0, "program")])

    async def test_in_place_editor_enter_commit_repairs_live_error(self):
        app, host = App(width=640, height=480), RecordingHost()
        app.add(self.owner)
        runtime = Runtime(app, host)
        task = asyncio.create_task(runtime.main())
        try:
            await eventually(lambda: app.is_open or task.done())
            if task.done():
                await task
            self.step.validating.connect(self.validate_quantity)
            self.assertFalse(await self.step.validate())
            self.grid.focus()
            host.events.append(Input("key_down", key="F2"))
            await eventually(lambda: runtime.popup is not None or task.done())
            if task.done():
                await task
            popup = runtime.popup
            popup.children[0].text = "4"
            host.events.append(Input("key_down", key="Enter"))
            await eventually(lambda: runtime.popup is None or task.done())
            if task.done():
                await task
            await runtime.dispatcher.drain()
            self.assertEqual(self.grid.rows[0].cells[0], 4)
            self.assertEqual((self.step.completed, self.step.error), (True, ""))
            self.assertEqual(self.calls, [(0, "program"), (4, "user")])
            self.assertEqual(len(self.step.grid_changes), 1)
            self.assertEqual(self.step.grid_changes[0][0].origin, "user")
        finally:
            runtime._stop.set()
            await task
            app.destroy()

