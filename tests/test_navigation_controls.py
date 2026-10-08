"""Part gestures, bounded navigation models, and portable presentation."""

from typing import get_type_hints

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Button, Container, Style, Theme, blueprint, registered_controls
from pysual.errors import LifecycleError
from pysual.host import Input
from pysual.layout import arrange
from pysual.navigation import Breadcrumb, BreadcrumbEvent, Pagination, SegmentedControl
from pysual.painting import paint_tree, resolve_style


class NavigationForm(Container):
    mode = blueprint(SegmentedControl, items=("List", "Grid"), selected_index=0)
    path = blueprint(Breadcrumb, items=("Home", "Project", "Controls"))
    pages = blueprint(Pagination, page_count=100)


class NavigationTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=1200, height=600)
        self.host = RecordingHost()
        self.addCleanup(self.app.destroy)

    def control(self, kind, **properties):
        owner = kind(parent=self.app, left=10, top=10, height=36, **properties)
        arrange(self.app, self.host)
        return owner

    def point(self, owner, part):
        if isinstance(owner, SegmentedControl):
            pitch, first = owner._geometry()
            x = 2 + (part - first + 0.5) * pitch
        elif isinstance(owner, Pagination):
            x = (owner._parts().index(part) + 0.5) * owner._display_pitch() - 2
        else:
            area = next(area for index, area, _ in owner._parts()[0] if index == part)
            x = area.x + area.width / 2
        return owner.bounds.x + x, owner.bounds.y + owner.bounds.height / 2

    def pointer(self, owner, kind, part, *, pointer_id=7, button=1):
        x, y = self.point(owner, part)
        owner.handle_input(Input(kind, x, y, pointer_id=pointer_id, button=button))

    def test_selection_commits_on_matching_primary_release_only(self):
        for owner, selected, value in (
            (self.control(SegmentedControl, items=("A", "B", "C"), selected_index=0), 2, "selected_index"),
            (self.control(Pagination, page_count=12), 3, "page"),
        ):
            with self.subTest(kind=type(owner).__name__):
                initial = getattr(owner, value)
                self.pointer(owner, "pointer_up", selected)
                self.pointer(owner, "pointer_down", selected, button=3)
                self.pointer(owner, "pointer_up", selected)
                self.assertEqual(getattr(owner, value), initial)
                self.pointer(owner, "pointer_down", selected)
                for event in ("pointer_move", "pointer_down", "pointer_up", "pointer_cancel"):
                    self.pointer(owner, event, selected, pointer_id=8)
                    self.assertTrue(owner._pressed)
                    self.assertEqual(getattr(owner, value), initial)
                self.pointer(owner, "pointer_up", selected, button=3)
                self.assertTrue(owner._pressed)
                self.pointer(owner, "pointer_up", selected)
                self.assertEqual(getattr(owner, value), selected)
                self.assertFalse(owner._pressed)

    def test_other_part_outside_cancel_and_blur_do_not_select(self):
        for owner, selected, other, attribute in (
            (self.control(SegmentedControl, items=("A", "B", "C"), selected_index=0), 2, 1, "selected_index"),
            (self.control(Pagination, page_count=12), 3, 2, "page"),
        ):
            initial = getattr(owner, attribute)
            self.pointer(owner, "pointer_down", selected)
            self.pointer(owner, "pointer_move", other)
            self.assertFalse(owner._pressed)
            self.pointer(owner, "pointer_move", selected)
            self.assertTrue(owner._pressed)
            self.assertEqual(owner._armed_part, selected)
            self.pointer(owner, "pointer_up", other)
            self.assertEqual(getattr(owner, attribute), initial)
            for cancellation in ("blur", "pointer_cancel", "outside"):
                self.pointer(owner, "pointer_down", selected)
                if cancellation == "outside":
                    owner.handle_input(Input("pointer_up", owner.bounds.right + 20,
                                             owner.bounds.y + 12, pointer_id=7))
                else:
                    owner.handle_input(Input(cancellation, pointer_id=7))
                self.pointer(owner, "pointer_up", selected)
                self.assertEqual(getattr(owner, attribute), initial)

    def test_keyboard_selection_clamps_and_cancels_pointer_gesture(self):
        segment = self.control(SegmentedControl, items=("List", "Grid", "Details"))
        for key, expected in (("ArrowRight", 0), ("ArrowRight", 1), ("End", 2),
                              ("ArrowRight", 2), ("Home", 0), ("ArrowLeft", 0)):
            segment.handle_input(Input("key_down", key=key))
            self.assertEqual(segment.selected_index, expected)
        self.pointer(segment, "pointer_down", 2)
        segment.handle_input(Input("key_down", key="ArrowRight"))
        self.pointer(segment, "pointer_up", 2)
        self.assertEqual(segment.selected_index, 1)
        self.assertEqual(segment.selected_item, "Grid")
        pagination = self.control(Pagination, page_count=20)
        for key, expected in (("ArrowLeft", 1), ("ArrowDown", 2), ("End", 20),
                              ("ArrowRight", 20), ("Home", 1)):
            pagination.handle_input(Input("key_down", key=key))
            self.assertEqual(pagination.page, expected)

    def test_disabled_control_and_ancestor_ignore_pointer_and_keys(self):
        for owner, part, attribute in (
            (self.control(SegmentedControl, items=("A", "B", "C"), selected_index=0), 2, "selected_index"),
            (self.control(Pagination, page_count=12), 3, "page"),
        ):
            initial = getattr(owner, attribute)
            self.pointer(owner, "pointer_down", part)
            owner.enabled = False
            self.pointer(owner, "pointer_up", part)
            owner.handle_input(Input("key_down", key="End"))
            self.assertEqual(getattr(owner, attribute), initial)
            self.assertFalse(owner._pressed)
            owner.enabled = True
            self.pointer(owner, "pointer_up", part)
            self.assertEqual(getattr(owner, attribute), initial)
        parent = Container(parent=self.app, enabled=False)
        segment = SegmentedControl(parent=parent, items=("A", "B"))
        segment.handle_input(Input("key_down", key="End"))
        self.assertEqual(segment.selected_index, -1)

    def test_atomic_updates_validate_without_mutating_live_gestures(self):
        segment = self.control(SegmentedControl, items=("A", "B", "C"), selected_index=2)
        self.pointer(segment, "pointer_down", 0)
        with self.assertRaises(ValueError):
            segment.update(items=("Only",), selected_index=1)
        self.assertEqual((segment.items, segment.selected_index), (("A", "B", "C"), 2))
        self.assertTrue(segment._pressed)
        segment.update(items=("Only",), selected_index=0)
        self.assertFalse(segment._pressed)
        self.assertEqual(segment.selected_item, "Only")
        with self.assertRaises(ValueError):
            segment.items = ()
        segment.update(items=(), selected_index=-1)
        self.assertIsNone(segment.selected_item)
        pages = self.control(Pagination, page_count=20, page=12)
        self.pointer(pages, "pointer_down", "previous")
        with self.assertRaises(ValueError):
            pages.update(page_count=3, page=4)
        self.assertEqual((pages.page_count, pages.page), (20, 12))
        self.assertTrue(pages._pressed)
        pages.update(page=2, page_count=3)
        self.assertFalse(pages._pressed)
        for properties, error in (({"page": 0}, ValueError), ({"page_count": 0}, ValueError),
                                   ({"page": True}, TypeError), ({"visible_pages": 16}, ValueError)):
            with self.assertRaises(error):
                pages.update(**properties)

    def test_selected_styles_are_parts_and_explicit_fallbacks_apply(self):
        theme = (Theme().styled(Button, Style(radius=13))
                 .styled(SegmentedControl, Style(fill="#123456"), part="segment", state="selected")
                 .styled(Pagination, Style(fill="#654321"), part="page", state="selected"))
        segment = self.control(SegmentedControl, theme_override=theme, items=("A", "B"), selected_index=1)
        pages = self.control(Pagination, theme_override=theme, page_count=3)
        self.assertEqual(resolve_style(segment).radius, 13)
        self.assertEqual(resolve_style(segment, "segment", selected=True).fill, "#123456")
        self.assertEqual(resolve_style(pages, "page", selected=True).fill, "#654321")

    def test_large_models_bound_paint_work_and_reveal_keyboard_selection(self):
        segment = self.control(SegmentedControl, items=("item",) * 100_000, width=100)
        paint_tree(self.app, self.host)
        self.assertLessEqual(self.host.texts.count("item"), 4)
        segment.handle_input(Input("key_down", key="End"))
        pitch, first = segment._geometry()
        self.assertTrue(first <= segment.selected_index < first + 100 / pitch)
        segment.destroy()
        path = self.control(Breadcrumb, items=("folder",) * 100_000, width=600)
        self.assertLessEqual(len(path._shown_indices()), 7)
        self.assertEqual(path._shown_indices()[0], 0)
        self.assertEqual(path._shown_indices()[-1], 99_999)
        path.handle_input(Input("key_down", key="Home"))
        self.assertIn(path._focus_index, path._shown_indices())
        paint_tree(self.app, self.host)
        self.assertLessEqual(len(self.host.texts), 7)
        path.destroy()
        pages = self.control(Pagination, page_count=1_000_000_000, page=400_000_000, width=130)
        self.assertIn(pages.page, pages._parts())
        self.assertEqual(pages._parts(), ["previous", pages.page, "next"])
        paint_tree(self.app, self.host)
        self.assertLessEqual(len(self.host.texts), 1)
        for part in ("previous", "next"):
            self.pointer(pages, "pointer_down", part)
            self.pointer(pages, "pointer_up", part)
        self.assertEqual(pages.page, 400_000_000)

    def test_fractional_equal_segments_keep_the_whole_row_visible(self):
        segment = self.control(SegmentedControl, items=tuple("ABCDEFG"),
                               selected_index=6, width=229)
        self.assertEqual(segment._geometry()[1], 0)
        paint_tree(self.app, self.host)
        self.assertEqual(self.host.texts, list("ABCDEFG"))
        self.pointer(segment, "pointer_down", 0)
        self.pointer(segment, "pointer_up", 0)
        self.assertEqual(segment.selected_index, 0)

    def test_visual_well_edges_select_and_keep_pointer_hover(self):
        segment = self.control(SegmentedControl, items=("List", "Grid", "Details"),
                               selected_index=1, width=300)
        segment._hover = True
        for local_x, local_y, expected in ((0, 0, 0), (299, 35, 2)):
            for kind in ("pointer_down", "pointer_up"):
                segment.handle_input(Input(kind, segment.bounds.x + local_x,
                                           segment.bounds.y + local_y, pointer_id=7))
            self.assertEqual(segment.selected_index, expected)
            self.assertEqual(segment._hot_part, expected)
        outside = Input("pointer_down", segment.bounds.right, segment.bounds.y)
        self.assertIsNone(segment._hit_part(outside))

    def test_boundary_arrows_ellipsis_and_current_path_are_not_actions(self):
        pages = self.control(Pagination, page_count=100)
        self.pointer(pages, "pointer_down", "previous")
        self.assertFalse(pages._pressed)
        self.pointer(pages, "pointer_down", None)
        self.assertFalse(pages._pressed)
        pages.page = pages.page_count
        self.pointer(pages, "pointer_down", "next")
        self.assertFalse(pages._pressed)
        path = self.control(Breadcrumb, items=("Root", "Parent", "Current"))
        self.pointer(path, "pointer_down", 2)
        self.pointer(path, "pointer_up", 2)
        self.assertFalse(path._pressed)
        with self.assertRaises(ValueError):
            path.activate(2)
        with self.assertRaises(TypeError):
            path.activate(True)
        with self.assertRaises(LifecycleError):
            path.activate(0)

    def test_instances_blueprints_and_disposal_are_independent(self):
        first, second = NavigationForm(), NavigationForm()
        self.addCleanup(first.destroy)
        self.addCleanup(second.destroy)
        first.mode.selected_index = 1
        first.path.items = ("Elsewhere",)
        first.pages.page = 20
        self.assertEqual((second.mode.selected_index, second.path.items, second.pages.page),
                         (0, ("Home", "Project", "Controls"), 1))
        children = first.children
        for child in children:
            child.handle_input(Input("key_down", key="Space"))
        first.destroy()
        for child in children:
            self.assertTrue(child._disposed)
            self.assertFalse(child._pressed)
            with self.assertRaises(LifecycleError):
                child.handle_input(Input("key_down", key="End"))
        self.assertFalse(second.pages._disposed)

    def test_public_catalog_has_no_new_container_aliases(self):
        for name, kind in (("segmented_control", SegmentedControl),
                           ("breadcrumb", Breadcrumb), ("pagination", Pagination)):
            self.assertIs(registered_controls()[name], kind)
            self.assertNotIn(name, registered_controls(factories_only=True))
            self.assertNotIn(name, vars(Container))
            self.assertIs(get_type_hints(kind)["visible"], bool)


class LiveNavigationTests(AsyncUIOwnerTestCase):
    async def test_breadcrumb_cannot_retarget_an_armed_gesture(self):
        app, host = App(width=800, height=250), RecordingHost()
        path = Breadcrumb(parent=app, items=("Root", "Parent", "Current"), left=10, top=10)
        seen = []
        path.navigated.connect(seen.append)
        try:
            app.run(backend=host)

            def pointer(kind, index, pointer_id=7):
                area = next(area for part, area, _ in path._parts()[0] if part == index)
                path.handle_input(Input(kind, path.bounds.x + area.x + area.width / 2,
                                        path.bounds.y + 16, pointer_id=pointer_id))

            pointer("pointer_down", 0)
            pointer("pointer_up", 1)
            pointer("pointer_up", 0)
            pointer("pointer_down", 0)
            pointer("pointer_up", 0, pointer_id=8)
            self.assertTrue(path._pressed)
            path.items = ("Elsewhere", "Child", "Leaf")
            pointer("pointer_up", 0)
            path.handle_input(Input("key_down", key="Space"))
            path.handle_input(Input("key_down", key="Home"))
            path.handle_input(Input("key_up", key="Space"))
            path.handle_input(Input("key_down", key="Enter"))
            path.handle_input(Input("blur"))
            path.handle_input(Input("key_up", key="Enter"))
            await app._runtime.dispatcher.drain()
            self.assertEqual(seen, [])
            pointer("pointer_down", 0)
            pointer("pointer_up", 0)
            path.handle_input(Input("key_down", key="Space"))
            path.handle_input(Input("key_up", key="Enter"))
            await app._runtime.dispatcher.drain()
            self.assertEqual(len(seen), 1)
            path.handle_input(Input("key_up", key="Space"))
            await app._runtime.dispatcher.drain()
            self.assertEqual([(event.index, event.item) for event in seen],
                             [(0, "Elsewhere"), (0, "Elsewhere")])
        finally:
            await app.destroy_async()

    async def test_routed_pointer_keyboard_and_program_changes_have_correct_origins(self):
        app, host = App(width=800, height=300), RecordingHost()
        segment = SegmentedControl(parent=app, items=("List", "Grid"), left=10, top=10)
        pages = Pagination(parent=app, page_count=20, left=10, top=70)
        path = Breadcrumb(parent=app, items=("Home", "Project", "Controls"), left=10, top=130)
        changes, navigated = [], []
        segment.changed.connect(changes.append)
        pages.changed.connect(changes.append)
        path.navigated.connect(navigated.append)
        try:
            app.run(backend=host)
            runtime = app._runtime
            x, y = segment.bounds.x + segment.bounds.width * 0.75, segment.bounds.y + 16
            for kind in ("pointer_down", "pointer_up"):
                runtime.router.process(Input(kind, x, y))
            runtime.router.process(Input("key_down", key="Home"))
            x, y = pages.bounds.x + 10, pages.bounds.y + 16
            runtime.router.process(Input("pointer_down", x, y))
            runtime.router.process(Input("pointer_up", x, y))
            runtime.router.process(Input("key_down", key="End"))
            area = path._parts()[0][0][1]
            x, y = path.bounds.x + area.width / 2, path.bounds.y + 16
            for kind in ("pointer_down", "pointer_up"):
                runtime.router.process(Input(kind, x, y))
            runtime.router.process(Input("key_down", key="Home"))
            runtime.router.process(Input("key_down", key="ArrowRight"))
            runtime.router.process(Input("key_down", key="Enter"))
            self.assertEqual(len(navigated), 0)  # Semantic channels are dispatched later.
            runtime.router.process(Input("key_up", key="Enter"))
            await runtime.dispatcher.drain()
            self.assertEqual([(e.property_name, e.new_value) for e in changes],
                             [("selected_index", 1), ("selected_index", 0), ("page", 20)])
            self.assertTrue(all(e.origin == "user" for e in changes))
            self.assertEqual([(e.index, e.item) for e in navigated], [(0, "Home"), (1, "Project")])
            self.assertTrue(all(type(e) is BreadcrumbEvent and e.origin == "user" for e in navigated))
            self.assertEqual(path.items, ("Home", "Project", "Controls"))
            pages.page = 2
            path.activate(0)
            await runtime.dispatcher.drain()
            self.assertEqual(changes[-1].origin, "program")
            self.assertEqual(navigated[-1].origin, "program")
            path.enabled = False
            path.activate(0)
            await runtime.dispatcher.drain()
            self.assertEqual(len(navigated), 3)
        finally:
            await app.destroy_async()
