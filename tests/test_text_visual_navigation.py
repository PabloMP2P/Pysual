"""Text representation and navigation regressions using bundled font metrics."""

from _ui_testcase import UIOwnerTestCase
from test_library import RecordingHost

from pysual import App, TextBox, light
from pysual.backends._web_svg import SVGRenderer
from pysual.graphemes import boundaries
from pysual.host import Input
from pysual.layout import arrange
from pysual.painting import paint_tree, resolve_style
from pysual.runtime import Runtime


class FontHost(SVGRenderer, RecordingHost):
    def __init__(self):
        RecordingHost.__init__(self)
        SVGRenderer.__init__(self)
        self.carets = []
        self.draws = []
        self.rectangles = []

    def present(self):
        self.publish_scene()

    def bump_resource_revision(self):
        self._resource_revision += 1

    def caret(self, x, y, height, color):
        self.carets.append((x, y, height))
        return False

    def text(self, text, x, y, color, size, mono=False):
        self.draws.append(text)
        super().text(text, x, y, color, size, mono)

    def rect(self, rect, color, *args, **kwargs):
        self.rectangles.append((rect, color))
        super().rect(rect, color, *args, **kwargs)


class TextVisualNavigationTests(UIOwnerTestCase):
    def entry(self, **kwargs):
        app, host = App(width=640, height=400, theme=light(), reduce_motion=True), FontHost()
        self.addCleanup(app.destroy)
        entry = TextBox(parent=app, left=20, top=20,
                        **{"width": 500, "height": 140, "multiline": True, **kwargs})
        runtime = Runtime(app, host)
        app._runtime = runtime
        self.addCleanup(setattr, app, "_runtime", None)
        runtime.router.focus = entry
        arrange(app, host)
        return app, entry, host, runtime

    def key(self, runtime, key, *, shift=False):
        runtime.router.process(Input("key_down", key=key, shift=shift))

    def width(self, host, entry, text):
        style = resolve_style(entry)
        return host.measure(text, style.font_size,
                            entry.monospace or style.font_family == "mono")[0]

    def nearest(self, host, entry, line, x):
        # Independent exhaustive oracle over legal document insertions. This
        # also checks the binary-search hit-testing path used by navigation.
        def width(column):
            prefix = line[:column]
            display = ("•" * (len(boundaries(prefix)) - 1) if entry.password
                       else prefix.expandtabs(entry.tab_size))
            return self.width(host, entry, display)
        return min(boundaries(line), key=lambda col: (abs(width(col) - x), -col))

    def test_password_toggle_reveals_caret_without_changing_document_or_history(self):
        app, entry, host, _ = self.entry(
            width=200, height=40, multiline=False, password=True, text="W" * 24)
        entry.replace_selection("W")
        paint_tree(app, host)
        masked_scroll = entry.capture_view_state()[3]
        before = (entry.text, entry.selection_range, entry._undo[:], entry._redo[:])
        for password in (False, True):
            with self.subTest(password=password):
                entry.password = password
                paint_tree(app, host)
                caret_x = host.carets[-1][0]
                self.assertGreaterEqual(caret_x, entry.bounds.x + entry._text_left())
                self.assertLess(caret_x, entry.bounds.right)
                self.assertEqual(
                    (entry.text, entry.selection_range, entry._undo, entry._redo), before)
                if not password:
                    self.assertGreater(entry.capture_view_state()[3], masked_scroll)
        self.assertAlmostEqual(entry.capture_view_state()[3], masked_scroll)
        # An ordinary repaint must respect deliberate scrolling away from it.
        entry.restore_view_state((25, 25, 0, 0.0))
        paint_tree(app, host)
        self.assertEqual(entry.capture_view_state()[3], 0)

    def test_selection_edges_match_caret_positions_in_both_directions(self):
        cases = (
            ("To do", 1, 2, {}),
            ("AV", 1, 2, {}),
            ("a\tTo do", 3, 4, {}),
            ("a\tTo do", 1, 5, {}),
            ("a\tTo do", 1, 5, {"monospace": True}),
            ("e\u0301\tTo do", 2, 5, {"password": True}),
        )
        for size in (15, 48):
            for text, start, end, options in cases:
                with self.subTest(size=size, text=text, options=options):
                    app, entry, host, _ = self.entry(text=text, font_size=size, **options)
                    edges = []
                    for offset in (start, end):
                        entry.select(offset, offset)
                        paint_tree(app, host)
                        edges.append(host.carets[-1][0])
                    for anchor, caret in ((start, end), (end, start)):
                        entry.select(anchor, caret)
                        host.rectangles.clear()
                        paint_tree(app, host)
                        selection = [rect for rect, color in host.rectangles
                                     if color == app.theme.tokens.selection]
                        self.assertEqual(len(selection), 1)
                        self.assertAlmostEqual(selection[0].x, edges[0])
                        self.assertAlmostEqual(selection[0].right, edges[1])
                        self.assertAlmostEqual(host.carets[-1][0],
                                               edges[1] if caret == end else edges[0])

    def test_arrows_keep_visual_x_across_a_short_line_in_both_directions(self):
        lines = ["W" * 6, "i" * 40, "x", "i" * 40]
        _, entry, host, runtime = self.entry(text="\n".join(lines))
        entry.select(5, 5)
        x = self.width(host, entry, "W" * 5)
        target = self.nearest(host, entry, lines[1], x)
        self.assertGreater(target, 5)
        starts = entry._lines()[1]
        for row, column in ((1, target), (2, 1), (3, target)):
            self.key(runtime, "ArrowDown")
            self.assertEqual(entry.selection_range, (starts[row] + column,) * 2)
        for row, column in ((2, 1), (1, target), (0, 5)):
            self.key(runtime, "ArrowUp")
            self.assertEqual(entry.selection_range, (starts[row] + column,) * 2)

    def test_page_movement_and_shift_preserve_visual_x_and_anchor(self):
        lines = ["W" * 6] + ["i" * 40] * 20
        _, entry, host, runtime = self.entry(text="\n".join(lines), height=100)
        entry.select(5, 5)
        x = self.width(host, entry, "W" * 5)
        page = entry._page_rows()
        self.assertGreater(page, 1)
        self.key(runtime, "PageDown", shift=True)
        offset = entry._lines()[1][page] + self.nearest(host, entry, lines[page], x)
        self.assertEqual(entry.selection_range, (5, offset))
        self.assertEqual(entry._anchor, 5)
        self.key(runtime, "PageUp", shift=True)
        self.assertEqual(entry.selection_range, (5, 5))

    def test_tabs_and_graphemes_choose_nearest_legal_insertion(self):
        for target in ("\t" + "i" * 40, ("e\u0301" * 24), ("i\u0301👩‍💻" * 12)):
            with self.subTest(target=target):
                _, entry, host, runtime = self.entry(text="W\tWWW\n" + target)
                entry.select(5, 5)
                x = self.width(host, entry, "W\tWWW".expandtabs(entry.tab_size))
                self.key(runtime, "ArrowDown", shift=True)
                column = entry._caret - entry._lines()[1][1]
                self.assertEqual(column, self.nearest(host, entry, target, x))
                self.assertIn(column, boundaries(target))
                self.assertEqual(entry._anchor, 5)

    def test_horizontal_navigation_and_editing_reset_the_preferred_position(self):
        for action in ("left", "home", "edit"):
            with self.subTest(action=action):
                _, entry, host, runtime = self.entry(text="W" * 20 + "\n" + "i" * 40)
                entry.select(3, 3)
                self.key(runtime, "ArrowDown")
                if action == "left":
                    for _ in range(6):
                        self.key(runtime, "ArrowLeft")
                elif action == "home":
                    self.key(runtime, "Home")
                else:
                    runtime.router.process(Input("text", text="WWW"))
                lines, starts = entry._lines()
                x = self.width(host, entry, lines[1][:entry._caret - starts[1]])
                expected = self.nearest(host, entry, lines[0], x)
                self.assertNotEqual(expected, 3)
                self.key(runtime, "ArrowUp")
                self.assertEqual(entry.selection_range, (expected, expected))

    def test_monospace_keeps_display_column_fast_path(self):
        for options in ({"monospace": True}, {"font_family": "mono"}):
            with self.subTest(options=options):
                _, entry, _, runtime = self.entry(text="a\tb\n123456\nx\n123456", **options)
                entry.select(3, 3)
                def no_pointer_search(*args):
                    self.fail("Monospace navigation should retain its column path")
                entry._pointer_columns = no_pointer_search
                starts = entry._lines()[1]
                for row, column in ((1, 5), (2, 1), (3, 5)):
                    self.key(runtime, "ArrowDown")
                    self.assertEqual(entry.selection_range, (starts[row] + column,) * 2)

    def test_font_change_restarts_the_preferred_visual_position(self):
        _, entry, host, runtime = self.entry(text="W" * 10 + "\n" + "i" * 40)
        entry.select(5, 5)
        self.key(runtime, "ArrowDown")
        entry.font_size = resolve_style(entry).font_size * 2
        lines, starts = entry._lines()
        x = self.width(host, entry, lines[1][:entry._caret - starts[1]])
        expected = self.nearest(host, entry, lines[0], x)
        self.key(runtime, "ArrowUp")
        self.assertEqual(entry.selection_range, (expected, expected))

    def test_resource_metric_change_restarts_the_preferred_visual_position(self):
        _, entry, host, runtime = self.entry(text="W" * 10 + "\n" + "i" * 40)
        entry.select(5, 5)
        self.key(runtime, "ArrowDown")
        original_measure = host.measure
        def wider(text, size, mono=False):
            width, height = original_measure(text, size, mono)
            return width * 2, height
        host.measure = wider
        host.bump_resource_revision()
        lines, starts = entry._lines()
        x = self.width(host, entry, lines[1][:entry._caret - starts[1]])
        expected = self.nearest(host, entry, lines[0], x)
        self.key(runtime, "ArrowUp")
        self.assertEqual(entry.selection_range, (expected, expected))

    def test_password_navigation_uses_masked_grapheme_widths(self):
        target = "e\u0301👩‍💻\t" * 12
        lines = ["W" * 6, target, "x", target]
        _, entry, host, runtime = self.entry(text="\n".join(lines), password=True)
        entry.select(5, 5)
        x = self.width(host, entry, "•" * 5)
        starts = entry._lines()[1]
        for row in (1, 2, 3):
            self.key(runtime, "ArrowDown", shift=True)
            column = self.nearest(host, entry, lines[row], x)
            self.assertEqual(entry._caret, starts[row] + column)
            self.assertIn(column, boundaries(lines[row]))
            self.assertEqual(entry._anchor, 5)

    def test_placeholder_flattens_lines_only_for_single_line_fields(self):
        for multiline in (False, True):
            with self.subTest(multiline=multiline):
                app, entry, host, runtime = self.entry(
                    placeholder="First\r\nSecond\rThird\nFourth", multiline=multiline)
                runtime.router.focus = None
                paint_tree(app, host)
                self.assertIn("First Second Third Fourth" if not multiline
                              else entry.placeholder, host.draws)
                self.assertEqual(entry.placeholder, "First\r\nSecond\rThird\nFourth")
