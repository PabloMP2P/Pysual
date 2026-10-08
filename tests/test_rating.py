"""Rating composes press gestures, bounded model updates, and portable stars."""

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Container, Style, Theme
from pysual._controls.ranges import Rating
from pysual.errors import LifecycleError
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree
from pysual.runtime import Runtime


class StarHost(RecordingHost):
    def __init__(self):
        super().__init__()
        self.stars = []

    def begin(self, color):
        super().begin(color)
        self.stars = []

    def text(self, text, *args, **kwargs):
        super().text(text, *args, **kwargs)
        if text in ("★", "☆"):
            self.stars.append((text, args))


class RatingTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=500, height=240)
        self.host = StarHost()
        self.runtime = Runtime(self.app, self.host)
        self.app._runtime = self.runtime
        self.addCleanup(self.app.destroy)
        self.addCleanup(setattr, self.app, "_runtime", None)

    def rating(self, **properties):
        owner = Rating(parent=self.app, left=10, top=10, height=36, **properties)
        arrange(self.app, self.host)
        return owner

    def pointer(self, owner, kind, star=1, *, pointer_id=7, button=1, routed=False):
        pitch = owner.measure(self.host)[0] / owner.maximum
        x = owner.bounds.right + 10 if star is None else owner.bounds.x + (star - 0.5) * pitch
        event = Input(kind, x, owner.bounds.y + owner.bounds.height / 2,
                      pointer_id=pointer_id, button=button)
        self.runtime.router.process(event) if routed else owner.handle_input(event)

    def stars(self):
        paint_tree(self.app, self.host)
        return "".join(glyph for glyph, _ in self.host.stars)

    def test_pointer_release_selects_only_the_same_star(self):
        rating = self.rating(value=1)
        self.pointer(rating, "pointer_down", 3)
        self.assertEqual(rating.value, 1)
        self.pointer(rating, "pointer_up", 4)
        self.assertEqual(rating.value, 1)
        self.pointer(rating, "pointer_up", 3)
        self.assertEqual(rating.value, 1)
        self.pointer(rating, "pointer_down", 3)
        self.pointer(rating, "pointer_move", 4)
        self.assertFalse(rating._pressed)
        self.pointer(rating, "pointer_move", 3)
        self.assertTrue(rating._pressed)
        self.pointer(rating, "pointer_up", 3)
        self.assertEqual(rating.value, 3)
        self.assertFalse(rating._pressed)

    def test_wrong_pointer_and_button_cannot_complete_or_cancel_a_gesture(self):
        rating = self.rating()
        self.pointer(rating, "pointer_down", 4, button=3)
        self.pointer(rating, "pointer_up", 4)
        self.assertEqual(rating.value, 0)
        self.pointer(rating, "pointer_down", 2)
        for kind in ("pointer_move", "pointer_down", "pointer_up", "pointer_cancel"):
            self.pointer(rating, kind, 4, pointer_id=8)
            self.assertEqual(rating.value, 0)
            self.assertTrue(rating._pressed)
        self.pointer(rating, "pointer_up", 2, button=3)
        self.assertEqual(rating.value, 0)
        self.assertTrue(rating._pressed)
        self.pointer(rating, "pointer_up", 2)
        self.assertEqual(rating.value, 2)

    def test_cancel_blur_and_outside_release_restore_committed_rating(self):
        for cancellation in ("pointer_cancel", "blur"):
            with self.subTest(cancellation=cancellation):
                rating = self.rating(value=1)
                self.pointer(rating, "pointer_down", 4)
                self.pointer(rating, cancellation, 4)
                self.assertFalse(rating._pressed)
                self.pointer(rating, "pointer_up", 4)
                self.assertEqual(rating.value, 1)
                rating.destroy()
        rating = self.rating(value=2)
        self.pointer(rating, "pointer_down", 3)
        self.pointer(rating, "pointer_up", None)
        self.pointer(rating, "pointer_up", 3)
        self.assertEqual(rating.value, 2)

    def test_space_after_intrinsic_stars_does_not_arm_a_press(self):
        rating = self.rating(width=200)
        self.pointer(rating, "pointer_down", 7)
        self.assertFalse(rating._pressed)
        self.pointer(rating, "pointer_up", 4)
        self.assertEqual(rating.value, 0)

    def test_keyboard_navigation_clamps_and_supports_unrated_value(self):
        rating = self.rating()
        for key, expected in (
            ("ArrowLeft", 0), ("ArrowRight", 1), ("ArrowUp", 2),
            ("ArrowDown", 1), ("End", 5), ("ArrowRight", 5),
            ("Home", 0), ("End", 5), ("Delete", 0),
            ("End", 5), ("Backspace", 0),
        ):
            with self.subTest(key=key):
                rating.handle_input(Input("key_down", key=key))
                self.assertEqual(rating.value, expected)
        self.pointer(rating, "pointer_down", 4)
        rating.handle_input(Input("key_down", key="ArrowRight"))
        self.pointer(rating, "pointer_up", 4)
        self.assertEqual(rating.value, 1)

    def test_read_only_and_disabled_controls_ignore_user_edits(self):
        for setting in ("read_only", "enabled"):
            with self.subTest(setting=setting):
                rating = self.rating(value=2)
                self.pointer(rating, "pointer_down", 4)
                setattr(rating, setting, setting == "read_only")
                self.pointer(rating, "pointer_up", 4)
                rating.handle_input(Input("key_down", key="End"))
                self.assertEqual(rating.value, 2)
                self.assertFalse(rating._pressed)
                rating.value = 3
                self.assertEqual(rating.value, 3)
                setattr(rating, setting, setting != "read_only")
                self.pointer(rating, "pointer_up", 4)
                self.assertEqual(rating.value, 3)
                rating.destroy()
        parent = Container(parent=self.app, enabled=False)
        rating = Rating(parent=parent, value=2)
        rating.handle_input(Input("key_down", key="End"))
        self.assertEqual(rating.value, 2)

    def test_instances_keep_gestures_and_values_independent(self):
        first, second = self.rating(), self.rating(value=4, maximum=7)
        self.pointer(first, "pointer_down", 3)
        self.pointer(second, "pointer_up", 3)
        self.assertEqual((first.value, second.value), (0, 4))
        second.handle_input(Input("key_down", key="End"))
        self.assertEqual((first.value, second.value), (0, 7))
        self.pointer(first, "pointer_up", 3)
        self.assertEqual((first.value, second.value), (3, 7))

    def test_atomic_range_updates_validate_before_touching_gesture_or_model(self):
        rating = self.rating(value=4)
        self.pointer(rating, "pointer_down", 3)
        with self.assertRaises(ValueError):
            rating.update(maximum=2, value=3)
        self.assertEqual((rating.maximum, rating.value), (5, 4))
        self.assertTrue(rating._pressed)
        self.pointer(rating, "pointer_up", 3)
        self.assertEqual(rating.value, 3)
        self.pointer(rating, "pointer_down", 2)
        rating.update(maximum=2, value=1)
        self.assertEqual((rating.maximum, rating.value), (2, 1))
        self.pointer(rating, "pointer_up", 2)
        self.assertEqual(rating.value, 1)
        rating.update(value=8, maximum=10)
        self.assertEqual((rating.maximum, rating.value), (10, 8))
        for properties in ({"value": -1}, {"maximum": 0}, {"value": 11}):
            with self.subTest(properties=properties), self.assertRaises(ValueError):
                rating.update(**properties)
        for properties in ({"value": 1.5}, {"maximum": True}):
            with self.subTest(properties=properties), self.assertRaises(TypeError):
                rating.update(**properties)

    def test_painting_previews_hover_without_committing_and_restores_on_leave(self):
        rating = self.rating(value=2)
        self.assertEqual(self.stars(), "★★☆☆☆")
        self.pointer(rating, "pointer_move", 4, routed=True)
        self.assertEqual(rating.value, 2)
        self.assertEqual(self.stars(), "★★★★☆")
        self.pointer(rating, "pointer_move", None, routed=True)
        self.assertEqual(self.stars(), "★★☆☆☆")
        self.pointer(rating, "pointer_move", 5, routed=True)
        rating.handle_input(Input("blur"))
        self.assertEqual(self.stars(), "★★☆☆☆")
        rating.read_only = True
        self.pointer(rating, "pointer_move", 4, routed=True)
        self.assertEqual(self.stars(), "★★☆☆☆")

    def test_selected_star_styles_and_large_clipped_ranges_use_portable_text(self):
        theme = (Theme()
                 .styled(Rating, Style(foreground="#112233"), part="star")
                 .styled(Rating, Style(foreground="#445566"), part="star", state="selected"))
        rating = self.rating(value=2, theme_override=theme)
        self.assertEqual(self.stars(), "★★☆☆☆")
        self.assertEqual([args[2] for _, args in self.host.stars],
                         ["#445566"] * 2 + ["#112233"] * 3)
        rating.update(maximum=1_000_000, width=60)
        arrange(self.app, self.host)
        self.assertEqual(self.stars(), "★★☆")
        self.assertEqual(len(self.host.stars), 3)

    def test_maximum_changes_intrinsic_width_and_destroy_clears_gesture(self):
        rating = self.rating()
        width, height = rating.measure(self.host)
        rating.maximum = 10
        self.assertEqual(rating.measure(self.host), (width * 2, height))
        self.pointer(rating, "pointer_down", 2)
        rating.destroy()
        self.assertFalse(rating._pressed)
        rating.destroy()
        with self.assertRaises(LifecycleError):
            rating.handle_input(Input("key_down", key="End"))


class LiveRatingTests(AsyncUIOwnerTestCase):
    async def test_routed_changes_have_user_origin_and_keyboard_focus(self):
        app, host = App(width=400, height=180), StarHost()
        rating = Rating(parent=app, left=10, top=10, height=36)
        seen = []
        rating.changed.connect(seen.append)
        try:
            app.run(backend=host)
            runtime = app._runtime
            pitch = rating.measure(host)[0] / rating.maximum
            x, y = rating.bounds.x + 2.5 * pitch, rating.bounds.y + 18
            for kind in ("pointer_down", "pointer_up"):
                runtime.router.process(Input(kind, x, y))
            self.assertTrue(rating.focused)
            runtime.router.process(Input("key_down", key="ArrowRight"))
            await runtime.dispatcher.drain()
            self.assertEqual([(event.old_value, event.new_value) for event in seen],
                             [(0, 3), (3, 4)])
            self.assertTrue(all(event.origin == "user" for event in seen))
            self.assertTrue(all(event.property_name == "value" for event in seen))
            rating.value = 1
            await runtime.dispatcher.drain()
            self.assertEqual(seen[-1].origin, "program")
            rating.enabled = False
            runtime.router.process(Input("key_down", key="End"))
            await runtime.dispatcher.drain()
            self.assertEqual(rating.value, 1)
        finally:
            await app.destroy_async()
