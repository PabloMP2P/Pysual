"""Both web modes use the same device-aligned, bounded SVG gradient bands."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from pysual.backends._web_pyodide import BundledSVGHost
from pysual.geometry import Rect

NS = {"s": "http://www.w3.org/2000/svg"}


class WebGradientTests(unittest.TestCase):
    def host(self, bridge):
        with patch.dict("sys.modules", {"js": SimpleNamespace(pysualHost=bridge)}):
            return BundledSVGHost()

    def gradient(self, host):
        root = ET.fromstring(host.export_svg())
        gradients = root.findall(".//s:linearGradient[@href]", NS)
        self.assertEqual(len(gradients), 1)
        gradient = gradients[0]
        reference = gradient.attrib["href"]
        self.assertTrue(reference.startswith("#"))
        banks = root.findall(f".//s:linearGradient[@id='{reference[1:]}']", NS)
        self.assertEqual(len(banks), 1)
        return gradient, banks[0].findall("s:stop", NS)

    def test_fractional_scale_builds_one_bounded_exact_color_bank(self):
        bridge = SimpleNamespace(renderScale=1.5)
        host = self.host(bridge)
        rect = Rect(10.25, 20, 101.5, 30)
        self.assertTrue(
            host.gradient_rect(rect, "#00000000", "#010305ff", "horizontal", 8, 2)
        )
        gradient, stops = self.gradient(host)
        self.assertEqual(gradient.get("gradientUnits"), "userSpaceOnUse")
        self.assertEqual(float(gradient.get("x1")), 15 / 1.5)
        self.assertEqual(float(gradient.get("x2")), 168 / 1.5)
        bank = [stop.get("stop-color") for stop in stops[::2]]
        self.assertEqual(len(stops), 192)
        self.assertEqual(len(bank), 96)
        self.assertEqual(bank[0], "#00000000")
        self.assertEqual(bank[-1], "#010305ff")
        self.assertEqual(bank[47], "#0001027e")
        self.assertEqual(bank[48], "#01020381")
        self.assertEqual(float(stops[0].get("offset")), 0)
        self.assertEqual(float(stops[-1].get("offset")), 1)

    def test_short_vertical_gradient_retains_python_tie_rounding(self):
        bridge = SimpleNamespace(renderScale=1.5)
        host = self.host(bridge)
        self.assertTrue(
            host.gradient_rect(
                Rect(2, -1.25, 12, 1.5), "#000000", "#01030501", "vertical", 0
            )
        )
        gradient, stops = self.gradient(host)
        self.assertEqual(float(gradient.get("y1")), -2 / 1.5)
        self.assertEqual(float(gradient.get("y2")), 1 / 1.5)
        self.assertEqual([stop.get("stop-color") for stop in stops[::2]],
                         ["#000000ff", "#00020280", "#01030501"])

    def test_empty_shapes_do_not_add_svg_nodes_or_require_bridge_methods(self):
        host = self.host(SimpleNamespace(renderScale=1))
        self.assertTrue(
            host.gradient_rect(Rect(0, 0, 0, 10), "#000000", "#ffffff", "vertical", 0)
        )
        self.assertEqual(host._nodes, [])
        self.assertTrue(host.gradient_rect(
            Rect(0, 0, 10, 10), "#000000", "#ffffff", "vertical", 0
        ))
        _, stops = self.gradient(host)
        self.assertEqual(len(stops), 20)
