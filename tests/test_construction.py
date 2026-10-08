"""Installed-client construction, ownership transactions, and attachment lifecycle."""

import asyncio
import unittest

from pysual import (
    App,
    Button,
    ClickEvent,
    ClosingEvent,
    Container,
    Control,
    RadioButton,
    SplitPane,
    TabControl,
    TabPage,
    prop,
)
from pysual.errors import BindingError, LifecycleError
from _support import exercise


class ConstructionTests(unittest.TestCase):
    def test_assignment_adopts_and_parent_preserves_app_handler_scope(self):
        class Badge(Button):
            count: int = prop(default=0)

        class Demo(App):
            def notice_on_click(self, event: ClickEvent):
                pass

        app = Demo()
        app.sidebar = Container()
        app.notice = Badge(parent=app.sidebar, count=3)
        app.alias = app.notice
        app.sidebar.alias = app.notice
        self.assertIs(app.sidebar.parent, app)
        self.assertIs(app.notice.parent, app.sidebar)
        self.assertEqual(app.notice.name, "notice")
        self.assertIs(app.notice._binding_owner, app)
        self.assertEqual(app.notice.click._convention, app.notice_on_click)
        self.assertEqual(app.sidebar.children, (app.notice,))
        self.assertNotIn("parent", Badge.properties())
        with self.assertRaisesRegex(BindingError, "framework"):
            app.notice.parent = app
        replacement = Badge()
        with self.assertRaisesRegex(BindingError, "Destroy"):
            app.notice = replacement
        self.assertIsNone(replacement.parent)
        app.notice.destroy()
        app.notice = replacement
        self.assertIs(replacement.parent, app)
        self.assertEqual(replacement.name, "notice")

    def test_private_locals_collections_and_none_do_not_guess_a_parent(self):
        app = App()
        app._private = Button()
        app.collection = [Button()]
        local = Button(parent=None)
        self.assertFalse(app.children)
        app.public = app._private
        self.assertIs(app.public.parent, app)
        self.assertIsNone(local.parent)
        self.assertIsNone(app.collection[0].parent)
        Button(parent=app)  # Anonymous content attaches immediately, without a name.
        self.assertIsNone(app.children[-1].name)
        local.destroy()
        app.collection[0].destroy()

    def test_parent_hook_waits_for_complete_custom_constructor(self):
        trace = []

        class Custom(Control):
            def __init__(self, *, width=10):
                super().__init__(width=width)
                self._ready = True
                trace.append("constructed")

            def on_attached(self):
                trace.append(("attached", self._ready, self.width))

        app = App()
        app.custom = Custom(parent=app, width=20)
        self.assertEqual(trace, ["constructed", ("attached", True, 20)])
        self.assertEqual(app.custom.name, "custom")

        class Broken(Control):
            def __init__(self):
                super().__init__()
                raise ValueError("constructor failed")

        with self.assertRaisesRegex(ValueError, "constructor failed"):
            Broken(parent=app)
        self.assertEqual(app.children, (app.custom,))

        class MissingSuper(Control):
            def __init__(self):
                pass

        with self.assertRaisesRegex(LifecycleError, "super"):
            MissingSuper(parent=app)

    def test_binding_preflight_runs_before_sibling_affecting_hook(self):
        class Demo(App):
            reserved = None

            def choice_on_click(self, event: ClosingEvent):
                pass

        app = Demo()
        app.existing = RadioButton(checked=True)
        candidate = RadioButton(checked=True)
        for name in ("run", "parent", "reserved", "Demo", "not-valid", "choice"):
            with self.subTest(name=name), self.assertRaises(BindingError):
                setattr(app, name, candidate)
            self.assertTrue(app.existing.checked)
            self.assertIsNone(candidate.parent)
            self.assertIsNone(candidate.name)
            self.assertFalse(candidate._channels)
        app.valid = candidate
        self.assertFalse(app.existing.checked)
        self.assertIs(candidate.parent, app)

    def test_attachment_failure_keeps_state_and_binding_unchanged(self):
        class Fragile(Container):
            def on_attached(self):
                raise ValueError("attachment failed")

        app = App()
        app.candidate = "previous state"
        child = Fragile()
        child.inner = Button()
        with self.assertRaisesRegex(ValueError, "attachment failed"):
            app.candidate = child
        self.assertEqual(app.candidate, "previous state")
        self.assertFalse(app.children)
        self.assertNotIn("candidate", app._bindings)
        self.assertIsNone(child.parent)
        self.assertIsNone(child.name)
        self.assertIs(child.inner.parent, child)
        child.destroy()
        with self.assertRaisesRegex(ValueError, "attachment failed"):
            Fragile(parent=app)
        self.assertFalse(app.children)

    def test_parent_restrictions_apply_to_all_creation_paths(self):
        app = App()
        app.tabs = TabControl()
        app.page = TabPage(parent=app.tabs)
        app.tabs.second_page = TabPage()
        self.assertFalse(app.tabs.second_page.visible)
        invalid = Button()
        with self.assertRaisesRegex(TypeError, "TabPage"):
            app.tabs.invalid = invalid
        self.assertIsNone(invalid.parent)
        self.assertIsNone(invalid.name)
        with self.assertRaisesRegex(TypeError, "TabPage"):
            Button(parent=app.tabs)
        app.split = SplitPane()
        Container(parent=app.split)
        Container(parent=app.split)
        with self.assertRaisesRegex(TypeError, "two containers"):
            Container(parent=app.split)
        with self.assertRaisesRegex(TypeError, "parent"):
            Button(parent=invalid)
        with self.assertRaisesRegex(TypeError, "direct constructor"):
            app.button(parent=app.tabs)
        foreign = Container()
        foreign.child = Button()
        with self.assertRaises(BindingError):
            app.alias = foreign.child
        with self.assertRaises(BindingError):
            app.page.loop = app
        with self.assertRaises(BindingError):
            foreign.child_loop = foreign
        foreign.destroy()
        with self.assertRaises(LifecycleError):
            Button(parent=foreign)
        app.close()
        with self.assertRaisesRegex(LifecycleError, "parent"):
            App(parent=Container())
        App().close()  # A rejected parent never consumes the singleton reservation.


class ConstructionEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_adoption_routes_and_rollback_cancels_subtree_tasks(self):
        trace = []

        class Demo(App):
            def live_on_click(self, event: ClickEvent):
                trace.append("click")

        app = Demo()

        async def pending():
            await asyncio.Event().wait()

        tasks = []

        class Fragile(Container):
            def on_attached(self):
                tasks.append(self.inner.create_task(pending()))
                raise ValueError("attachment failed")

        async def scenario(session):
            app.live = Button()
            app.live.activate()
            broken = Fragile()
            broken.inner = Button()
            with self.assertRaisesRegex(ValueError, "attachment failed"):
                app.broken = broken
            self.assertIsNone(broken._dispatcher)
            self.assertIsNone(broken.inner._dispatcher)
            await asyncio.sleep(0)
            with self.assertRaises(asyncio.CancelledError):
                await tasks[0]
            self.assertTrue(tasks[0].cancelled())
            broken.destroy()

        await exercise(app, scenario)
        self.assertEqual(trace, ["click"])
