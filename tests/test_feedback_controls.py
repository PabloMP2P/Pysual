"""Status semantics, independent gestures, and portable feedback painting."""

from math import isfinite
import xml.etree.ElementTree as ET

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Container, ProgressBar, Style, Theme, blueprint
from pysual.backends._term_cells import CellRenderer
from pysual.backends._web_svg import SVGRenderer
from pysual.errors import LifecycleError
from pysual.feedback import AlertBanner, Badge, Meter
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import Painter, paint_tree, resolve_style


class FeedbackHost(RecordingHost):
    def begin(self, color):
        super().begin(color)
        self.rects = []

    def rect(self, *args, **kwargs):
        self.rects.append(args)


def part_input(owner, part, kind, *, pointer_id=7, button=1):
    area = owner._areas()[part]
    return Input(kind, owner.bounds.x + area.x + area.width / 2,
                 owner.bounds.y + area.y + area.height / 2,
                 pointer_id=pointer_id, button=button)


class FeedbackTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=700, height=250)
        self.host = FeedbackHost()
        self.addCleanup(self.app.destroy)

    def paint(self):
        arrange(self.app, self.host)
        paint_tree(self.app, self.host)

    def test_badge_is_noninteractive_and_tone_text_survives_hover(self):
        badge = Badge(parent=self.app, text="Synced", tone="success")
        self.paint()
        self.assertEqual(self.host.texts, ["Synced"])
        self.assertFalse(badge.focusable)
        first = self.host.rects[:]
        badge._hover = badge._pressed = True
        badge.invalidate()
        self.paint()
        self.assertEqual(self.host.rects, first)
        badge.show_dot = False
        self.paint()
        self.assertLess(len(self.host.rects), len(first))

    def test_badge_tone_validation_and_disabled_style(self):
        badge = Badge(parent=self.app, text="Warning", tone="warning")
        revision = badge._paint_revision
        with self.assertRaises(ValueError):
            badge.update(text="Changed", tone="bad")
        self.assertEqual((badge.text, badge.tone, badge._paint_revision), ("Warning", "warning", revision))
        theme = Theme().styled(Badge, Style(fill="#112233", foreground="#abcdef"), part="warning")
        theme = theme.styled(Badge, Style(fill="#778899", foreground="#334455"), part="warning", state="disabled")
        badge.update(enabled=False, theme_override=theme)
        self.paint()
        self.assertEqual(self.host.rects[0][1], "#778899")

    def test_badge_centers_dot_and_caption_together_at_explicit_widths(self):
        class Host(FeedbackHost):
            def text(self, text, x, y, *args):
                self.text_position = (text, x, y)
                super().text(text, x, y, *args)

        host = Host()
        badge = Badge(parent=self.app, text="Synced", tone="success", width=180)
        arrange(self.app, host)
        paint_tree(self.app, host)
        dot = next(args[0] for args in host.rects if args[0].width == 7)
        text, x, _ = host.text_position
        self.assertEqual(text, "Synced")
        style = resolve_style(badge, "success")
        width = host.measure(text, style.font_size, style.font_family == "mono")[0]
        self.assertAlmostEqual(dot.x + x + width, badge.bounds.x * 2 + badge.bounds.width)
        self.assertAlmostEqual(x - dot.right, 7)
        badge.text = ""
        arrange(self.app, host)
        paint_tree(self.app, host)
        dot = next(args[0] for args in host.rects if args[0].width == 7)
        self.assertAlmostEqual(dot.x + dot.width / 2, badge.bounds.x + badge.bounds.width / 2)

    def test_meter_range_thresholds_and_atomic_failed_updates(self):
        meter = Meter(value=65, warning_at=70, danger_at=90)
        self.addCleanup(meter.destroy)
        for properties in (
            {"minimum": 66}, {"maximum": 64}, {"value": 101},
            {"minimum": 10, "maximum": 10, "value": 10},
            {"minimum": -1e308, "maximum": 1e308},
            {"value": float("nan")}, {"value": True},
            {"warning_at": 91}, {"danger_at": 69}, {"danger_at": 110},
            {"warning_at": -1}, {"warning_at": float("inf")},
            {"segments": 101}, {"segments": -1}, {"segments": True},
        ):
            with self.assertRaises((TypeError, ValueError)):
                meter.update(label="should not commit", **properties)
            self.assertEqual((meter.minimum, meter.maximum, meter.value), (0, 100, 65))
            self.assertEqual((meter.label, meter.warning_at, meter.danger_at), ("", 70, 90))
        meter.update(minimum=-50, maximum=-10, value=-25, warning_at=-20, danger_at=-15)
        self.assertEqual(meter.value, -25)

    def test_meter_set_range_clamps_values_and_thresholds(self):
        meter = Meter(value=65, warning_at=70, danger_at=90)
        self.addCleanup(meter.destroy)
        meter.set_range(20, 50)
        self.assertEqual((meter.minimum, meter.maximum, meter.value, meter.warning_at, meter.danger_at),
                         (20, 50, 50, 50, 50))
        meter.set_range(-10, 10, value=-5)
        self.assertEqual((meter.value, meter.warning_at, meter.danger_at), (-5, 10, 10))
        revision = meter._paint_revision
        for low, high, value in ((10, -10, 0), (0, 0, 0), (0, 5, 9), (True, 5, 3)):
            with self.assertRaises((TypeError, ValueError)):
                meter.set_range(low, high, value=value)
            self.assertEqual(meter._paint_revision, revision)

    def test_meter_distinguishes_measurement_from_progress_and_uses_tone_threshold(self):
        theme = (Theme().styled(ProgressBar, Style(fill="#123456"), part="fill")
                 .styled(Meter, Style(fill="#567890"), part="warning")
                 .styled(Meter, Style(fill="#abcdef"), part="danger")
                 .styled(Meter, Style(fill="#111111"), part="track"))
        meter = Meter(parent=self.app, value=40, maximum=80, label="Storage", unit="GB",
                      warning_at=50, danger_at=70, width=200, theme_override=theme)
        self.paint()
        self.assertEqual(set(self.host.texts), {"Storage", "40 GB"})
        fills = [rect for rect in self.host.rects if rect[1] == "#123456"]
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0][0].width, 100)
        for value, color in ((50, "#567890"), (70, "#abcdef"), (80, "#abcdef")):
            meter.value = value
            self.paint()
            self.assertTrue(any(rect[1] == color for rect in self.host.rects))
        meter.value = 0
        self.paint()
        self.assertFalse(any(rect[1] in ("#123456", "#567890", "#abcdef") for rect in self.host.rects))

    def test_segmented_meter_fills_partial_segment_and_bounds_work(self):
        theme = (Theme().styled(Meter, Style(fill="#123456"), part="fill")
                 .styled(Meter, Style(fill="#111111"), part="track"))
        meter = Meter(parent=self.app, value=25, segments=10, width=100,
                      show_value=False, theme_override=theme)
        self.paint()
        fill = [rect[0] for rect in self.host.rects if rect[1] == "#123456"]
        self.assertEqual(len(fill), 3)
        self.assertAlmostEqual(fill[0].width / 2, fill[-1].width)
        self.assertEqual(self.host.texts, [])
        meter.update(segments=100, value=100, width=10000)
        self.paint()
        self.assertLessEqual(len(self.host.rects), 200)
        for size in (0, 1, 4, 12):
            meter.update(width=size, height=size)
            self.paint()
            for rect in self.host.rects:
                self.assertTrue(all(isfinite(value) and value >= 0 for value in (rect[0].width, rect[0].height)))

    def test_instances_blueprints_and_lifetimes_are_independent(self):
        class Form(Container):
            badge = blueprint(Badge, text="Ready", tone="success")
            alert = blueprint(AlertBanner, text="Saved", action_text="View")
            meter = blueprint(Meter, value=10, warning_at=70)

        first, second = Form(parent=self.app), Form(parent=self.app)
        first.badge.text = "Offline"
        first.meter.value = 90
        self.assertEqual((second.badge.text, second.meter.value), ("Ready", 10))
        self.assertIsNot(first.alert._press, second.alert._press)
        first.alert.handle_input(Input("key_down", key="Space"))
        self.assertTrue(first.alert._pressed)
        disposed_alert = first.alert
        first.destroy()
        self.assertFalse(disposed_alert._pressed)
        self.assertFalse(second.alert._pressed)
        first.destroy()
        with self.assertRaises(LifecycleError):
            disposed_alert.handle_input(Input("key_up", key="Space"))

    def test_alert_startup_actions_fail_without_hiding_or_emitting(self):
        alert = AlertBanner(action_text="Manage")
        self.addCleanup(alert.destroy)
        for action in (alert.activate, alert.dismiss):
            with self.assertRaises(LifecycleError):
                action()
        self.assertTrue(alert.visible)

    def test_failed_alert_update_does_not_cancel_live_gesture(self):
        alert = AlertBanner(parent=self.app, action_text="Manage")
        alert.handle_input(Input("key_down", key="Enter"))
        revision = alert._paint_revision
        with self.assertRaises(ValueError):
            alert.update(action_text="Elsewhere", tone="neutral")
        self.assertEqual(alert.action_text, "Manage")
        self.assertTrue(alert._pressed)
        self.assertEqual(alert._paint_revision, revision)
        alert.action_text = "Elsewhere"
        self.assertFalse(alert._pressed)

    def test_tiny_alert_regions_and_long_captions_are_bounded(self):
        alert = AlertBanner(parent=self.app, action_text="A very long action", text="Long condition")
        for size in (0, 1, 4, 18, 40):
            alert.update(width=size, height=size)
            self.paint()
            for area in alert._areas().values():
                self.assertGreaterEqual(area.width, 0)
                self.assertGreaterEqual(area.height, 0)
                self.assertLessEqual(area.right, size)
                self.assertLessEqual(area.bottom, size)

    def test_svg_and_character_cells_render_feedback(self):
        controls = (
            Badge(parent=self.app, left=0, top=0, text="Synced", tone="success"),
            AlertBanner(parent=self.app, left=0, top=45, width=550, text="Ready to publish", action_text="Review"),
            Meter(parent=self.app, left=0, top=120, width=200, value=42, label="Storage", unit="GB"),
        )
        arrange(self.app, self.host)
        scene = SVGRenderer()
        scene.reset_scene("Feedback", 700, 250)
        cells = CellRenderer(90, 18)
        self.addCleanup(cells.close)
        self.addCleanup(scene.close_scene)
        for host in (scene, cells):
            host.begin(self.app.theme.tokens.background)
            for control in controls:
                host.clip(control.bounds)
                control.paint(Painter(host, control.bounds, control.effective_theme, control))
        scene.publish_scene()
        xml = ET.fromstring(scene.export_svg())
        labels = [node.text for node in xml.findall(".//{http://www.w3.org/2000/svg}text")]
        for label in ("Synced", "Ready to publish", "Review", "Storage", "42 GB"):
            self.assertIn(label, labels)
            self.assertIn(label, "".join(cell.text for cell in cells.cells))


class LiveFeedbackTests(AsyncUIOwnerTestCase):
    async def asyncSetUp(self):
        self.app = App(width=700, height=300)
        self.host = FeedbackHost()
        self.alert = AlertBanner(parent=self.app, left=10, top=10, width=600,
                                 text="Review your changes", action_text="Review")
        self.actions, self.dismissals = [], []
        self.alert.action.connect(self.actions.append)
        self.alert.dismissed.connect(self.dismissals.append)
        self.app.run(backend=self.host)

    async def asyncTearDown(self):
        await self.app.destroy_async()

    async def drain(self):
        await self.app._runtime.dispatcher.drain()

    def pointer(self, part, kind, **kwargs):
        self.app._runtime.router.process(part_input(self.alert, part, kind, **kwargs))

    def key(self, key, kind="key_down"):
        self.app._runtime.router.process(Input(kind, key=key))

    async def test_pointer_actions_match_parts_ids_and_primary_button(self):
        for target in ("action", "close"):
            self.pointer("action", "pointer_up")
            self.pointer("action", "pointer_down", button=3)
            self.pointer(target, "pointer_up", button=3)
        self.pointer("action", "pointer_down")
        self.pointer("action", "pointer_up", pointer_id=8)
        self.assertTrue(self.alert._pressed)
        self.pointer("close", "pointer_up")
        await self.drain()
        self.assertTrue(self.alert.visible)
        self.assertFalse(self.actions)
        self.pointer("action", "pointer_down")
        self.pointer("action", "pointer_up")
        await self.drain()
        self.assertEqual(len(self.actions), 1)
        self.assertEqual(self.actions[0].origin, "user")
        self.assertTrue(self.alert.visible)
        self.pointer("close", "pointer_down")
        self.pointer("close", "pointer_up")
        await self.drain()
        self.assertFalse(self.alert.visible)
        self.assertEqual(len(self.dismissals), 1)
        self.assertEqual(self.dismissals[0].origin, "user")
        self.alert.dismiss()
        await self.drain()
        self.assertEqual(len(self.dismissals), 1)

    async def test_keyboard_release_matching_arrows_escape_and_focus(self):
        self.alert.focus()
        self.key("Enter", "key_up")
        self.key("Enter")
        self.key("Enter")
        self.key("Space", "key_up")
        await self.drain()
        self.assertFalse(self.actions)
        self.key("Enter", "key_up")
        await self.drain()
        self.assertEqual(len(self.actions), 1)
        self.key("Space")
        self.key("ArrowRight")
        self.key("Space", "key_up")
        await self.drain()
        self.assertTrue(self.alert.visible)
        self.key("Home")
        self.key("Space")
        self.key("Space", "key_up")
        await self.drain()
        self.assertEqual(len(self.actions), 2)
        self.key("Escape", "key_up")
        self.assertTrue(self.alert.visible)
        self.key("Escape")
        self.assertTrue(self.alert.visible)
        self.key("Escape", "key_up")
        await self.drain()
        self.assertFalse(self.alert.visible)
        self.assertEqual(len(self.dismissals), 1)

    async def test_visibility_disable_blur_cancel_and_property_changes_abort_gestures(self):
        for field, value in (("enabled", False), ("visible", False), ("action_text", "Updated")):
            self.alert.update(visible=True, enabled=True, action_text="Review")
            arrange(self.app, self.host)
            self.pointer("action", "pointer_down")
            setattr(self.alert, field, value)
            self.alert.handle_input(part_input(self.alert, "action", "pointer_up"))
            await self.drain()
            self.assertFalse(self.actions)
            self.assertFalse(self.alert._pressed)
        self.alert.update(visible=True, enabled=True)
        self.alert.handle_input(Input("key_down", key="Space"))
        self.alert.handle_input(Input("blur"))
        self.alert.handle_input(Input("key_up", key="Space"))
        self.alert.handle_input(part_input(self.alert, "action", "pointer_down"))
        self.alert.handle_input(Input("pointer_cancel", pointer_id=7))
        self.alert.handle_input(part_input(self.alert, "action", "pointer_up"))
        await self.drain()
        self.assertFalse(self.actions)

    async def test_optional_actions_and_inherited_disabled_state(self):
        self.alert.update(action_text="", dismissible=False)
        self.alert.focus()
        for key in ("Enter", "Space", "Escape"):
            self.key(key)
            self.key(key, "key_up")
        self.alert.activate()
        self.alert.dismiss()
        await self.drain()
        self.assertFalse(self.actions)
        self.assertFalse(self.dismissals)
        self.assertTrue(self.alert.visible)
        self.alert.update(action_text="Review", dismissible=True)
        self.app.enabled = False
        self.alert.activate()
        self.alert.dismiss()
        await self.drain()
        self.assertFalse(self.actions)
        self.assertFalse(self.dismissals)

    async def test_program_actions_and_measurement_change_events(self):
        self.alert.activate()
        await self.drain()
        self.assertEqual(self.actions[0].origin, "program")
        self.alert.dismiss()
        self.alert.visible = True
        self.alert.dismiss()
        await self.drain()
        self.assertEqual([event.origin for event in self.dismissals], ["program", "program"])
        meter = Meter(parent=self.app, value=20)
        seen = []
        meter.changed.connect(seen.append)
        meter.value = 50
        meter.update(value=60, maximum=200)
        await self.drain()
        self.assertEqual([(event.old_value, event.new_value, event.origin) for event in seen],
                         [(20, 50, "program"), (50, 60, "program")])
