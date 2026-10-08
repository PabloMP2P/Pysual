"""Choice-card interaction and disclosure content ownership across real hosts."""

import asyncio
from typing import get_type_hints

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost, eventually

from pysual import App, Button, Container, Label, Style, SubWindow, Theme, blueprint
from pysual.cards import ChoiceCard, Disclosure
from pysual.errors import BindingError, LifecycleError
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import Painter, paint_tree, resolve_style
from pysual.runtime import Runtime


class CardForm(Container):
    choice = blueprint(ChoiceCard, text="Grid", checked=True, icon="grid")
    section = blueprint(Disclosure, title="Advanced", expanded=True)


class CardTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=640, height=400)
        self.host = RecordingHost()
        self.addCleanup(self.app.destroy)

    def card(self, **properties):
        card = ChoiceCard(parent=self.app, width=240, height=90, **properties)
        arrange(self.app, self.host)
        return card

    def pointer(self, target, kind, *, pointer_id=7, button=1, inside=True):
        rect = target.bounds
        target.handle_input(Input(kind, rect.x + 5 if inside else rect.right + 20,
                                  rect.y + 5, pointer_id=pointer_id, button=button))

    def test_choices_require_matching_release_and_ignore_other_pointers(self):
        card = self.card()
        self.pointer(card, "pointer_up")
        self.pointer(card, "pointer_down", button=3)
        self.pointer(card, "pointer_up")
        self.assertFalse(card.checked)
        self.pointer(card, "pointer_down")
        for kind in ("pointer_down", "pointer_move", "pointer_cancel", "pointer_up"):
            self.pointer(card, kind, pointer_id=9, inside=False)
            self.assertTrue(card._pressed)
        self.pointer(card, "pointer_up", button=3)
        self.assertFalse(card.checked)
        self.pointer(card, "pointer_up")
        self.assertTrue(card.checked)
        self.assertFalse(card._pressed)
        for cancellation in ("pointer_cancel", "blur", "outside"):
            card.checked = False
            self.pointer(card, "pointer_down")
            if cancellation == "outside":
                self.pointer(card, "pointer_up", inside=False)
            else:
                card.handle_input(Input(cancellation, pointer_id=7))
            self.pointer(card, "pointer_up")
            self.assertFalse(card.checked)

    def test_choice_keyboard_groups_skip_unavailable_and_keep_parents_separate(self):
        first = self.card(checked=True, group="view")
        disabled = self.card(enabled=False, group="view")
        readonly = self.card(read_only=True, group="view")
        hidden = self.card(visible=False, group="view")
        unfocusable = self.card(focusable=False, group="view")
        last = self.card(group="view")
        unrelated = self.card(checked=True, group="other")
        parent = Container(parent=self.app)
        nested = ChoiceCard(parent=parent, checked=True, group="view")
        first.handle_input(Input("key_down", key="ArrowRight"))
        self.assertFalse(first.checked)
        self.assertTrue(last.checked)
        last.handle_input(Input("key_down", key="ArrowRight"))
        self.assertTrue(first.checked)
        first.handle_input(Input("key_down", key="End"))
        self.assertTrue(last.checked)
        last.handle_input(Input("key_down", key="Home"))
        self.assertTrue(first.checked)
        self.assertTrue(unrelated.checked)
        self.assertTrue(nested.checked)
        for card in (disabled, readonly, hidden, unfocusable):
            self.assertFalse(card.checked)
        for key in ("Enter", "Space"):
            last.handle_input(Input("key_down", key=key))
            last.handle_input(Input("key_down", key=key))
            self.assertTrue(first.checked)
            last.handle_input(Input("key_up", key="Escape"))
            self.assertTrue(first.checked)
            last.handle_input(Input("key_up", key=key))
            self.assertTrue(last.checked)
            first.checked = True
        last.update(group="other", checked=True)
        self.assertFalse(unrelated.checked)
        self.assertTrue(first.checked)

    def test_disabled_readonly_and_validation_cancel_or_preserve_gestures(self):
        card = self.card(icon="grid")
        for property_name in ("enabled", "read_only"):
            self.pointer(card, "pointer_down")
            setattr(card, property_name, property_name == "read_only")
            self.pointer(card, "pointer_up")
            card.handle_input(Input("key_down", key="Space"))
            card.handle_input(Input("key_up", key="Space"))
            self.assertFalse(card.checked)
            self.assertFalse(card._pressed)
            setattr(card, property_name, property_name == "enabled")
        self.pointer(card, "pointer_down")
        with self.assertRaises(ValueError):
            card.update(icon="missing-icon", checked=True, description="Changed")
        self.assertEqual((card.icon, card.checked, card.description), ("grid", False, ""))
        self.assertTrue(card._pressed)
        card.update(icon="menu", checked=True, description="A useful description")
        self.assertFalse(card._pressed)
        with self.assertRaises(TypeError):
            card.update(checked=1)
        self.app.enabled = False
        card.checked = False
        self.pointer(card, "pointer_down")
        self.pointer(card, "pointer_up")
        self.assertFalse(card.checked)

    def test_disclosure_adopts_content_and_preserves_child_state_when_collapsed(self):
        content = Container(layout="stack")
        label = Label(parent=content, text="Persistent state")
        hidden = Label(parent=content, text="Stay hidden", visible=False)
        section = Disclosure(parent=self.app, title="Options", summary="Details", content=content,
                             expanded=True, width=300)
        arrange(self.app, self.host)
        expanded_height = section.bounds.height
        self.assertIs(content.parent, section.content)
        paint_tree(self.app, self.host)
        self.assertIn("Persistent state", self.host.texts)
        section.expanded = False
        arrange(self.app, self.host)
        self.host.texts.clear()
        paint_tree(self.app, self.host)
        self.assertLess(section.bounds.height, expanded_height)
        self.assertNotIn("Persistent state", self.host.texts)
        self.assertTrue(label.visible)
        self.assertFalse(hidden.visible)
        self.assertFalse(section.content.visible)
        section.expanded = True
        arrange(self.app, self.host)
        self.assertEqual(section.bounds.height, expanded_height)
        section.destroy()
        self.assertTrue(content._disposed)
        self.assertTrue(label._disposed)

    def test_disclosure_only_heading_activates_and_keyboard_is_directional(self):
        section = Disclosure(parent=self.app, expanded=True, width=300, height=200)
        header = section._header
        arrange(self.app, self.host)
        section.handle_input(Input("pointer_down", section.bounds.x + 10, section.bounds.bottom - 10))
        self.pointer(header, "pointer_up")
        self.assertTrue(section.expanded)
        self.pointer(header, "pointer_down")
        self.pointer(header, "pointer_up", pointer_id=9)
        self.assertTrue(section.expanded)
        self.pointer(header, "pointer_up")
        self.assertFalse(section.expanded)
        header.handle_input(Input("key_down", key="Space"))
        self.assertFalse(section.expanded)
        header.handle_input(Input("key_up", key="Space"))
        self.assertTrue(section.expanded)
        for key, value in (("ArrowLeft", False), ("ArrowRight", True)):
            self.pointer(header, "pointer_down")
            header.handle_input(Input("key_down", key=key))
            self.pointer(header, "pointer_up")
            self.assertEqual(section.expanded, value)
        self.pointer(header, "pointer_down")
        with self.assertRaises(TypeError):
            section.update(title="Changed", expanded="yes")
        self.assertEqual(section.title, "Details")
        self.assertTrue(header._pressed)
        section.enabled = False
        self.pointer(header, "pointer_up")
        header.handle_input(Input("key_down", key="ArrowLeft"))
        self.assertTrue(section.expanded)
        self.assertFalse(header._pressed)

    def test_content_validation_does_not_adopt_or_destroy_borrowed_objects(self):
        content = Container()
        self.addCleanup(content.destroy)
        with self.assertRaises(TypeError):
            Disclosure(content=content, expanded="yes")
        self.assertIsNone(content.parent)
        self.assertFalse(content._disposed)
        self.app.add(content)
        with self.assertRaises(BindingError):
            Disclosure(content=content)
        self.assertIs(content.parent, self.app)
        with self.assertRaises(BindingError):
            Disclosure(content=self.app)
        with self.assertRaises(TypeError):
            Disclosure(content="Settings")

    def test_blueprints_lifetime_and_styles_stay_independent(self):
        first, second = CardForm(), CardForm()
        self.addCleanup(first.destroy)
        self.addCleanup(second.destroy)
        self.assertIsNot(first.section.content, second.section.content)
        self.assertIsNot(first.choice._press, second.choice._press)
        first.section.expanded = False
        self.assertTrue(second.section.expanded)
        section, choice = first.section, first.choice
        first.destroy()
        self.assertTrue(section._disposed)
        self.assertFalse(second.section._disposed)
        with self.assertRaises(LifecycleError):
            choice.select()
        for kind in (ChoiceCard, Disclosure):
            self.assertIs(get_type_hints(kind)["visible"], bool)
        theme = (Theme().styled(Button, Style(radius=19))
                 .styled(ChoiceCard, Style(fill="#123456"), state="selected")
                 .styled(Disclosure, Style(fill="#654321"), part="header", state="selected"))
        card = self.card(checked=True, theme_override=theme)
        section = Disclosure(parent=self.app, expanded=True, theme_override=theme)
        self.assertEqual(resolve_style(card).radius, 19)
        self.assertEqual(resolve_style(card, selected=True).fill, "#123456")
        self.assertEqual(section._header._part_style("header").fill, "#654321")

    def test_portable_paint_handles_narrow_zero_and_multiline_labels(self):
        card = self.card(text="Title\nOther", description="Long\tdetail" * 100, icon="grid", checked=True)
        section = Disclosure(parent=self.app, title="Header\nOther", summary="Summary\tdetail" * 100)
        for width, height in ((0, 0), (1, 1), (10, 5), (180, 90)):
            card.update(width=width, height=height)
            section.update(width=width, height=height)
            arrange(self.app, self.host)
            self.host.texts.clear()
            paint_tree(self.app, self.host)
            self.assertTrue(all("\n" not in text for text in self.host.texts))

    def test_vertical_card_wraps_description_and_honors_icon_surface(self):
        class Host(RecordingHost):
            def __init__(self):
                super().__init__()
                self.positions, self.surfaces, self.markers = [], [], []

            def text(self, text, x, y, *args):
                self.positions.append((text, x, y))
                super().text(text, x, y, *args)

            def styled_rect(self, rect, style):
                self.surfaces.append((rect, style))
                return True

            def marker(self, rect, style, **kwargs):
                self.markers.append(rect)
                return True

        host = Host()
        theme = Theme().styled(ChoiceCard, Style(fill="#defabc", radius=8), part="icon")
        card = ChoiceCard(parent=self.app, text="Grid view", description="Best for visual browsing",
                          icon="grid", orientation="vertical", width=160, height=180,
                          theme_override=theme)
        arrange(self.app, host)
        card.paint(Painter(host, card.bounds, theme, card))
        self.assertEqual(host.texts[0], "Grid view")
        self.assertGreater(len(host.texts), 2)
        self.assertEqual(" ".join(host.texts[1:]), card.description)
        title_y = host.positions[0][2]
        self.assertTrue(all(y > title_y for _, _, y in host.positions[1:]))
        icon_rect = next(rect for rect, style in host.surfaces if style.fill == "#defabc")
        self.assertLess(icon_rect.bottom, title_y)
        self.assertLessEqual(icon_rect.right, host.markers[0].x)
        self.assertEqual(icon_rect.y, host.markers[0].y)

    def test_vertical_description_has_a_bounded_line_budget_and_elides_overflow(self):
        card = self.card(text="Title", description="A detailed explanation " * 1_000,
                         orientation="vertical", icon="grid")
        card.update(width=180, height=190)
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)
        self.assertEqual(len(self.host.texts), 4)
        self.assertTrue(self.host.texts[-1].endswith("…"))
        with self.assertRaises(ValueError):
            card.orientation = "diagonal"
        self.assertEqual(card.orientation, "vertical")


class CardRuntimeTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=640, height=400, layout="stack")
        self.host = RecordingHost()
        self.runtime = Runtime(self.app, self.host)
        self.task = None

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

    async def test_user_origins_focus_collapse_and_hidden_tab_targets(self):
        first = ChoiceCard(parent=self.app, text="First", checked=True, width=240)
        unfocusable = ChoiceCard(parent=self.app, text="Not a tab stop", focusable=False, width=240)
        second = ChoiceCard(parent=self.app, text="Second", width=240)
        section = Disclosure(parent=self.app, expanded=True, width=300)
        child = Button(parent=section.content, text="Nested action")
        changes = []
        first.changed.connect(lambda event: changes.append((event.source, event.new_value, event.origin)))
        second.changed.connect(lambda event: changes.append((event.source, event.new_value, event.origin)))
        section.changed.connect(lambda event: changes.append((event.source, event.new_value, event.origin)))
        await self.start()
        first.focus()
        self.host.events = [Input("key_down", key="ArrowRight")]
        await eventually(lambda: second.checked)
        self.assertIs(self.runtime.router.focus, second)
        self.assertFalse(unfocusable.checked)
        await eventually(lambda: len(changes) == 2)
        self.assertEqual([(value, origin) for _, value, origin in changes], [(False, "user"), (True, "user")])
        child.focus()
        section.expanded = False
        self.assertIs(self.runtime.router.focus, section._header)
        self.runtime.router._tab(False)
        self.assertIsNot(self.runtime.router.focus, child)
        section.focus()
        self.host.events = [Input("key_down", key="Space"), Input("key_up", key="Space")]
        await eventually(lambda: section.expanded)
        await eventually(lambda: len(changes) == 4)
        self.assertEqual(changes[-1], (section, True, "user"))

    async def test_modal_content_collapse_rejection_is_atomic(self):
        section = Disclosure(parent=self.app, title="Original", expanded=True, width=300)
        child = Button(parent=section.content, text="Open details")
        await self.start()
        for parent in (self.app, section.content):
            dialog = SubWindow(parent=parent, visible=False)
            Button(parent=dialog, text="Dialog action")
            dialog.show(modal=True, owner=child)
            original_focus = self.runtime.router.focus
            original_revision = section._paint_revision
            with self.assertRaises(LifecycleError):
                section.expanded = False
            with self.assertRaises(LifecycleError):
                section.update(title="Should not commit", expanded=False)
            self.assertTrue(section.expanded)
            self.assertTrue(section.content.visible)
            self.assertEqual(section.title, "Original")
            self.assertEqual(section._paint_revision, original_revision)
            self.assertIs(self.runtime.router.focus, original_focus)
            await dialog.close_async()
            dialog.destroy()
        section.expanded = False
        self.assertFalse(section.content.visible)
