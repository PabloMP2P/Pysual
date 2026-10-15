"""Search fields compose ordinary text editing, focus, events and ownership."""

import asyncio

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost, eventually

from pysual import App, Container, Style, TextBox, Theme
from pysual.fields import SearchField
from pysual.host import Input
from pysual.runtime import Runtime
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, paint_tree, resolve_style
from pysual import get_theme, theme_names
from test_retained_paint import PatchRecordingHost


class RequiredQuery(SearchField):
    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "text" and not value:
            raise ValueError("A query is required")


class SearchFieldCompositionTests(UIOwnerTestCase):
    def test_programmatic_sync_normalization_and_atomic_rejection(self):
        field = SearchField(text="first\r\nquery", placeholder="Find items")
        self.addCleanup(field.destroy)
        self.assertEqual(field.text, "first query")
        self.assertEqual(field.entry.text, field.text)
        self.assertEqual(field.entry.placeholder, "Find items")
        field.text = "second\nquery"
        self.assertEqual(field.entry.text, "second query")
        field.update(text="third\rquery", placeholder="Find people", read_only=True)
        self.assertEqual(field.entry.text, "third query")
        self.assertEqual(field.entry.placeholder, "Find people")
        self.assertTrue(field.entry.read_only)
        self.assertFalse(field.clear_button.enabled)
        with self.assertRaises(TypeError):
            field.update(text=42, placeholder="Rejected", read_only=False)
        self.assertEqual(field.text, "third query")
        self.assertEqual(field.entry.text, "third query")
        self.assertEqual(field.placeholder, "Find people")
        self.assertEqual(field.entry.placeholder, "Find people")
        self.assertTrue(field.read_only and field.entry.read_only)
        field.update(text="", read_only=False)
        self.assertFalse(field.clear_button.enabled)

    def test_rejected_child_edit_preserves_text_selection_and_history(self):
        field = RequiredQuery(text="query")
        self.addCleanup(field.destroy)
        field.entry.select_all()
        snapshot = (field.entry.text, field.entry.selection_range, field.entry.can_undo)
        with self.assertRaisesRegex(ValueError, "required"):
            field.entry.replace_selection("")
        self.assertEqual(field.text, "query")
        self.assertEqual(
            (field.entry.text, field.entry.selection_range, field.entry.can_undo), snapshot,
        )

    def test_owned_names_and_disposal_are_per_instance(self):
        host = Container()
        self.addCleanup(host.destroy)
        first = SearchField(text="first", parent=host)
        second = SearchField(text="second", parent=host)
        self.assertEqual([child.name for child in first.children],
                         ["search_icon", "entry", "clear_button"])
        self.assertIsNot(first.entry, second.entry)
        self.assertIs(first.entry.parent, first)
        first.entry.select_all()
        first.entry.replace_selection("changed")
        self.assertEqual(first.text, "changed")
        self.assertEqual(second.text, "second")
        children = first.children
        first.destroy()
        self.assertTrue(all(child._disposed for child in children))
        self.assertFalse(second._disposed or second.entry._disposed)
        self.assertEqual(host.children, (second,))


class SearchFieldRuntimeTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App()
        self.field = SearchField(parent=self.app, left=20, top=20, width=320, height=40)
        self.other = SearchField(parent=self.app, left=20, top=80, width=320, height=40)
        self.runtime = Runtime(self.app, RecordingHost())
        self.task = asyncio.create_task(self.runtime.main())
        await eventually(lambda: self.app.is_open or self.task.done())
        if self.task.done():
            await self.task
        await eventually(lambda: self.field.clear_button.bounds.width > 0)

    async def asyncTearDown(self):
        self.runtime._stop.set()
        await self.task
        self.app.destroy()

    def click_clear(self):
        bounds = self.field.clear_button.bounds
        x, y = bounds.x + bounds.width / 2, bounds.y + bounds.height / 2
        self.runtime.router.process(Input("pointer_down", x=x, y=y))
        self.runtime.router.process(Input("pointer_up", x=x, y=y))

    async def test_text_then_enter_in_one_input_batch_has_current_value_and_origin(self):
        seen = []
        self.field.changed.connect(lambda event: seen.append(("changed", event, self.field.text)))
        self.field.submitted.connect(lambda event: seen.append(("submitted", event, self.field.text)))
        self.field.focus()
        self.assertIs(self.runtime.router.focus, self.field.entry)
        self.assertTrue(self.field.focused)
        self.runtime.router.process(Input("text", text="latest query"))
        self.runtime.router.process(Input("key_down", key="Enter"))
        self.assertEqual(self.field.text, "latest query")
        await self.runtime.dispatcher.drain()
        self.assertEqual([(kind, text) for kind, _, text in seen],
                         [("changed", "latest query"), ("submitted", "latest query")])
        self.assertTrue(all(event.source is self.field and event.origin == "user"
                            for _, event, _ in seen))
        self.assertEqual(seen[0][1].old_value, "")
        self.assertEqual(seen[0][1].new_value, "latest query")
        self.assertEqual(seen[0][1].property_name, "text")
        self.assertEqual(self.other.text, "")

    async def test_typing_then_clear_in_one_batch_refocuses_and_is_undoable(self):
        changes = []
        self.field.changed.connect(changes.append)
        self.field.focus()
        self.runtime.router.process(Input("text", text="query"))
        self.click_clear()
        self.assertEqual(self.field.text, "")
        self.assertEqual(self.field.entry.text, "")
        self.assertIs(self.runtime.router.focus, self.field.entry)
        self.assertFalse(self.field.clear_button.enabled)
        await self.runtime.dispatcher.drain()
        self.assertEqual([(event.old_value, event.new_value) for event in changes],
                         [("", "query"), ("query", "")])
        self.assertTrue(all(event.origin == "user" and event.source is self.field for event in changes))
        self.runtime.router.process(Input("key_down", key="z", ctrl=True))
        self.assertEqual(self.field.text, "query")
        await self.runtime.dispatcher.drain()
        self.assertEqual(len(changes), 3)
        self.assertEqual(changes[-1].new_value, "query")

    async def test_programmatic_changes_and_clear_preserve_program_origin(self):
        changes = []
        self.field.changed.connect(changes.append)
        self.field.update(text="query", placeholder="People")
        await self.runtime.dispatcher.drain()
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].origin, "program")
        self.field.clear_button.activate()
        await self.runtime.dispatcher.drain()
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[-1].origin, "program")
        self.assertEqual(self.field.text, "")
        self.assertIs(self.runtime.router.focus, self.field.entry)

    async def test_keyboard_cut_and_paste_forward_user_origin_to_search_changes(self):
        changes = []
        self.field.changed.connect(changes.append)
        self.field.focus()
        self.runtime.host.clipboard = "query"
        self.runtime.router.process(Input("key_down", key="v", ctrl=True))
        await eventually(lambda: self.field.text == "query")
        self.field.entry.select_all()
        self.runtime.router.process(Input("key_down", key="x", ctrl=True))
        await eventually(lambda: self.field.text == "")
        await self.runtime.dispatcher.drain()
        self.assertEqual([(event.new_value, event.origin) for event in changes],
                         [("query", "user"), ("", "user")])
        self.assertTrue(all(event.source is self.field for event in changes))
        self.assertEqual(self.field.entry._origin, "program")

    async def test_rejected_clipboard_edit_restores_origin(self):
        field = RequiredQuery(parent=self.app, text="required", top=140)
        field.focus()
        field.entry.select_all()
        changes = []
        field.changed.connect(changes.append)
        with self.assertRaisesRegex(ValueError, "required"):
            await self.runtime.router.clipboard("x")
        self.assertEqual(field.entry._origin, "program")
        self.assertEqual(field.text, "required")
        self.assertEqual(field.entry.selection_text, "required")
        self.assertFalse(field.entry.can_undo)
        await self.runtime.dispatcher.drain()
        self.assertEqual(changes, [])

    async def test_rejected_clear_preserves_selection_history_and_events(self):
        field = RequiredQuery(parent=self.app, text="required", top=140)
        field.entry.select(1, 4)
        changes, clicks = [], []
        field.changed.connect(changes.append)
        field.clear_button.click.connect(clicks.append)
        with self.assertRaisesRegex(ValueError, "required"):
            field.clear_button.activate()
        await self.runtime.dispatcher.drain()
        self.assertEqual(field.text, "required")
        self.assertEqual(field.entry.text, "required")
        self.assertEqual(field.entry.selection_range, (1, 4))
        self.assertFalse(field.entry.can_undo)
        self.assertEqual(changes, [])
        self.assertEqual(clicks, [])

    async def test_read_only_allows_selection_and_submit_but_blocks_edits_and_clear(self):
        self.field.update(text="fixed query", read_only=True)
        seen = []
        self.field.changed.connect(lambda event: seen.append("changed"))
        self.field.submitted.connect(lambda event: seen.append("submitted"))
        self.field.focus()
        self.runtime.router.process(Input("key_down", key="a", ctrl=True))
        self.assertEqual(self.field.entry.selection_text, "fixed query")
        self.runtime.router.process(Input("text", text="replacement"))
        self.runtime.router.process(Input("key_down", key="Backspace"))
        self.field.clear_button.activate()
        self.runtime.router.process(Input("key_down", key="Enter"))
        await self.runtime.dispatcher.drain()
        self.assertEqual(self.field.text, "fixed query")
        self.assertFalse(self.field.clear_button.enabled)
        self.assertEqual(seen, ["submitted"])

    async def test_disabled_parent_blocks_routed_input_and_clear(self):
        self.field.text = "query"
        self.field.focus()
        self.field.enabled = False
        changes, submissions = [], []
        self.field.changed.connect(changes.append)
        self.field.submitted.connect(submissions.append)
        self.runtime.router.process(Input("text", text="ignored"))
        self.runtime.router.process(Input("key_down", key="Enter"))
        self.click_clear()
        self.field.clear_button.activate()
        await self.runtime.dispatcher.drain()
        self.assertEqual(self.field.text, "query")
        self.assertEqual(changes, [])
        self.assertEqual(submissions, [])
        self.assertFalse(self.field.entry.effective_enabled)

    async def test_keyboard_clear_and_second_instance_keep_focus_and_state_independent(self):
        self.field.text = "first"
        self.other.text = "second"
        self.field.clear_button.focus()
        self.assertTrue(self.field.focused)
        self.runtime.router.process(Input("key_down", key="Enter"))
        self.runtime.router.process(Input("key_up", key="Enter"))
        self.assertEqual(self.field.text, "")
        self.assertEqual(self.other.text, "second")
        self.assertIs(self.runtime.router.focus, self.field.entry)
        self.other.focus()
        self.runtime.router.process(Input("text", text="!"))
        self.assertEqual(self.other.text, "!second")
        self.assertEqual(self.field.text, "")
        self.assertFalse(self.field.focused)
        self.assertTrue(self.other.focused)

    async def test_search_icon_focuses_entry_without_changing_query(self):
        self.field.text = "Keep this query"
        bounds = self.field.search_icon.bounds
        self.runtime.router.process(Input("pointer_down", x=bounds.x + 8, y=bounds.y + 8))
        self.runtime.router.process(Input("pointer_up", x=bounds.x + 8, y=bounds.y + 8))
        self.assertIs(self.runtime.router.focus, self.field.entry)
        self.assertEqual(self.field.text, "Keep this query")


class SearchFieldPaintTests(UIOwnerTestCase):
    def test_read_only_clear_glyph_tracks_empty_text_in_retained_paint(self):
        app, host = App(width=400, height=160), PatchRecordingHost()
        field = SearchField(parent=app, read_only=True, width=320, height=40)
        runtime = Runtime(app, host)
        app._runtime = runtime
        scene = runtime._retained_paint = RetainedPaintTree(host)
        try:
            arrange(app, host)
            for text in ("", "query", ""):
                field.text = text
                paint_tree(app, host, retained=scene)
                retained = host.snapshot()
                paint_tree(app, host)
                self.assertEqual(retained, tuple(host._drawing))
        finally:
            app._runtime = None
            app.destroy()

    def test_composite_focus_is_one_outer_outline_and_clears_in_both_render_paths(self):
        for retained in (False, True):
            with self.subTest(retained=retained):
                app, host = App(width=400, height=160), PatchRecordingHost()
                field = SearchField(parent=app, left=12, top=12, width=320, height=40)
                other = SearchField(parent=app, left=12, top=70, width=320, height=40)
                runtime = Runtime(app, host)
                app._runtime = runtime
                scene = RetainedPaintTree(host) if retained else None
                runtime._retained_paint = scene
                try:
                    arrange(app, host)

                    def outlines():
                        paint_tree(app, host, runtime.router.focus, retained=scene)
                        drawing = host.snapshot() if retained else host._drawing
                        return [args[0] for kind, _, args, _ in drawing
                                if kind == "rect" and len(args) == 5
                                and args[1] == "" and args[3] == app.theme.tokens.accent
                                and args[4] == 2]

                    self.assertEqual(outlines(), [])
                    field.focus()
                    self.assertEqual(outlines(), [field.bounds.inset(1)])
                    other.focus()
                    self.assertEqual(outlines(), [other.bounds.inset(1)])
                    runtime.router.set_focus(None)
                    self.assertEqual(outlines(), [])
                finally:
                    app._runtime = None
                    app.destroy()

    def test_all_themes_style_the_whole_search_field_as_an_input(self):
        from pysual import TextBox
        for name in theme_names():
            app = App(theme=get_theme(name))
            field = SearchField(parent=app)
            entry = TextBox(parent=app)
            try:
                with self.subTest(theme=name):
                    outer, ordinary = resolve_style(field), resolve_style(entry)
                    for attribute in ("fill", "radius", "border", "border_width", "bevel"):
                        self.assertEqual(getattr(outer, attribute), getattr(ordinary, attribute))
                    for attribute in ("font_size", "font_family", "foreground"):
                        self.assertEqual(getattr(resolve_style(field.entry), attribute),
                                         getattr(ordinary, attribute))
            finally:
                app.destroy()


class TypographyHost(RecordingHost):
    def begin(self, color):
        super().begin(color)
        self.faces, self.selections, self.carets = [], [], []

    def text(self, text, x, y, color, size, mono=False):
        self.faces.append((text, color, size, mono))
        super().text(text, x, y, color, size, mono)

    def measure(self, text, size, mono=False):
        return len(text) * size * (0.6 if mono else 0.4), size * 1.2

    def rect(self, rect, fill, *args, **kwargs):
        self.selections.append((rect, fill))

    def caret(self, x, y, height, color):
        self.carets.append((x, y, height))
        return True


class SearchFieldTypographyTests(UIOwnerTestCase):
    def setUp(self):
        self.app, self.host = App(), TypographyHost()
        self.addCleanup(self.app.destroy)

    def paint(self):
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)

    def test_direct_typography_construction_update_and_reset_reach_query(self):
        values = dict(font_size=28, font_family="mono", foreground="#ff0000")
        field = SearchField(parent=self.app, text="Query", width=400, **values)
        ordinary = TextBox(parent=self.app, text="Reference", top=100, **values)
        for update, expected in (
            ({}, ("#ff0000", 28, True)),
            (dict(font_size=19, font_family="ui", foreground="#00ff00"),
             ("#00ff00", 19, False)),
            (dict(font_size=None, font_family=None, foreground=None),
             (self.app.theme.tokens.foreground, self.app.theme.tokens.font_size, False)),
        ):
            with self.subTest(update=update):
                field.update(**update)
                ordinary.update(**update)
                self.paint()
                self.assertIn(("Query", *expected), self.host.faces)
                self.assertIn(("Reference", *expected), self.host.faces)

    def test_search_theme_typography_and_explicit_entry_overrides(self):
        field = SearchField(parent=self.app, text="Query", width=400)
        for target, size, family in ((self.app, 24, "mono"), (field, 18, "ui")):
            theme = (Theme().styled(TextBox, Style(font_size=13))
                     .styled(SearchField, Style(font_size=size,
                         font_family=family, foreground="#123456")))
            if target is self.app:
                self.app.theme = theme
            else:
                field.theme_override = theme
            self.paint()
            self.assertIn(("Query", "#123456", size, family == "mono"), self.host.faces)
            field.text = ""
            self.paint()
            self.assertIn(("Search", self.app.theme.tokens.muted, size, family == "mono"),
                          self.host.faces)
            field.text = "Query"
        field.theme_override = None
        field.entry.update(font_size=17, font_family="mono", foreground="#abcdef")
        field.update(text="Changed", read_only=True)
        self.app.theme = Theme().styled(SearchField, Style(font_size=30))
        self.paint()
        self.assertIn(("Changed", "#abcdef", 17, True), self.host.faces)
        # An explicit change on the owner replaces only that entry property.
        field.font_size = 22
        self.paint()
        self.assertIn(("Changed", "#abcdef", 22, True), self.host.faces)

    def test_styled_long_query_selection_caret_and_scroll_share_metrics(self):
        self.app.theme = Theme().styled(SearchField, Style(font_size=28, font_family="mono"))
        field = SearchField(parent=self.app, text="abcdefghij" * 10, width=300, height=70)
        runtime = Runtime(self.app, self.host)
        self.app._runtime = runtime
        self.addCleanup(setattr, self.app, "_runtime", None)
        self.paint()
        field.focus()
        field.entry.select(2, 5)
        self.paint()
        selected = [rect for rect, color in self.host.selections
                    if color == self.app.theme.tokens.selection]
        self.assertEqual(len(selected), 1)
        self.assertAlmostEqual(selected[0].width, self.host.measure("cde", 28, True)[0])
        self.assertAlmostEqual(self.host.carets[0][0], selected[0].right)
        self.assertAlmostEqual(self.host.carets[0][2], 28 * 1.4)
        field.entry.select(len(field.text), len(field.text))
        for size in (28, 14):
            field.font_size = size
            self.paint()
            self.assertGreater(field.entry._scroll_x, 0)
            self.assertAlmostEqual(field.entry._document_width(),
                                   self.host.measure(field.text, size, True)[0])
            x, _, height = self.host.carets[0]
            self.assertTrue(field.entry.bounds.x <= x < field.entry.bounds.right)
            self.assertAlmostEqual(height, size * 1.4)
