"""Commands, popup navigation and shortcut scope through the shared router."""

import asyncio
from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from pysual import App, Button, Menu, MenuBar, MenuGroup, MenuItem
from pysual.commands import normalize_shortcut
from pysual.host import Input
from pysual.runtime import Runtime
from test_library import RecordingHost, eventually


class MenuDataTests(UIOwnerTestCase):
    def test_named_shortcuts_accept_case_variants_and_reject_alias_duplicates(self):
        for key in ("Enter", "Space", "Home", "End", "PageUp", "PageDown",
                    "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"):
            for spelling in (key, key.lower(), key.upper()):
                with self.subTest(spelling=spelling):
                    self.assertEqual(normalize_shortcut(f"ctrl+{spelling}"), f"Ctrl+{key}")
        with self.assertRaisesRegex(ValueError, "Duplicate menu shortcut"):
            Menu(items=(MenuItem("a", "A", "Ctrl+Enter"),
                        MenuItem("b", "B", "CTRL+ENTER")))

    def test_typed_records_and_ambiguous_chords_fail_early(self):
        self.assertEqual(normalize_shortcut("shift+ctrl+s"), "Ctrl+Shift+S")
        for chord in ("s", "Alt+S", "Ctrl+Ctrl+S", "F13", "Ctrl++"):
            with self.assertRaises(ValueError):
                Button(shortcut=chord)
        with self.assertRaises(TypeError):
            MenuGroup("Mutable", [MenuItem("save", "Save")])
        with self.assertRaises(ValueError):
            Menu(items=(MenuItem("save", "Save"), MenuItem("save", "Again")))
        with self.assertRaises(ValueError):
            MenuBar(
                groups=(
                    MenuGroup(
                        "File",
                        (
                            MenuItem("save", "Save", "Ctrl+S"),
                            MenuItem("other", "Other", "ctrl+s"),
                        ),
                    ),
                )
            )


class MenuRuntimeTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        commands, clicks = [], []

        class Demo(App):
            def build(self):
                self.layout = "stack"
                self.bar = self.menu_bar(
                    groups=(
                        MenuGroup(
                            "File",
                            (
                                MenuItem("disabled", "Disabled", enabled=False),
                                MenuItem("save", "Save", "Ctrl+S"),
                                MenuItem("separator", "", separator=True),
                                MenuItem("last", "Last"),
                            ),
                        ),
                        MenuGroup("Edit", (MenuItem("undo", "Undo", "Ctrl+Z"),)),
                    ),
                    height=32,
                )
                self.editor = self.text_box(text="original", height=38)
                self.action = self.button(text="Action", shortcut="Ctrl+K", height=38)
                self.context = self.menu(items=(MenuItem("inspect", "Inspect"),))

            def bar_on_command(self, event):
                commands.append((event.key, event.origin))

            def context_on_command(self, event):
                commands.append((event.key, event.origin))

            def action_on_click(self, event):
                clicks.append(event.origin)

            def action_on_context_menu(self, event):
                self.context.show(self.action, at=(event.x, event.y))

        self.commands, self.clicks = commands, clicks
        self.app, self.host = Demo(width=480, height=320), RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task

    async def inputs(self, *events):
        self.host.events.extend(events)
        await asyncio.sleep(0.04)
        if self.task.done():
            await self.task
        self.runtime.dispatcher.raise_errors()

    async def key(self, key, **modifiers):
        await self.inputs(Input("key_down", key=key, **modifiers))

    async def test_shortcuts_preserve_local_editing_and_disabled_scope(self):
        self.app.editor.focus()
        self.app.editor.select_all()
        await self.inputs(Input("text", text="new"))
        await self.key("s", ctrl=True)
        await self.key("z", ctrl=True)
        self.assertEqual(self.app.editor.text, "original")
        self.assertEqual(self.commands, [("save", "user")])
        await self.key("k", ctrl=True)
        self.assertEqual(self.clicks, ["user"])
        self.app.action.enabled = False
        await self.key("k", ctrl=True)
        self.assertEqual(len(self.clicks), 1)
        modal = self.app.sub_window(title="Modal", width=240, height=150)
        modal.button(text="Close")
        opened = asyncio.create_task(modal.show_modal_async())
        await eventually(lambda: self.runtime.modal is modal)
        await self.key("s", ctrl=True)
        self.assertEqual(len(self.commands), 1)
        await modal.close_async()
        await opened
        modal.destroy()

    async def test_open_menu_tracks_its_header_through_window_resize(self):
        self.app.layout = "absolute"
        bar = self.app.bar
        bar.update(left=20, top=10, width=230, anchor="left,right,top",
                   groups=tuple(MenuGroup(name, (MenuItem(name, name),))
                                for name in ("File", "Edit", "Selection", "View", "Help")))
        await self.inputs()
        bar._open(3)
        await self.inputs()
        popup = self.runtime.popup
        self.assertEqual(popup.bounds.x, bar._overflow_rect.x)
        self.host.size = (680, 320)
        await self.inputs(Input("resize"))
        self.assertTrue(popup.is_open)
        self.assertGreater(bar._headers[3].width, 0)
        self.assertEqual(popup.bounds.x, bar._headers[3].x)
        self.assertEqual(popup.bounds.y, bar.bounds.bottom)
        self.host.size = (480, 320)
        await self.inputs(Input("resize"))
        self.assertTrue(popup.is_open)
        self.assertEqual(bar._headers[3].width, 0)
        self.assertEqual(popup.bounds.x, bar._overflow_rect.x)

    async def test_explicit_context_point_stays_fixed_when_anchor_moves(self):
        self.app.layout = "absolute"
        self.app.context.show(self.app.action, at=(50, 100))
        await self.inputs()
        popup = self.runtime.popup
        self.app.action.update(left=100, top=150)
        await self.inputs()
        self.assertTrue(popup.is_open)
        self.assertEqual((popup.bounds.x, popup.bounds.y), (50, 100))

    async def test_menu_keyboard_switching_commit_and_replacement_cleanup(self):
        await self.key("F10")
        self.assertIsNotNone(self.runtime.popup)
        self.assertEqual(self.runtime.popup.children[0].selected_index, 1)
        await self.key("ArrowDown")
        self.assertEqual(self.runtime.popup.children[0].selected_index, 3)
        await self.key("ArrowRight")
        await self.key("Enter")
        self.assertEqual(self.commands, [("undo", "user")])
        self.assertIsNone(self.runtime.popup)
        await self.key("F10")
        self.app.bar.groups = ()
        self.assertIsNone(self.runtime.popup)
        self.assertTrue(self.app.bar.focused)

    async def test_f10_runs_an_enabled_binding_and_otherwise_opens_the_bar(self):
        for enabled in (True, False):
            self.app.bar.groups = (
                MenuGroup("File", (MenuItem("nested", "Nested", children=(
                    MenuItem("f10", "Action", "F10", enabled=enabled),)),)),
            )
            self.commands.clear()
            await self.key("F10")
            self.assertEqual(self.commands, [("f10", "user")] if enabled else [])
            self.assertEqual(self.runtime.popup is not None, not enabled)
            if self.runtime.popup is not None:
                await self.key("Escape")

    async def test_context_pointer_does_not_activate_button_and_disposal_closes_menu(
        self,
    ):
        box = self.app.action.bounds
        await self.inputs(Input("pointer_down", box.x + 20, box.y + 20, button=3))
        self.assertIsNotNone(self.runtime.popup)
        self.assertEqual(self.clicks, [])
        await self.key("Enter")
        self.assertEqual(self.commands, [("inspect", "user")])
        self.assertIsNone(self.runtime.popup)
        self.app.context.show(self.app.action)
        self.app.context.destroy()
        self.assertIsNone(self.runtime.popup)


class MenuScrollTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=900, height=500, reduce_motion=True)
        self.owner = self.app.button(text="Menu", left=10, top=10, width=180, height=40)
        self.menu = self.app.menu(items=tuple(
            MenuItem(str(i), f"Command {i}", children=(
                MenuItem("child", "Child", children=(MenuItem("leaf", "Leaf"),)),
            ) if i == 3 else ()) for i in range(20)
        ))
        self.runtime = Runtime(self.app, RecordingHost())
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task

    def open_child(self):
        self.menu.show(self.owner)
        self.runtime._paint_frame()
        popup = self.runtime.popup
        rows = popup.children[0]
        self.hover(rows, 3)
        self.assertEqual(len(popup.children), 2)
        return popup, rows, popup.children[1]

    def hover(self, rows, index):
        self.runtime.router.process(Input(
            "pointer_move", x=rows.bounds.x + 50,
            y=rows.bounds.y + (index - rows._scroll + 0.5) * rows._row_height(),
        ))
        self.runtime._paint_frame()

    async def test_wheel_closes_descendants_restores_focus_and_reopens_at_new_row(self):
        popup, rows, child = self.open_child()
        self.runtime.router.process(Input("key_down", key="ArrowRight"))
        self.runtime.router.process(Input("key_down", key="ArrowRight"))
        self.runtime._paint_frame()
        grandchild = popup.children[2]
        self.assertIs(self.runtime.router.focus, grandchild)
        self.runtime.router.process(Input(
            "wheel", x=rows.bounds.x + 50, y=rows.bounds.y + 15, delta=1,
        ))
        self.runtime._paint_frame()
        self.assertEqual(popup.children, (rows,))
        self.assertTrue(child._disposed and grandchild._disposed)
        self.assertEqual(rows._scroll, 3)
        self.assertIs(self.runtime.router.focus, rows)
        self.runtime.router.process(Input("key_down", key="ArrowDown"))
        self.assertEqual(rows.selected_index, 4)
        self.hover(rows, 3)
        self.assertEqual(len(popup.children), 2)
        self.assertEqual(popup.children[1].bounds.y, rows.bounds.y)

    async def test_wheel_without_row_movement_preserves_submenu(self):
        popup, rows, child = self.open_child()
        for delta in (-1, 0.1):
            self.runtime.router.process(Input(
                "wheel", x=rows.bounds.x + 50, y=rows.bounds.y + 15, delta=delta,
            ))
            self.runtime._paint_frame()
            self.assertEqual(rows._scroll, 0)
            self.assertEqual(popup.children, (rows, child))
            self.assertFalse(child._disposed)

    async def test_scrollbar_page_and_drag_close_only_after_scroll_changes(self):
        for gesture in ("page", "drag"):
            with self.subTest(gesture=gesture):
                popup, rows, child = self.open_child()
                track, thumb = rows._scrollbar()
                x = track.x + track.width / 2
                y = track.bottom - 2 if gesture == "page" else thumb.y + 5
                self.runtime.router.process(Input("pointer_down", x=x, y=y))
                if gesture == "drag":
                    self.assertEqual(popup.children, (rows, child))
                    self.runtime.router.process(Input("pointer_move", x=x, y=y + 60))
                self.runtime.router.process(Input("pointer_up", x=x, y=y + 60))
                self.runtime._paint_frame()
                self.assertGreater(rows._scroll, 0)
                self.assertEqual(popup.children, (rows,))
                self.assertTrue(child._disposed)
                self.assertIsNone(rows._scroll_drag)
