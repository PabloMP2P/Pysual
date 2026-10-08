"""Actual Python event handlers are shared by the UI runtime and live SVG host."""

import http.client
import json
import time
import unittest
from urllib.parse import urlsplit

from pysual import Button, Window
from pysual.backends.web import WebHost


class WebApplicationTests(unittest.TestCase):
    def test_live_pointer_input_invokes_python_callback_and_updates_svg(self):
        class Counter(Window):
            def build(self):
                self.action_button = Button(text="Before", left=20, top=20, width=120, height=40)

            def action_button_on_click(self, event):
                self.action_button.text = "After Python"

        host = WebHost(open_browser=False)
        app = Counter(width=240, height=160)
        app.run(backend=host)
        self.addCleanup(app.destroy)
        self.addCleanup(app.close)
        endpoint = urlsplit(host.url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        self.addCleanup(connection.close)
        body = json.dumps({"events": [
            {"kind": "pointer_down", "x": 40, "y": 40, "button": 1},
            {"kind": "pointer_up", "x": 40, "y": 40, "button": 1},
        ]})
        connection.request("POST", "/events", body=body, headers={
            "Content-Type": "application/json", "X-Pysual-Token": endpoint.fragment})
        response = connection.getresponse()
        self.assertEqual(response.status, 200, response.read())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and "After Python" not in host.export_svg():
            time.sleep(0.01)
        self.assertEqual(app.action_button.text, "After Python")
        self.assertIn("After Python", host.export_svg())
        app.close()
        app.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
