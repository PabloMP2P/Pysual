"""New control families use public construction and composition contracts."""

from typing import get_type_hints

from _ui_testcase import UIOwnerTestCase
from pysual import (
    AlertBanner, Badge, Breadcrumb, ChoiceCard, CircularProgress, Container,
    DatePicker, Disclosure, Meter, Pagination, RangeSlider,
    Rating, SearchField, SegmentedControl, blueprint, registered_controls,
)


class ReviewForm(Container):
    rating = blueprint(Rating, value=2)
    search = blueprint(SearchField, text="review")
    date_picker = blueprint(DatePicker, value="2026-10-15")
    interval = blueprint(RangeSlider, value=(10, 70))
    progress = blueprint(CircularProgress, value=40)
    view = blueprint(SegmentedControl, items=("All", "Done"), selected_index=0)
    path = blueprint(Breadcrumb, items=("Workspace", "Reviews"))
    pages = blueprint(Pagination, page_count=3)
    approach = blueprint(ChoiceCard, text="Focused", checked=True)
    details = blueprint(Disclosure, title="Advanced", expanded=True)
    status = blueprint(Badge, text="Ready", tone="success")
    notice = blueprint(AlertBanner, text="Review ready", action_text="Open")
    level = blueprint(Meter, value=40, warning_at=75, danger_at=90)


class CatalogControlIntegrationTests(UIOwnerTestCase):
    def test_new_catalog_entries_leave_application_member_names_available(self):
        catalog = registered_controls()
        aliases = registered_controls(factories_only=True)
        for name, control_type in (("rating", Rating), ("search_field", SearchField),
                                   ("date_picker", DatePicker), ("range_slider", RangeSlider),
                                   ("circular_progress", CircularProgress),
                                   ("segmented_control", SegmentedControl),
                                   ("breadcrumb", Breadcrumb), ("pagination", Pagination),
                                   ("choice_card", ChoiceCard), ("disclosure", Disclosure),
                                   ("badge", Badge), ("alert_banner", AlertBanner),
                                   ("meter", Meter)):
            with self.subTest(name=name):
                self.assertIs(catalog[name], control_type)
                self.assertNotIn(name, aliases)
                self.assertNotIn(name, vars(Container))
                self.assertIs(get_type_hints(control_type)["visible"], bool)
                created = Container()
                try:
                    child = created.create(control_type)
                    self.assertIs(child.parent, created)
                finally:
                    created.destroy()
                self.assertTrue(child._disposed)
        parent = Container()
        self.addCleanup(parent.destroy)
        parent.rating = parent.create(Rating, value=3)
        parent.search_field = parent.create(SearchField, text="Ready")
        parent.date_picker = parent.create(DatePicker, value="2026-11-01")
        self.assertEqual(len(parent.children), 3)
        self.assertEqual(parent.rating.value, 3)
        self.assertEqual(parent.search_field.text, "Ready")

    def test_blueprints_keep_composite_children_and_destruction_independent(self):
        first, second = ReviewForm(), ReviewForm()
        self.addCleanup(first.destroy)
        self.addCleanup(second.destroy)
        first.rating.value = 4
        first.search.text = "Changed"
        first.date_picker.value = "2026-11-01"
        first.interval.value = (20, 50)
        first.progress.value = 80
        first.view.selected_index = 1
        first.path.items = ("Home",)
        first.pages.page = 3
        first.approach.checked = False
        first.details.expanded = False
        first.status.text = "Changed"
        first.notice.visible = False
        first.level.value = 80
        self.assertEqual(second.rating.value, 2)
        self.assertEqual(second.search.text, "review")
        self.assertEqual(second.date_picker.value, "2026-10-15")
        self.assertEqual(second.interval.value, (10, 70))
        self.assertEqual(second.progress.value, 40)
        self.assertEqual(second.view.selected_index, 0)
        self.assertEqual(second.path.items, ("Workspace", "Reviews"))
        self.assertEqual(second.pages.page, 1)
        self.assertTrue(second.approach.checked)
        self.assertTrue(second.details.expanded)
        self.assertEqual(second.status.text, "Ready")
        self.assertTrue(second.notice.visible)
        self.assertEqual(second.level.value, 40)
        self.assertIsNot(first.details.content, second.details.content)
        self.assertIsNot(first.search.entry, second.search.entry)
        entry = first.search.entry
        first.destroy()
        self.assertTrue(entry._disposed)
        self.assertFalse(second.search.entry._disposed)
