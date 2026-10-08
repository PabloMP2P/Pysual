import asyncio
import inspect
import json
import threading
import unittest
from dataclasses import dataclass

from pysual import (
    App,
    Button,
    ClickEvent,
    ClosingEvent,
    Container,
    Control,
    Dirty,
    Event,
    Popup,
    SubWindow,
    TextBox,
    UiEvent,
    prop,
    register_control,
)
from _support import exercise
from _ui_testcase import AsyncUIOwnerTestCase
from pysual.errors import BindingError, EventOverloadError, LifecycleError, SchemaError
from pysual.host import Input
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


@dataclass(frozen=True)
class ImmutableRecord:
    values: tuple[int, ...]


class PropertyTests(unittest.TestCase):
    def test_nested_numbers_are_finite_before_assignment_and_serialization(self):
        class Samples(Control):
            samples: tuple[tuple[float | None, ...], ...] = prop(default=())

        control = Samples(samples=((1.0, None), (2.0,)))
        field = Samples.properties()["samples"]
        try:
            accepted = control.samples
            encoded = field.encode(accepted)
            self.assertEqual(field.decode(json.loads(json.dumps(encoded))), accepted)
            for number in (float("nan"), float("inf"), float("-inf")):
                invalid = (accepted[0], (number,))
                with self.subTest(number=number):
                    for operation in (
                        lambda: Samples(samples=invalid),
                        lambda: setattr(control, "samples", invalid),
                        lambda: field.encode(invalid),
                        lambda: field.decode([[1.0, None], [number]]),
                    ):
                        with self.assertRaisesRegex(ValueError, "finite"):
                            operation()
                    self.assertIs(control.samples, accepted)
                    self.assertEqual(control.dirty, Dirty.NONE)
            control.samples = (accepted[0], (3.0,))
            self.assertIs(control.samples[0], accepted[0])
        finally:
            control.destroy()

    def test_shared_tuple_entries_keep_validation_for_every_replacement(self):
        class Records(Control):
            records: tuple[ImmutableRecord, ...] = prop(default=(), persist=False)

        first, second = ImmutableRecord((1,)), ImmutableRecord((2,))
        control = Records(records=(first, second))
        try:
            control.records = (first, ImmutableRecord((3,)))
            self.assertEqual(control.records[1].values, (3,))
            accepted = control.records
            for invalid in (
                (first, "wrong type"),
                (first, ImmutableRecord([4])),
                (*accepted, "new invalid entry"),
            ):
                with self.assertRaises((TypeError, SchemaError)):
                    control.records = invalid
                self.assertIs(control.records, accepted)
            control.records = (second, first)
            self.assertEqual(control.records, (second, first))
        finally:
            control.destroy()

    def test_constructor_and_assignment_share_validation(self):
        with self.assertRaisesRegex(TypeError, "did you mean 'width'"):
            Button(wdith=12)
        button = Button(width=12)
        for invalid in (True, "12", -1, float("inf"), float("nan")):
            with self.subTest(value=invalid):
                with self.assertRaises((TypeError, ValueError)):
                    button.width = invalid
                self.assertEqual(button.width, 12)
                self.assertEqual(button.dirty, Dirty.NONE)
        with self.assertRaisesRegex(AttributeError, "width"):
            button.wdith = 1
        button.width = 20
        self.assertTrue(button.dirty & Dirty.MEASURE)

    def test_custom_schema_inherits_and_is_immutable(self):
        class Badge(Control):
            count: int = prop(default=0, minimum=0)

        badge = Badge(count=2, width=10)
        self.assertEqual(badge.count, 2)
        self.assertIn("width", Badge.properties())
        with self.assertRaises(TypeError):
            Badge.properties()["count"] = None
        with self.assertRaises(ValueError):
            Badge(count=-1)
        with self.assertRaises(SchemaError):

            class Invalid(Badge):
                count: str = prop(default="")

    def test_unsupported_mutable_defaults_and_shadowing_fail(self):
        with self.assertRaises(SchemaError):

            class Bad(Control):
                items: list = prop(default=[])

        with self.assertRaises(SchemaError):

            class BadWidth(Control):
                width = 10

    def test_valid_overrides_survive_another_inheritance_level(self):
        class Sized(Control):
            width: float | None = prop(default=80.0, minimum=0)

        class Specialized(Sized):
            pass

        self.assertEqual(Specialized().width, 80.0)

    def test_thread_and_disposal_guards(self):
        button = Button()
        errors = []

        def worker():
            try:
                button.text = "worker update"
            except LifecycleError as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(button.text, "worker update")
        button.destroy()
        button.destroy()
        with self.assertRaises(LifecycleError):
            button.text = "stale"

    def test_render_property_reads_keep_thread_and_disposal_guards(self):
        from pysual.painting import resolve_style

        button = Button()
        reads = (
            lambda: button.text,
            lambda: button.effective_theme,
            lambda: button.effective_enabled,
            lambda: resolve_style(button),
        )
        errors = []

        def worker():
            for read in reads:
                try:
                    read()
                except LifecycleError:
                    errors.append(True)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(errors, [])
        button.destroy()
        for read in reads:
            with self.assertRaises(LifecycleError):
                read()

    def test_inherited_render_state_and_custom_enabled_policy(self):
        from pysual import dark, light

        class Policy(Container):
            @property
            def effective_enabled(self):
                return super().effective_enabled and self.allowed

        app = App(theme=dark())
        try:
            policy = Policy(parent=app)
            policy.allowed = False
            group = Container(parent=policy)
            button = Button(parent=group)
            self.assertFalse(button.effective_enabled)
            policy.allowed = True
            self.assertTrue(button.effective_enabled)
            app.enabled = False
            self.assertFalse(button.effective_enabled)
            self.assertIs(button.effective_theme, app.theme)
            group.theme_override = light()
            self.assertIs(button.effective_theme, group.theme_override)
            button.theme_override = dark()
            self.assertIs(button.effective_theme, button.theme_override)
            button.theme_override = group.theme_override = None
            app.theme = light()
            self.assertIs(button.effective_theme, app.theme)
        finally:
            app.destroy()

    def test_rejected_task_closes_its_coroutine(self):
        async def work():
            pass

        control = Control()
        for disposed in (False, True):
            if disposed:
                control.destroy()
            coroutine = work()
            with self.assertRaises(LifecycleError):
                control.create_task(coroutine)
            self.assertIsNone(coroutine.cr_frame)


class RuntimeInputTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=640, height=400)
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        if not self.task.done():
            self.runtime._stop.set()
            await asyncio.wait_for(self.task, 2)

    async def test_unawaited_close_failure_is_reported_by_runtime(self):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        unhandled = []
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        try:

            async def fail(event):
                raise ValueError("close failed")

            self.app.closing.connect(fail)
            self.app.close()
            with self.assertRaisesRegex(ValueError, "close failed"):
                await asyncio.wait_for(self.task, 2)
            await asyncio.sleep(0)
            self.assertEqual(unhandled, [])
            self.assertTrue(self.host.closed)
        finally:
            loop.set_exception_handler(previous)

    async def test_owner_cancellation_cannot_cancel_shared_app_close(self):
        for accept in (False, True):
            with self.subTest(accept=accept):
                entered, release = asyncio.Event(), asyncio.Event()
                decisions = []

                async def decide(event):
                    decisions.append(event)
                    entered.set()
                    await release.wait()
                    if not accept:
                        event.cancel()

                subscription = self.app.closing.connect(decide)
                owner = self.app.add(Control())

                async def close_from_owner():
                    return await self.app.close_async()

                first = owner.create_task(close_from_owner())
                await asyncio.wait_for(entered.wait(), 2)
                second = asyncio.create_task(self.app.close_async())
                await asyncio.sleep(0)
                shared = self.runtime._close_task
                owner.destroy()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                self.assertFalse(shared.done())
                self.assertFalse(second.done())
                release.set()
                self.assertEqual(await asyncio.wait_for(second, 2), accept)
                self.assertEqual(len(decisions), 1)
                self.assertEqual(self.runtime._stop.is_set(), accept)
                subscription.disconnect()

    async def test_vetoed_close_restores_owned_task_cancellation(self):
        resumed, cancelled = asyncio.Event(), asyncio.Event()
        subscription = self.app.closing.connect(lambda event: event.cancel())

        async def controller():
            for _ in range(3):
                self.assertFalse(await self.app.close_async())
            resumed.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = self.app.create_task(controller())
        try:
            await asyncio.wait_for(resumed.wait(), 2)
            subscription.disconnect()
            self.app.close()
            await asyncio.wait_for(self.task, 2)
            self.assertTrue(task.cancelled())
            self.assertTrue(cancelled.is_set())
        finally:
            task.cancel()

    async def test_cancelled_close_wait_restores_owned_task_cancellation(self):
        entered, release, resumed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def veto(event):
            entered.set()
            await release.wait()
            event.cancel()

        subscription = self.app.closing.connect(veto)

        async def controller():
            try:
                await self.app.close_async()
            except asyncio.CancelledError:
                resumed.set()
            await asyncio.Event().wait()

        task = self.app.create_task(controller())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.wait_for(resumed.wait(), 2)
            shared = self.runtime._close_task
            self.assertFalse(shared.done())
            release.set()
            self.assertFalse(await asyncio.wait_for(shared, 2))
            subscription.disconnect()
            self.app.close()
            await asyncio.wait_for(self.task, 2)
            self.assertTrue(task.cancelled())
        finally:
            release.set()
            task.cancel()

    async def test_capture_rejects_other_pointers_and_cancels_before_clearing_drag(
        self,
    ):
        class Draggable(Button):
            def _initialize(self):
                super()._initialize()
                self._drag = None
                self._cancelled = []

            def handle_input(self, event, /):
                if event.kind == "pointer_down":
                    self._drag = "started"
                elif event.kind == "blur" and self._drag is not None:
                    self._cancelled.append(self._drag)
                super().handle_input(event)

        first = Draggable(parent=self.app, width=120, height=40)
        second = Button(parent=self.app, left=160, width=120, height=40)
        self.runtime._paint_frame()
        router = self.runtime.router
        clicks = []
        first.click.connect(lambda event: clicks.append("first"))
        second.click.connect(lambda event: clicks.append("second"))
        router.process(Input("pointer_down", 10, 10, pointer_id=1))
        router.process(Input("pointer_move", 10, 10, pointer_id=1))
        for kind in (
            "pointer_down",
            "pointer_move",
            "pointer_up",
            "pointer_cancel",
            "pointer_leave",
        ):
            router.process(Input(kind, 170, 10, pointer_id=2))
            self.assertIs(router.capture, first)
            self.assertEqual(router.capture_id, 1)
            self.assertTrue(first._pressed)
            self.assertFalse(second._pressed)
            self.assertIs(router.hover, first)
        router.process(Input("pointer_up", 10, 10, pointer_id=1))
        await self.runtime.dispatcher.drain()
        self.assertEqual(clicks, ["first"])
        self.assertIsNone(router.capture_id)
        for kind in ("pointer_cancel", "blur"):
            router.process(Input("pointer_down", 10, 10, pointer_id=3))
            router.process(Input(kind, pointer_id=3))
            self.assertIsNone(router.capture)
            self.assertIsNone(router.capture_id)
            self.assertFalse(first._pressed)
            self.assertIsNone(first._drag)
            self.assertIsNone(router.hover)
        self.assertEqual(first._cancelled, ["started", "started"])
        await self.runtime.dispatcher.drain()
        self.assertEqual(clicks, ["first"])

    async def test_reconcile_drops_nonfocusable_focus_and_invalid_capture(self):
        button = Button(parent=self.app, width=120, height=40)
        text = TextBox(parent=self.app, top=60, width=120, height=40)
        self.runtime._paint_frame()
        router = self.runtime.router
        clicks = []
        button.click.connect(lambda event: clicks.append(event))
        router.set_focus(button)
        button.focusable = False
        router.process(Input("key_down", key="Space"))
        self.assertIsNone(router.focus)
        await self.runtime.dispatcher.drain()
        self.assertEqual(clicks, [])

        router.set_focus(text)
        router.process(Input("composition", text="pending"))
        text.focusable = False
        router.reconcile()
        self.assertIsNone(router.focus)
        self.assertIsNone(self.host.text_target)
        self.assertEqual(text._composition, "")

        for name in ("visible", "enabled", "destroy"):
            router.process(Input("pointer_down", 10, 10, pointer_id=4))
            router.reconcile()
            self.assertIs(router.capture, button)  # Pointer-only controls still work.
            if name == "destroy":
                button.destroy()
            else:
                setattr(button, name, False)
            router.reconcile()
            self.assertIsNone(router.capture)
            self.assertIsNone(router.capture_id)
            self.assertFalse(button._pressed)
            if name != "destroy":
                setattr(button, name, True)

    async def test_popup_and_modal_transitions_cancel_capture(self):
        anchor = Button(parent=self.app, width=120, height=40, focusable=False)
        self.runtime._paint_frame()
        router = self.runtime.router
        router.process(Input("pointer_down", 10, 10, pointer_id=7))
        popup = Popup(width=200, height=80)
        popup.show(anchor)
        self.assertIsNone(router.capture)
        self.assertIsNone(router.capture_id)
        self.assertFalse(anchor._pressed)
        inside = Button(parent=popup, width=120, height=40, focusable=False)
        self.runtime._paint_frame()
        router.process(
            Input(
                "pointer_down", inside.bounds.x + 10, inside.bounds.y + 10, pointer_id=8
            )
        )
        self.assertIs(router.capture, inside)
        popup.dismiss()
        self.assertIsNone(router.capture)
        self.assertIsNone(router.capture_id)
        self.assertFalse(inside._pressed)

        router.process(Input("pointer_down", 10, 10, pointer_id=9))
        dialog = SubWindow(parent=self.app, visible=False)
        opened = asyncio.create_task(self.runtime.open_modal(dialog))
        await asyncio.sleep(0)
        self.assertIsNone(router.capture)
        self.assertIsNone(router.capture_id)
        self.assertFalse(anchor._pressed)
        self.runtime.finish_modal(dialog, "done")
        self.assertEqual(await opened, "done")

    async def test_cancelled_capture_receives_one_blur_with_or_without_focus(self):
        class Traced(Button):
            def _initialize(self):
                super()._initialize()
                self._blurs = []
                self._drag = None

            def handle_input(self, event, /):
                if event.kind == "pointer_down":
                    self._drag = "pending"
                elif event.kind == "blur":
                    self._blurs.append(self._drag)
                super().handle_input(event)

        router = self.runtime.router
        for focusable in (True, False):
            for transition in (
                "blur",
                "visible",
                "enabled",
                "show",
                "dismiss",
                "modal",
            ):
                with self.subTest(focusable=focusable, transition=transition):
                    popup = None
                    anchor = Button(parent=self.app, left=200, width=120, height=40)
                    parent = self.app
                    if transition == "dismiss":
                        popup = Popup(width=200, height=80)
                        self.runtime._paint_frame()
                        popup.show(anchor)
                        parent = popup
                    control = Traced(
                        parent=parent, width=120, height=40, focusable=focusable
                    )
                    self.runtime._paint_frame()
                    router.process(
                        Input(
                            "pointer_down", control.bounds.x + 10, control.bounds.y + 10
                        )
                    )
                    self.assertIs(router.capture, control)
                    if transition == "blur":
                        router.process(Input("blur"))
                    elif transition in ("visible", "enabled"):
                        setattr(control, transition, False)
                        router.reconcile()
                    elif transition == "show":
                        popup = Popup(width=200, height=80)
                        popup.show(anchor)
                    elif transition == "dismiss":
                        popup.dismiss()
                    else:
                        dialog = SubWindow(parent=self.app, visible=False)
                        opened = asyncio.create_task(self.runtime.open_modal(dialog))
                        await asyncio.sleep(0)
                        self.runtime.finish_modal(dialog, None)
                        await opened
                        dialog.destroy()
                    self.assertEqual(control._blurs, ["pending"])
                    self.assertIsNone(control._drag)
                    self.assertIsNone(router.capture)
                    if popup is not None and popup.is_open:
                        popup.dismiss(restore_focus=False)
                    control.destroy()
                    anchor.destroy()


class OwnershipTests(unittest.TestCase):
    def test_independent_windows_across_classes(self):
        class First(App):
            pass

        class Second(App):
            pass

        first = First()
        peer = First()
        second = Second()
        self.assertIsNot(first, peer)
        self.assertIsNot(first, second)
        first.close()
        self.assertFalse(first._disposed)
        self.assertFalse(second._disposed)
        second.close()
        first.destroy()
        second.destroy()
        peer.destroy()

    def test_failed_constructor_releases_reservation(self):
        class Bad(App):
            def __init__(self):
                super().__init__()
                self.child = self.button()
                raise ValueError("construction failed")

        with self.assertRaises(ValueError):
            Bad()
        App().close()

    def test_first_assignment_alias_and_explicit_replacement(self):
        app = App()
        app.save = app.button()
        app.alias = app.save
        self.assertEqual(app.alias.name, "save")
        with self.assertRaises(BindingError):
            app.save = app.button()
        with self.assertRaises(BindingError):
            del app.save
        stale = app.save
        app.save.destroy()
        app.save = app.button()
        self.assertEqual(app.save.name, "save")
        with self.assertRaises(LifecycleError):
            stale.text = "bad"

    def test_collisions_and_wrong_tree(self):
        app = App()
        with self.assertRaisesRegex(BindingError, "distinct name"):
            app.button = app.button()
        with self.assertRaises(BindingError):
            app.other = Button(parent=Container())
        with self.assertRaises(BindingError):
            app.add(app)

    def test_factory_registration_is_concrete_and_idempotent(self):
        class LocalBadge(Control):
            count: int = prop(default=1, minimum=0)

        register_control(LocalBadge, name="contract_badge")
        register_control(LocalBadge, name="contract_badge")
        app = App()
        app.notice = app.contract_badge(count=5)
        self.assertIs(type(app.notice), LocalBadge)
        self.assertIs(app.notice.parent, app)
        signature = inspect.signature(app.contract_badge)
        self.assertIs(signature.return_annotation, LocalBadge)
        self.assertIs(signature.parameters["count"].annotation, int)
        with self.assertRaises(TypeError):
            app.contract_badge(cout=1)
        with self.assertRaises(BindingError):
            register_control(LocalBadge, name="build")

    def test_source_free_assignment(self):
        namespace = {"App": App, "Button": Button}
        exec(
            compile(
                "class Live(App):\n def build(self):\n  self.ok = Button()\n",
                "<no-file>",
                "exec",
            ),
            namespace,
        )
        app = namespace["Live"]()
        app.build()
        self.assertEqual(app.ok.name, "ok")

    def test_ambiguous_root_and_control_handler_pairs_are_rejected(self):
        class Demo(App):
            action_on_click = Event(ClickEvent)

        app = Demo()
        with self.assertRaisesRegex(BindingError, "Ambiguous"):
            app.Demo_on_action = app.button()

    def test_missing_super_in_custom_constructor_is_actionable(self):
        class Bad(App):
            def __init__(self):
                pass

        with self.assertRaisesRegex(LifecycleError, "super"):
            Bad()
        App().close()


class EventTests(AsyncUIOwnerTestCase):
    async def test_build_once_loaded_and_convention_order(self):
        trace = []

        class Demo(App):
            def build(self):
                trace.append("build")
                self.ok = self.button()
                self.ok.click.connect(lambda event: trace.append("explicit"))

            def Demo_on_loaded(self, event: UiEvent):
                trace.append(self.lifecycle_state)

            def ok_on_click(self, event: ClickEvent):
                trace.append("convention")

        app = Demo()
        self.assertEqual(trace, [])

        async def scenario(session):
            app.ok.click.emit(ClickEvent(source=app.ok))

        await exercise(app, scenario)
        self.assertEqual(trace, ["build", "RUNNING", "convention", "explicit"])
        # This internal event harness deliberately destroys its tree; native
        # close/reopen is covered by the public Window lifecycle tests.
        with self.assertRaises(LifecycleError):
            await exercise(app, scenario)

    async def test_repeated_async_events_all_delivered(self):
        started, completed = [], []
        barrier = asyncio.Event()

        class Demo(App):
            def build(self):
                self.ok = self.button()

            async def ok_on_click(self, event: ClickEvent):
                started.append(event.timestamp)
                await barrier.wait()
                completed.append(event.timestamp)

        app = Demo()

        async def scenario(session):
            for i in range(12):
                app.ok.click.emit(ClickEvent(source=app.ok, timestamp=float(i)))
            await asyncio.sleep(0)
            self.assertEqual(started, list(range(12)))
            self.assertEqual(completed, [])
            barrier.set()

        await exercise(app, scenario, max_pending_handlers=12)
        self.assertEqual(completed, list(range(12)))

    async def test_overload_reserves_complete_listener_snapshot(self):
        trace = []
        app = App()

        async def scenario(session):
            app.ok = app.button()
            app.ok.click.connect(lambda event: trace.append(1))
            app.ok.click.connect(lambda event: trace.append(2))
            app.ok.click.emit(ClickEvent(source=app.ok))

        with self.assertRaisesRegex(EventOverloadError, "incoming=2, limit=1"):
            await exercise(app, scenario, max_pending_handlers=1)
        self.assertEqual(trace, [])
        self.assertEqual(app.lifecycle_state, "DESTROYED")

    async def test_awaited_closing_cancel_is_sticky_and_sealed(self):
        events, trace = [], []

        class Demo(App):
            async def Demo_on_closing(self, event: ClosingEvent):
                events.append(event)
                await asyncio.sleep(0)
                event.cancel()
                trace.append("decision")

        app = Demo()

        async def scenario(session):
            app.closing.connect(lambda event: trace.append("after"))
            results = await asyncio.gather(
                session.request_close(), session.request_close()
            )
            self.assertEqual(results, [False, False])
            self.assertEqual(app.lifecycle_state, "RUNNING")
            self.assertEqual(len(events), 1)
            with self.assertRaises(LifecycleError):
                events[0].cancel()

        await exercise(app, scenario)
        self.assertEqual(trace, ["decision", "after"])

    async def test_handler_failure_reaches_caller_after_cleanup(self):
        app = App()

        async def scenario(session):
            app.ok = app.button()

            def broken(event):
                raise ValueError("handler failed")

            app.ok.click.connect(broken)
            app.ok.click.emit(ClickEvent(source=app.ok))

        with self.assertRaisesRegex(ValueError, "handler failed"):
            await exercise(app, scenario)
        self.assertEqual(app.lifecycle_state, "DESTROYED")
        App().close()

    async def test_binding_rejects_arity_and_wrong_annotation(self):
        app = App()

        async def scenario(session):
            app.ok = app.button()
            with self.assertRaises(BindingError):
                app.ok.click.connect(lambda: None)

            def wrong(event: ClosingEvent):
                pass

            with self.assertRaises(BindingError):
                app.ok.click.connect(wrong)

        await exercise(app, scenario)

    async def test_disposal_suppresses_queued_delivery(self):
        app, trace = App(), []

        async def scenario(session):
            app.ok = app.button()
            app.ok.click.connect(lambda event: trace.append(1))
            app.ok.click.emit(ClickEvent(source=app.ok))
            app.ok.destroy()

        await exercise(app, scenario)
        self.assertEqual(trace, [])

    async def test_pending_handler_cleanup_precedes_disposal(self):
        app, trace = App(), []

        async def scenario(session):
            app.ok = app.button()

            async def callback(event):
                try:
                    await asyncio.Event().wait()
                finally:
                    app.ok.text = "cleanup"
                    trace.append(app.ok.text)

            app.ok.click.connect(callback)
            app.ok.click.emit(ClickEvent(source=app.ok))
            await asyncio.sleep(0)
            raise ValueError("stop")

        with self.assertRaisesRegex(ValueError, "stop"):
            await exercise(app, scenario)
        self.assertEqual(trace, ["cleanup"])

    async def test_two_composites_have_independent_handler_scopes(self):
        trace = []

        class Composite(Container):
            def _initialize(self):
                super()._initialize()
                self.ok = self.button()

            def ok_on_click(self, event):
                trace.append(self)

        app = App()

        async def scenario(session):
            app.first = app.add(Composite())
            app.second = app.add(Composite())
            app.first.ok.click.emit(ClickEvent(source=app.first.ok))
            app.second.ok.click.emit(ClickEvent(source=app.second.ok))

        await exercise(app, scenario)
        self.assertEqual(len(trace), 2)
        self.assertIsNot(trace[0], trace[1])

    async def test_failed_build_releases_singleton(self):
        class Bad(App):
            def build(self):
                self.ok = self.button()
                raise ValueError("build failed")

        async def scenario(session):
            pass

        with self.assertRaisesRegex(ValueError, "build failed"):
            await exercise(Bad(), scenario)
        App().close()


if __name__ == "__main__":
    unittest.main()
