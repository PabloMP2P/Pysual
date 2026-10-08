"""Window chrome uses themed materials without changing close hit geometry."""

from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, Style, SubWindow, Theme
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import Painter


class ChromeHost(RecordingHost):
    def __init__(self):
        super().__init__()
        self.operations = []

    def styled_rect(self, bounds, style):
        self.operations.append(("surface", bounds, style))
        return True

    def rect(self, bounds, fill, *args, **kwargs):
        self.operations.append(("rect", bounds, fill))

    def text(self, text, x, y, color, size, mono=False):
        self.operations.append(("text", text, x, y, color, size, mono))


class WindowChromeTests(UIOwnerTestCase):
    def setUp(self):
        self.app = App(width=500, height=300)
        self.host = ChromeHost()
        self.addCleanup(self.app.destroy)

    def window(self, theme):
        window = SubWindow(parent=self.app, title="Preferences", left=12, top=15,
                           width=320, height=180, theme_override=theme,
                           resizable=False)
        arrange(self.app, self.host)
        return window

    def paint(self, window):
        self.host.operations.clear()
        window.paint(Painter(self.host, window.bounds, window.effective_theme, window))
        return self.host.operations

    def close_face(self, window):
        return [op for op in self.paint(window) if op[0] == "surface"][-1]

    def pointer(self, window, kind, *, over_close=True):
        hit = window._close_bounds()
        x = hit.x + hit.width / 2 if over_close else 5
        window.handle_input(Input(kind, window.bounds.x + x,
                                  window.bounds.y + hit.height / 2))

    def test_default_close_is_transparent_and_inherits_title_typography(self):
        theme = Theme().styled(SubWindow, Style(fill="#15366b", foreground="#ffffff",
                            font_size=18, font_family="mono"), part="titlebar")
        window = self.window(theme)
        operations = self.paint(window)
        face = [op for op in operations if op[0] == "surface"][-1][2]
        glyph = next(op for op in operations if op[:2] == ("text", "×"))
        self.assertEqual(face.fill, "#00000000")
        self.assertEqual(face.border_width, 0)
        self.assertEqual(glyph[4], "#ffffff")
        self.assertTrue(glyph[6])

    def test_close_face_uses_its_own_style_and_retains_the_full_hit_target(self):
        theme = Theme().styled(SubWindow, Style(fill="#bf3024", foreground="#ffffff",
                            radius=4, border_width=1, padding=5), part="close")
        window = self.window(theme)
        original_hit = window._close_bounds()
        face = self.close_face(window)
        self.assertEqual(face[2].fill, "#bf3024")
        self.assertEqual(face[1].width, original_hit.width - 10)
        self.assertEqual(window._close_bounds(), original_hit)
        glyph = next(op for op in self.host.operations if op[:2] == ("text", "×"))
        self.assertEqual(glyph[4], "#ffffff")

    def test_close_states_follow_its_hit_region_and_cancelled_press(self):
        theme = Theme().styled(SubWindow, Style(fill="#111111"), part="close")
        for state, color in (("hover", "#222222"), ("pressed", "#333333"),
                             ("disabled", "#444444")):
            theme = theme.styled(SubWindow, Style(fill=color), part="close", state=state)
        window = self.window(theme)
        window._hover = True  # The input router owns the window-level hover bit.
        self.pointer(window, "pointer_move", over_close=False)
        self.assertEqual(self.close_face(window)[2].fill, "#111111")
        revision = window._paint_revision
        self.pointer(window, "pointer_move")
        self.assertGreater(window._paint_revision, revision)
        self.assertEqual(self.close_face(window)[2].fill, "#222222")
        self.pointer(window, "pointer_down")
        self.assertEqual(self.close_face(window)[2].fill, "#333333")
        self.pointer(window, "pointer_move", over_close=False)
        self.assertEqual(self.close_face(window)[2].fill, "#111111")
        window.handle_input(Input("pointer_cancel"))
        self.assertIsNone(window._close_armed)
        self.assertFalse(window._close_hover)
        window.enabled = False
        self.assertEqual(self.close_face(window)[2].fill, "#444444")

    def test_pattern_is_cleared_only_behind_the_elided_title(self):
        theme = Theme().styled(SubWindow, Style(fill="#eeeeee", foreground="#111111",
                            pattern="scanlines", pattern_color="#111111",
                            pattern_spacing=2), part="titlebar")
        window = self.window(theme)
        window.title = "A very long title " * 12
        operations = self.paint(window)
        caption_index = next(i for i, op in enumerate(operations)
                             if op[0] == "text" and op[1] != "×")
        strip = operations[caption_index - 1]
        caption = operations[caption_index]
        self.assertEqual(strip[0], "rect")
        self.assertEqual(strip[2], "#eeeeee")
        self.assertLessEqual(strip[1].x, caption[2])
        self.assertGreaterEqual(strip[1].right,
                                caption[2] + self.host.measure(caption[1], caption[5])[0])
        self.assertLessEqual(strip[1].right, window.bounds.x + window._close_bounds().x)
        self.assertEqual(operations[1][2].pattern, "scanlines")

    def test_unpatterned_and_unclosable_windows_do_not_gain_decorations(self):
        window = self.window(Theme())
        window.closable = False
        operations = self.paint(window)
        self.assertEqual(len([op for op in operations if op[0] == "surface"]), 2)
        self.assertFalse(any(op[0] == "rect" for op in operations))
        self.assertFalse(any(op[:2] == ("text", "×") for op in operations))

    def resize(self, window, dx, dy):
        x, y = window.bounds.right - 5, window.bounds.bottom - 5
        window.handle_input(Input("pointer_down", x, y, button=1))
        window.handle_input(Input("pointer_move", x + dx, y + dy))
        arrange(self.app, self.host)
        window.handle_input(Input("pointer_up", x + dx, y + dy, button=1))

    def test_resize_stops_at_padded_parent_and_grip_can_be_reused(self):
        self.host.size = (500, 300)
        self.app.padding = 17
        window = SubWindow(parent=self.app, left=45, top=35, width=180, height=120)
        arrange(self.app, self.host)
        area = self.app.content_bounds(self.app.bounds)
        original_position = window.bounds.x, window.bounds.y

        self.resize(window, 1000, 1000)
        self.assertEqual((window.bounds.right, window.bounds.bottom), (area.right, area.bottom))
        self.assertEqual((window.bounds.x, window.bounds.y), original_position)

        self.resize(window, -25, -20)
        self.assertEqual((window.bounds.right, window.bounds.bottom), (area.right - 25, area.bottom - 20))

    def test_nested_window_resize_uses_parent_content_below_titlebar(self):
        parent = SubWindow(parent=self.app, left=37, top=28, width=430, height=270, padding=13)
        window = SubWindow(parent=parent, left=31, top=19, width=170, height=100)
        arrange(self.app, self.host)
        area = parent.content_bounds(parent.bounds)

        self.resize(window, 1000, 1000)
        self.assertEqual((window.bounds.x, window.bounds.y), (area.x + 31, area.y + 19))
        self.assertEqual((window.bounds.right, window.bounds.bottom), (area.right, area.bottom))

    def test_resize_preserves_minimums_when_parent_cannot_fit_them(self):
        self.host.size = (300, 200)
        window = SubWindow(parent=self.app, left=160, top=110, width=160, height=100,
                           min_width=150, min_height=90)
        arrange(self.app, self.host)

        self.resize(window, -1000, -1000)
        self.assertEqual((window.width, window.height), (150, 90))
        self.resize(window, 1000, 1000)
        self.assertEqual((window.width, window.height), (150, 90))

    def test_resize_uses_arranged_anchor_position_after_parent_shrinks(self):
        self.app.update(width=700, height=500)
        self.host.size = (700, 500)
        window = SubWindow(parent=self.app, left=450, top=300, width=200, height=150,
                           anchor="right,bottom")
        arrange(self.app, self.host)
        self.host.size = (400, 300)
        arrange(self.app, self.host)
        self.assertEqual((window.bounds.x, window.bounds.y), (150, 100))

        x, y = window.bounds.right - 5, window.bounds.bottom - 5
        window.handle_input(Input("pointer_down", x, y, button=1))
        for _ in range(2):
            window.handle_input(Input("pointer_move", x + 1, y + 1))
            arrange(self.app, self.host)
            self.assertEqual((window.width, window.height), (201, 151))
            self.assertEqual((window.bounds.right, window.bounds.bottom), (350, 250))
        for _ in range(2):
            window.handle_input(Input("pointer_move", x + 1000, y + 1000))
            arrange(self.app, self.host)
            self.assertEqual((window.width, window.height), (250, 200))
            self.assertEqual((window.bounds.right, window.bounds.bottom), (350, 250))
        window.handle_input(Input("pointer_up", x + 1000, y + 1000, button=1))
