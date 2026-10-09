"""Fixed captions stay on one visual line without changing their source data."""

import json
import unittest

from _ui_testcase import AsyncUIOwnerTestCase, UIOwnerTestCase
from test_fixed_collection_rows import CellHost
from test_library import RecordingHost

from pysual import (
    App, Button, ChartSeries, ChartSlice, CheckBox, DataGrid, DonutChart,
    Dropdown, GridColumn, GridRow, GroupBox, Label, LineChart, ListView,
    MenuBar, MenuGroup, MenuItem, RadioButton, Rect, SubWindow, TabControl,
    Toggle, TreeNode, TreeView, dark,
)
from pysual.backends._web_svg import SVGRenderer
from pysual.graphemes import floor_boundary
from pysual.hints import _Hint
from pysual.layout import arrange
from pysual.menus import _MenuRows
from pysual.painting import Painter, paint_tree, resolve_style
from pysual.runtime import Runtime


CAPTION = "Alpha\r\nBeta\nGamma\rDelta"
DISPLAY = "Alpha Beta Gamma Delta"


class BatchCaptionHost(SVGRenderer):
    def __init__(self):
        super().__init__()
        self.measurements = 0
        self.batches = 0

    def measure(self, text, size, mono=False):
        self.measurements += 1
        return super().measure(text, size, mono)

    def measure_many(self, texts, size, mono=False):
        self.batches += 1
        return [self.measure(text, size, mono) for text in texts]


class CaptionFittingTests(unittest.TestCase):
    def test_zero_advance_prefixes_bound_batches_and_total_measurement_work(self):
        class BudgetHost:
            def __init__(self):
                self.batches = []
                self.characters = 0

            def measure(self, text, size, mono=False):
                self.characters += len(text)
                return len(text.lstrip("\u200b\x01")) * 8, 16

            def measure_many(self, texts, size, mono=False):
                payload = json.dumps(
                    {"op": "measure_many", "texts": texts, "size": size, "mono": mono},
                    ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")
                self.batches.append((len(texts), len(payload)))
                return [self.measure(text, size, mono) for text in texts]

        # Cover both failures seen with native captions, plus probes which are
        # themselves too large to share a batch. Controls exercise JSON's
        # six-byte escaping, not just the UTF-8 size of ordinary characters.
        for marker in ("\u200b", "\x01"):
            for length in (9000, 16000, 200000):
                with self.subTest(marker=repr(marker), length=length):
                    host = BudgetHost()
                    painter = Painter(host, Rect(0, 0, 35, 30), dark())
                    source = marker * length + "abcde"
                    self.assertEqual(painter.elide(source, 35), marker * length + "abc…")
                    self.assertTrue(host.batches)
                    self.assertTrue(all(count <= 64 and size <= 1024 * 1024
                                        for count, size in host.batches), host.batches)
                    self.assertLess(host.characters, len(source) * 80)

    def test_nonpositive_width_skips_metrics_but_keeps_font_validation(self):
        for batched in (False, True):
            host = BatchCaptionHost()
            if not batched:
                host.measure_many = None
            painter = Painter(host, Rect(0, 0, 100, 30), dark())
            for width in (0, -10):
                with self.subTest(batched=batched, width=width):
                    self.assertEqual(painter.elide("Caption", width), "")
                    with self.assertRaises(ValueError):
                        painter.elide("Caption", width, font_family="unknown")
            self.assertEqual((host.measurements, host.batches), (0, 0))
            # A narrow real glyph can fit even when an ellipsis cannot.
            width = SVGRenderer().measure("i", 15)[0]
            self.assertEqual(painter.elide("i", width, size=15), "i")
            self.assertGreater(host.measurements, 0)

    def test_long_narrow_captions_bound_measurement_work(self):
        for length in (64, 128, 256, 512):
            with self.subTest(length=length):
                host = BatchCaptionHost()
                painter = Painter(host, Rect(0, 0, 150, 30), dark())
                text = ("A document title " * 40)[:length]
                result = painter.elide(text, 150)
                self.assertTrue(result.endswith("…"))
                self.assertLess(host.measurements, 40)
                self.assertLessEqual(host.batches, 2)
                self.assertLessEqual(host.measure(result, 15)[0], 150)

    def test_batch_fitting_matches_exhaustive_prefixes_with_bundled_fonts(self):
        host, reference = BatchCaptionHost(), SVGRenderer()
        painter = Painter(host, Rect(0, 0, 300, 30), dark())
        for pattern in ("AV To document ", "café e\u0301 ", "release 👩‍💻 ", "👨‍👩‍👧‍👦 end "):
            for length in (0, 1, 32, 33, 64, 128, 256, 512):
                text = (pattern * 512)[:length]
                for width in (0, 10, 75, 150, 300, 900):
                    for mono in (False, True):
                        with self.subTest(pattern=pattern, length=length, width=width, mono=mono):
                            if reference.measure(text, 15, mono)[0] <= width:
                                expected = text
                            elif reference.measure("…", 15, mono)[0] > width:
                                expected = ""
                            else:
                                fitted = 0
                                for index in range(1, len(text) + 1):
                                    if reference.measure(text[:index] + "…", 15, mono)[0] > width:
                                        break
                                    fitted = index
                                expected = text[:floor_boundary(text, fitted)] + "…"
                            self.assertEqual(painter.elide(text, width, mono=mono), expected)


class CaptionHost(SVGRenderer, RecordingHost):
    def __init__(self):
        RecordingHost.__init__(self)
        SVGRenderer.__init__(self)
        self.draws = []
        self.reset_scene("Fixed captions", 640, 400)

    def present(self):
        self.publish_scene()

    def text(self, text, x, y, color, size, mono=False):
        self.draws.append((text, x, y, self.measure(text, size, mono)[1]))
        super().text(text, x, y, color, size, mono)


def menu_rows(app, text=CAPTION):
    rows = _MenuRows(parent=app, items=(text, "Next"), width=450, height=100)
    rows._entries = (MenuItem("a", text), MenuItem("b", "Next"))
    return rows


class FixedCaptionTests(UIOwnerTestCase):
    def app(self):
        app = App(width=640, height=400)
        self.addCleanup(app.destroy)
        return app

    def paint(self, app, host=None):
        host = host or CaptionHost()
        arrange(app, host)
        paint_tree(app, host)
        return host

    def test_fixed_caption_sites_use_one_line_with_real_font_metrics(self):
        factories = {
            "grid": lambda app: DataGrid(parent=app, width=450, height=120,
                columns=(GridColumn("a", CAPTION, width=440),),
                rows=(GridRow("a", (CAPTION,)),)),
            "tree": lambda app: TreeView(parent=app, width=450, height=120,
                nodes=(TreeNode("a", CAPTION),)),
            "list": lambda app: ListView(parent=app, width=450, height=120,
                items=(CAPTION,)),
            "dropdown": lambda app: Dropdown(parent=app, width=450,
                items=(CAPTION,), selected_index=0),
            "dropdown placeholder": lambda app: Dropdown(parent=app, width=450,
                placeholder=CAPTION),
            "menu rows": menu_rows,
            "menu bar": lambda app: MenuBar(parent=app, width=450,
                groups=(MenuGroup(CAPTION, (MenuItem("a", "Open"),)),)),
            "tab": lambda app: TabControl(parent=app, width=450, height=240,
                tab_width=400).tab_page(title=CAPTION),
            "group": lambda app: GroupBox(parent=app, title=CAPTION, width=450),
            "subwindow": lambda app: SubWindow(parent=app, title=CAPTION, width=450),
            "line chart": lambda app: LineChart(parent=app, title=CAPTION,
                series=(ChartSeries("a", CAPTION, ((0, 1), (1, 2))),),
                width=450, height=240),
            "donut chart": lambda app: DonutChart(parent=app, title=CAPTION,
                slices=(ChartSlice("a", CAPTION, 1),), width=600, height=300),
            "hint": lambda app: _Hint(parent=app, text=CAPTION, width=450, height=32),
        }
        for name, factory in factories.items():
            with self.subTest(control=name):
                app = self.app()
                factory(app)
                host = self.paint(app)
                texts = [draw[0] for draw in host.draws]
                self.assertIn(DISPLAY, texts)
                self.assertTrue(all("\n" not in text and "\r" not in text for text in texts))
                self.assertIn(DISPLAY, host.export_svg())
                app.destroy()

    def test_collection_captions_fit_their_rows_and_leave_next_record_intact(self):
        for kind in ("grid", "tree", "list"):
            with self.subTest(control=kind):
                app = self.app()
                if kind == "grid":
                    control = DataGrid(parent=app, width=450, height=140,
                        columns=(GridColumn("a", "Heading", width=440),),
                        rows=(GridRow("a", (CAPTION,)), GridRow("b", ("Next",))),
                        selected_key="a")
                    self.assertEqual(control.selected_cell_text, CAPTION)
                    top = control.header_height
                elif kind == "tree":
                    control = TreeView(parent=app, width=450, height=140,
                        nodes=(TreeNode("a", CAPTION), TreeNode("b", "Next")))
                    top = 0
                else:
                    control = ListView(parent=app, width=450, height=140,
                        items=(CAPTION, "Next"))
                    top = 0
                host = self.paint(app)
                caption = next(draw for draw in host.draws if draw[0] == DISPLAY)
                following = next(draw for draw in host.draws if draw[0] == "Next")
                self.assertGreaterEqual(caption[2], top)
                self.assertLessEqual(caption[2] + caption[3], top + control.row_height)
                self.assertEqual(following[2] - caption[2], control.row_height)
                app.destroy()

    def test_narrow_caption_elides_the_combined_line_before_drawing(self):
        app = self.app()
        control = Dropdown(parent=app, width=100, items=("Alpha\nBeta\nGamma",),
                           selected_index=0)
        host = self.paint(app)
        text = host.draws[0][0]
        self.assertTrue(text.endswith("…"))
        self.assertLessEqual(host.measure(text, 15)[0], control.bounds.width - 40)
        self.assertEqual(control.selected_item, "Alpha\nBeta\nGamma")

    def test_terminal_rows_remain_adjacent_for_multiline_source_values(self):
        for kind in ("grid", "tree", "list", "menu"):
            with self.subTest(control=kind):
                app = self.app()
                text = "Alpha\r\nBeta\nGamma"
                if kind == "grid":
                    DataGrid(parent=app, width=320, height=96,
                        columns=(GridColumn("a", "Heading", width=310),),
                        rows=(GridRow("a", (text,)), GridRow("b", ("Next",))))
                elif kind == "tree":
                    TreeView(parent=app, width=320, height=96,
                        nodes=(TreeNode("a", text), TreeNode("b", "Next")))
                elif kind == "list":
                    ListView(parent=app, width=320, height=96, items=(text, "Next"))
                else:
                    menu_rows(app, text).width = 320
                rows = self.paint(app, CellHost())._renderer.snapshot_rows()
                first = next(i for i, row in enumerate(rows) if "Alpha Beta Gamma" in row)
                following = next(i for i, row in enumerate(rows) if "Next" in row)
                self.assertEqual(following, first + 1)
                app.destroy()

    def test_auto_headings_and_menu_hit_boxes_measure_the_single_line_display(self):
        app, host = self.app(), CaptionHost()
        group = GroupBox(parent=app, title=CAPTION)
        window = SubWindow(parent=app, title=CAPTION, width=None, height=None, top=100)
        bar = MenuBar(parent=app, width=600, top=250,
            groups=(MenuGroup(CAPTION, (MenuItem("a", "Open"),)),))
        self.paint(app, host)
        for control, part in ((group, "heading"), (window, "titlebar")):
            style = resolve_style(control, part)
            width = host.measure(DISPLAY, style.font_size, style.font_family == "mono")[0]
            self.assertGreaterEqual(control.bounds.width, width + control.padding * 2)
        style = resolve_style(bar, "item")
        expected = host.measure(DISPLAY, style.font_size, style.font_family == "mono")[0] + 24
        self.assertAlmostEqual(bar._headers[0].width, expected)
        self.assertEqual((group.title, window.title, bar.groups[0].text), (CAPTION,) * 3)

    def test_intentional_multiline_controls_and_donut_center_are_preserved(self):
        text = "Alpha\nBeta\nGamma"
        for cls in (Label, Button, CheckBox, Toggle, RadioButton):
            with self.subTest(control=cls.__name__):
                app = self.app()
                control = cls(parent=app, text=text, width=450)
                host = self.paint(app)
                self.assertIn(text, [draw[0] for draw in host.draws])
                self.assertGreaterEqual(control.bounds.height, host.measure(text, 15)[1])
                self.assertEqual(control.text, text)
                app.destroy()
        app = self.app()
        DonutChart(parent=app, center_text=text, width=450, height=300)
        self.assertIn(text, [draw[0] for draw in self.paint(app).draws])


class FixedCaptionClipboardTests(AsyncUIOwnerTestCase):
    async def test_grid_copy_retains_original_line_breaks_after_paint(self):
        app, host = App(), RecordingHost()
        self.addCleanup(app.destroy)
        grid = DataGrid(parent=app, width=450, height=140,
            columns=(GridColumn("a", "Heading", width=440),),
            rows=(GridRow("a", (CAPTION,)),), selected_key="a")
        arrange(app, host)
        paint_tree(app, host)
        runtime = Runtime(app, host)
        runtime.router.set_focus(grid)
        await runtime.router.clipboard("c")
        self.assertEqual(host.clipboard, CAPTION)
        self.assertEqual(grid.rows[0].cells[0], CAPTION)
