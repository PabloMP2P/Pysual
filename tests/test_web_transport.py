"""Exercise real loopback transport, SVG frames and asynchronous host services."""

import asyncio
import base64
import http.client
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import shutil
import subprocess
import zlib
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

from pysual import Rect
from pysual.backends._web_native import LiveSVGHost, _MAX_REQUEST
from pysual.host import CapabilityError, Input, MAX_TEXT_BYTES, Viewport
from pysual.image_resources import MAX_IMAGE_BYTES


NS = {"s": "http://www.w3.org/2000/svg"}
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class LiveSVGStartupTests(unittest.TestCase):
    def test_loopback_startup_does_not_resolve_a_hostname(self):
        host = LiveSVGHost(open_browser=False)
        self.addCleanup(host.close)
        # Reverse DNS is not needed for a numeric loopback listener and can
        # block the UI owner on systems with an unavailable resolver.
        with patch("socket.getfqdn", side_effect=AssertionError("unexpected DNS lookup")):
            host.open("Loopback", 320, 240, True, None)
        endpoint = urlsplit(host.url)
        self.assertEqual(endpoint.hostname, "127.0.0.1")
        self.assertEqual(host._server.server_name, "localhost")
        self.assertEqual(host._server.server_port, endpoint.port)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn(b"<svg", response.read())


class LiveSVGHostTests(unittest.TestCase):
    def setUp(self):
        self.host = LiveSVGHost(open_browser=False)
        self.host.open("Document <test>", 320, 240, True, None)
        self.addCleanup(self.host.close)

    def request(self, method, path, body=None, *, authenticated=True, headers=None):
        endpoint = urlsplit(self.host.url)
        connection = http.client.HTTPConnection(
            endpoint.hostname, endpoint.port, timeout=3
        )
        self.addCleanup(connection.close)
        request_headers = {"Content-Type": "application/json"}
        if authenticated:
            request_headers["X-Pysual-Token"] = endpoint.fragment
        request_headers.update(headers or {})
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())

    def frame(self, color="#ffffff"):
        self.host.begin("#111111")
        self.host.rect(Rect(10, 10, 100, 30), color, radius=5)
        self.host.text("AV & <text>", 12, 12, "#000000", 14)
        self.host.present()

    def test_present_releases_the_scene_lock_for_input(self):
        self.frame()
        revision = self.host._revision
        replies = []

        def post():
            replies.append(self.request(
                "POST", "/events", '{"events":[{"kind":"repaint"}]}'
            )[0])

        workers = [threading.Thread(target=post) for _ in range(2)]
        for worker in workers:
            worker.start()
        self.host.present()
        self.host.present()
        for worker in workers:
            worker.join(2)
            self.assertFalse(worker.is_alive(), "Input request blocked on the scene lock")
        self.assertCountEqual(replies, [200, 200])
        self.assertEqual(self.host._revision, revision)
        self.assertEqual([event.kind for event in self.host.poll()], ["repaint", "repaint"])

    def test_event_stream_delivers_successive_frames_and_rejects_a_bad_token(self):
        from urllib.parse import quote

        self.frame("#112233")
        endpoint = urlsplit(self.host.url)
        denied = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        self.addCleanup(denied.close)
        denied.request(
            "GET", "/stream?token=nope",
            headers={"Host": endpoint.netloc},
        )
        self.assertEqual(denied.getresponse().status, 403)

        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request(
            "GET", "/stream?token=" + quote(endpoint.fragment),
            headers={"Host": endpoint.netloc},
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))

        def read_event():
            data = b""
            while b"\n\n" not in data:
                piece = response.read(1)
                self.assertTrue(piece, "Event stream closed before a frame")
                data += piece
            return data

        first = read_event()
        self.assertIn(b'"revision":', first)

        def publish():
            self.host.begin("#abcdef")
            self.host.rect(Rect(1, 1, 8, 8), "#ffffff")
            self.host.present()

        threading.Thread(target=publish).start()
        second = read_event()
        self.assertIn(b"#abcdef", second)
        self.assertNotEqual(first, second)

    def test_event_stream_delivers_metadata_and_services_without_scene_changes(self):
        self.frame()
        endpoint = urlsplit(self.host.url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/stream?token=" + endpoint.fragment)
        response = connection.getresponse()
        self.assertEqual(response.status, 200)

        def packet():
            while True:
                line = response.readline()
                self.assertTrue(line, "Event stream closed before its packet")
                if line.startswith(b"data: "):
                    return json.loads(line[6:])

        revision = packet()["revision"]
        self.host.set_title("Renamed without repaint")
        renamed = packet()
        self.assertEqual(renamed["title"], "Renamed without repaint")
        self.assertEqual(renamed["revision"], revision)
        self.assertEqual(renamed["updates"], [])
        self.host.text_input(Rect(1, 2, 30, 20))
        focused = packet()
        self.assertEqual(focused["text_input"], [1, 2, 30, 20])
        self.assertEqual(focused["revision"], revision)

        async def file_request():
            task = asyncio.create_task(self.host.open_text_file())
            try:
                request = await asyncio.to_thread(packet)
                self.assertEqual(request["revision"], revision)
                self.assertEqual(request["updates"], [])
                self.assertEqual(len(request["commands"]), 1)
                command = request["commands"][0]
                self.assertEqual(command["method"], "open_text_file")
                self.host._receive({"replies": [{"id": command["id"], "value": None}]})
                self.assertIsNone(await task)
                cleared = await asyncio.to_thread(packet)
                self.assertEqual(cleared["commands"], [])
                self.assertEqual(cleared["revision"], revision)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(file_request())

    def test_multiline_text_has_explicit_baselines_matching_measured_rows(self):
        for mono in (False, True):
            with self.subTest(mono=mono):
                self.host.begin("#ffffff")
                self.host.text("A &\n\nB\n", 12, 9, "#000000", 16, mono)
                self.host.present()
                nodes = ET.fromstring(self.host.export_svg()).findall(".//s:text", NS)
                self.assertEqual([node.text for node in nodes], ["A &", "B"])
                self.assertEqual([node.get("x") for node in nodes], ["12", "12"])
                row = self.host.measure("", 16, mono)[1]
                self.assertEqual(float(nodes[1].get("y")) - float(nodes[0].get("y")), 2 * row)
                self.assertEqual(self.host.measure("A &\n\nB\n", 16, mono)[1], 4 * row)

    def test_image_fit_uses_svg_geometry_without_an_image_size_service(self):
        source = "data:image/png;base64," + base64.b64encode(PNG).decode("ascii")
        self.host.begin("#fff")
        for fit in ("stretch", "contain", "cover"):
            self.host.image(source, Rect(20, 20, 80, 100), fit=fit)
        self.host.present()
        nodes = ET.fromstring(self.host.export_svg()).findall(".//s:image", NS)
        self.assertEqual([n.get("preserveAspectRatio") for n in nodes],
                         ["none", "xMidYMid meet", "xMidYMid slice"])
        self.assertEqual(len(self.host._images), 1)
        with self.assertRaises(ValueError):
            self.host.image(source, Rect(20, 20, 80, 100), fit="unknown")

    def test_unchanged_titles_keep_metadata_revision_and_reopen_sends_title(self):
        revision = self.host._metadata_revision
        with patch.object(self.host._condition, "notify_all") as notify:
            self.host.set_title("Document <test>")
            self.host.set_title("Document <test>")
            self.assertEqual(self.host._metadata_revision, revision)
            notify.assert_not_called()
            self.host.set_title("Renamed")
            self.assertEqual(self.host._metadata_revision, revision + 1)
            notify.assert_called_once()
        packet = self.host._frame_packet(-1)
        self.assertEqual(packet["title"], "Renamed")
        self.host.close()
        self.host.open("Renamed", 320, 240, True, None)
        self.host.set_title("Renamed")
        self.assertEqual(self.host._frame_packet(-1)["title"], "Renamed")

    def test_consecutive_shapes_share_clips_and_identical_frames_keep_revision(self):
        for _ in range(2):
            self.host.begin("#111111")
            self.host.clip(Rect(0, 0, 40, 40))
            self.host.rect(Rect(1, 2, 10, 10), "#ff0000")
            self.host.clip(Rect(0, 0, 40, 40))
            self.host.rect(Rect(20, 2, 10, 10), "#00ff00")
            self.host.clip(Rect(50, 0, 10, 10))
            self.host.rect(Rect(45, 0, 20, 20), "#0000ff")
            self.host.clip(Rect(0, 0, 0, 10))
            self.host.rect(Rect(0, 0, 10, 10), "#ffffff")
            self.host.clip(None)
            self.host.rect(Rect(0, 0, 10, 10), "#aaaaaa")
            self.host.present()
            self.assertEqual(self.host._revision, 1)
        root = ET.fromstring(self.host.export_svg())
        clips = root.findall("s:g/s:defs/s:clipPath", NS)
        self.assertEqual(len(clips), 3)
        self.assertEqual(clips[-1].find("s:rect", NS).get("width"), "0")
        groups = root.findall("s:g/s:g", NS)
        self.assertEqual([len(group) for group in groups], [2, 1, 1])
        self.assertEqual([node.get("fill") for node in groups[0]], ["#ff0000", "#00ff00"])
        self.assertEqual(root.findall("s:rect", NS)[-1].get("fill"), "#aaaaaa")

    def test_drawing_after_present_does_not_mutate_retained_clip_groups(self):
        self.host.begin("#111111")
        self.host.clip(Rect(0, 0, 40, 40))
        self.host.rect(Rect(0, 0, 10, 10), "#ff0000")
        self.host.present()
        first = json.dumps(self.host._frames[1])
        self.host.rect(Rect(10, 0, 10, 10), "#00ff00")
        self.host.present()
        self.assertEqual(self.host._revision, 2)
        self.assertEqual(json.dumps(self.host._frames[1]), first)

    def test_surface_clip_groups_are_independent_and_restore_the_parent_group(self):
        self.host.begin("#111111")
        self.host.clip(Rect(0, 0, 100, 100))
        self.host.rect(Rect(0, 0, 10, 10), "#ff0000")
        surface = self.host.surface_create(Rect(10, 10, 40, 40))
        self.host.surface_begin(surface)
        self.host.clip(Rect(10, 10, 20, 20))
        self.host.rect(Rect(10, 10, 10, 10), "#00ff00")
        self.host.rect(Rect(20, 10, 10, 10), "#0000ff")
        self.host.surface_end()
        self.host.surface_blit(surface)
        self.host.rect(Rect(30, 0, 10, 10), "#ffffff")
        self.host.present()
        root = ET.fromstring(self.host.export_svg())
        clips = root.findall(".//s:clipPath", NS)
        self.assertEqual(len(clips), 2)
        self.assertEqual(len({clip.get("id") for clip in clips}), 2)
        parent = root.find("s:g/s:g", NS)
        self.assertEqual([node.tag.rsplit("}", 1)[-1] for node in parent], ["rect", "g", "rect"])
        self.assertEqual(len(parent.findall("s:g/s:g/s:g/s:rect", NS)), 2)

    def test_listener_is_loopback_and_api_requires_capability_and_origin(self):
        self.assertEqual(self.host._server.server_address[0], "127.0.0.1")
        for endpoint in ("/frame", "/export.svg", "/export.html"):
            self.assertEqual(self.request("GET", endpoint, authenticated=False)[0], 403)
        self.assertEqual(
            self.request("POST", "/events", "", authenticated=False)[0], 403
        )
        self.assertEqual(
            self.request(
                "POST", "/events", "", headers={"Origin": "https://evil.invalid"}
            )[0],
            403,
        )
        self.assertEqual(
            self.request("GET", "/frame", headers={"Host": "attacker.invalid"})[0], 403
        )
        self.assertEqual(self.request("GET", "/../../pyproject.toml")[0], 404)
        with self.assertRaises(ValueError):
            LiveSVGHost(host="0.0.0.0")

    def test_shell_is_packaged_and_has_browser_services_and_no_application_code(self):
        from importlib.resources import files

        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'<svg id="frame"', body)
        self.assertIn(b"Save HTML", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        assets = {}
        for name in ("live.js", "svg.js", "input.js", "services.js"):
            with self.subTest(asset=name):
                status, javascript, asset_headers = self.request(
                    "GET", "/" + name, authenticated=False
                )
                self.assertEqual(status, 200)
                self.assertEqual(
                    asset_headers["Content-Type"].split(";", 1)[0], "text/javascript"
                )
                self.assertEqual(asset_headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(
                    asset_headers["Content-Security-Policy"],
                    headers["Content-Security-Policy"],
                )
                self.assertEqual(
                    javascript, files("pysual").joinpath("web", name).read_bytes()
                )
                self.assertNotIn(b"eval(", javascript)
                assets[name] = javascript
        for feature in (
            b"reconcile",
            b"pointercancel",
            b"bindDOMInput",
            b"visualViewport",
        ):
            self.assertIn(feature, assets["svg.js"])
        for feature in (b"keepalive", b"AbortController", b"createSVGClient"):
            self.assertIn(feature, assets["live.js"])
        for feature in (b"navigator.clipboard", b"TextDecoder", b"download"):
            self.assertIn(feature, assets["services.js"])
        self.assertEqual(
            self.request("GET", "/font-mono.ttf")[1][:4], b"\x00\x01\x00\x00"
        )

    def test_event_ack_has_a_stricter_policy_without_changing_input_or_framing(self):
        events = [
            {"kind": "key_down", "key": "a"},
            {"kind": "text", "text": "\u00e9"},
            {"kind": "key_up", "key": "a"},
        ]
        status, body, headers = self.request(
            "POST", "/events", json.dumps({"events": events})
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"{}")
        self.assertEqual(headers["Content-Length"], "2")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertNotIn("Transfer-Encoding", headers)
        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(
            headers["Content-Security-Policy"],
            "default-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'none'",
        )
        for name, value in (
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("Cross-Origin-Resource-Policy", "same-origin"),
        ):
            self.assertEqual(headers[name], value)
        self.assertEqual(
            self.host.poll(),
            [Input("key_down", key="a"), Input("text", text="\u00e9"),
             Input("key_up", key="a")],
        )

    def test_event_ack_does_not_change_document_asset_or_export_policy(self):
        self.frame()
        full_policy = self.request("GET", "/")[2]["Content-Security-Policy"]
        self.assertIn("script-src 'self'", full_policy)
        self.assertIn("style-src 'self' 'unsafe-inline'", full_policy)
        ack_policy = self.request("POST", "/events", "{}")[2]["Content-Security-Policy"]
        self.assertNotEqual(ack_policy, full_policy)
        endpoints = ("/", "/live.js", "/font-sans.ttf", "/export.svg", "/export.html")
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                status, body, headers = self.request("GET", endpoint)
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertEqual(headers["Content-Security-Policy"], full_policy)

    def test_rejected_event_requests_keep_full_policy_and_security_checks(self):
        full_policy = self.request("GET", "/")[2]["Content-Security-Policy"]
        invalid_batch = '{"events":[{"kind":"text","text":"a"},{"kind":"shell"}]}'
        oversized = {"headers": {"Content-Length": str(_MAX_REQUEST + 1)}}
        cases = (
            ("/events", "{}", {"authenticated": False}, 403),
            ("/events", "{}", {"headers": {"Host": "attacker.invalid"}}, 403),
            ("/events", "{}", {"headers": {"Origin": "https://evil.invalid"}}, 403),
            ("/events", "{", {}, 400),
            ("/events", invalid_batch, {}, 400),
            ("/events", "{}", {"headers": {"Content-Type": "text/plain"}}, 415),
            ("/events", "{}", oversized, 413),
            ("/missing", "{}", {}, 404),
        )
        for endpoint, payload, options, expected in cases:
            with self.subTest(endpoint=endpoint, options=options, expected=expected):
                status, body, headers = self.request(
                    "POST", endpoint, payload, **options
                )
                self.assertEqual(status, expected)
                self.assertIn("error", json.loads(body))
                self.assertEqual(headers["Content-Security-Policy"], full_policy)
                self.assertEqual(headers["Content-Length"], str(len(body)))
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["Cache-Control"], "no-store")
                self.assertEqual(headers["Referrer-Policy"], "no-referrer")
                self.assertEqual(headers["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertEqual(self.host.poll(), [])

    def test_browser_service_cancellation_and_save_acknowledgment(self):
        from importlib.resources import files

        node = shutil.which("node")
        self.assertIsNotNone(node, "Install Node.js 20+ for browser host checks")
        result = subprocess.run(
            [node, "--test", str(Path(__file__).parent / "web/_web_native.test.mjs")],
            env={
                **os.environ,
                "PYSUAL_DOCUMENT_HOST": str(files("pysual").joinpath("web/live.js")),
            },
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_incremental_frames_only_include_changed_primitives(self):
        self.frame()
        status, body, _ = self.request("GET", "/frame?revision=-1")
        first = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(first["reset"])
        self.assertEqual(len(first["updates"]), 3)
        self.frame("#ff0000")
        next_frame = json.loads(
            self.request("GET", f"/frame?revision={first['revision']}")[1]
        )
        self.assertFalse(next_frame["reset"])
        self.assertEqual([entry[0] for entry in next_frame["updates"]], [1])
        revision = self.host._revision
        self.frame("#ff0000")
        self.assertEqual(self.host._revision, revision)
        self.host.begin("#111111")
        self.host.present()
        removed = json.loads(self.request("GET", f"/frame?revision={revision}")[1])
        self.assertEqual(removed["length"], 1)
        self.assertEqual(removed["updates"], [])

    def test_old_clients_receive_complete_frame_and_history_is_bounded(self):
        for index in range(8):
            self.frame(f"#{index:06x}")
        self.assertEqual(len(self.host._frames), 4)
        packet = json.loads(self.request("GET", "/frame?revision=1")[1])
        self.assertTrue(packet["reset"])
        self.assertEqual(len(packet["updates"]), 3)

    def test_viewport_pointer_keyboard_paste_and_composition(self):
        events = [
            {
                "kind": "viewport",
                "viewport": {
                    "width": 900,
                    "height": 600,
                    "scale": 2,
                    "safe_area": [1, 2, 3, 4],
                    "keyboard_occlusion": 20,
                },
            },
            {
                "kind": "pointer_down",
                "x": 12.5,
                "y": 19,
                "pointer_id": 9,
                "pointer_kind": "touch",
                "button": 1,
            },
            {"kind": "wheel", "delta": 0.25, "shift": True},
            {"kind": "key_down", "key": "ArrowLeft", "ctrl": True},
            {"kind": "text", "text": "paste \u2603\ncontent", "paste": True},
            {"kind": "composition", "text": "compose"},
        ]
        self.assertEqual(
            self.request("POST", "/events", json.dumps({"events": events}))[0], 200
        )
        inputs = self.host.poll()
        self.assertEqual(inputs[0].viewport, Viewport(900, 600, 2, (1, 2, 3, 4), 20))
        self.assertEqual(self.host.size, (900, 600))
        self.assertEqual(self.host.render_scale, 2)
        self.assertEqual(inputs[1].pointer_kind, "touch")
        self.assertEqual(inputs[2].delta, 0.25)
        self.assertEqual(inputs[4].text, "paste \u2603\ncontent")
        self.assertTrue(inputs[4].paste)
        self.assertEqual(self.host.poll(), [])

    def test_malformed_and_oversized_requests_are_rejected_atomically(self):
        for payload in (
            {"events": [{"kind": "shell"}]},
            {"events": [{"kind": "pointer_move", "x": float("nan")}]},
            {
                "events": [
                    {
                        "kind": "viewport",
                        "viewport": {"width": 1, "height": 1, "scale": 0},
                    }
                ]
            },
            {"events": [{"kind": "key_down", "ctrl": "yes"}]},
            {"events": [{"kind": "text", "text": "paste", "paste": "yes"}]},
            {"events": [{"kind": "blur"}] * 257},
            {"events": [{"kind": "blur"}], "replies": [{"id": "not-an-id"}]},
        ):
            self.assertEqual(
                self.request("POST", "/events", json.dumps(payload))[0], 400
            )
        self.assertEqual(
            self.request(
                "POST",
                "/events",
                # Reject the declared length before reading any body. Sending
                # unread bytes races the deliberate connection close on Windows.
                "",
                headers={"Content-Length": str(_MAX_REQUEST + 1)},
            )[0],
            413,
        )
        self.assertEqual(
            self.request("POST", "/events", "", headers={"Content-Type": "text/plain"})[
                0
            ],
            415,
        )
        self.assertEqual(self.host.poll(), [])

    def test_queue_coalesces_pointer_moves_and_recovers_from_overflow(self):
        self.host._receive(
            {"events": [{"kind": "pointer_move", "x": index} for index in range(200)]}
        )
        self.assertEqual(self.host.poll(), [Input("pointer_move", x=199)])
        for _ in range(10):
            self.host._receive({"events": [{"kind": "key_down", "key": "a"}] * 256})
        result = self.host.poll()
        self.assertEqual(len(result), 2560)
        self.assertTrue(all(event.kind == "key_down" for event in result))

    def test_motion_overflow_preserves_close_and_lifecycle_events(self):
        protected = [
            Input("close"),
            Input("blur"),
            Input("suspend"),
            Input("viewport", viewport=Viewport(400, 300)),
        ]
        self.host._events.extend(protected)
        for _ in range(20):
            self.host._receive(
                {
                    "events": [
                        {"kind": kind}
                        for kind in ["pointer_move", "pointer_leave"] * 128
                    ]
                }
            )
        result = self.host.poll()
        self.assertEqual(result[:4], protected)

    def test_http_accepts_a_legal_text_result_with_json_expansion(self):
        text = "\x00" * MAX_TEXT_BYTES
        body = json.dumps(
            {"replies": [{"id": 42, "value": {"name": "quoted.txt", "text": text}}]}
        )
        self.assertGreater(len(body), MAX_TEXT_BYTES + 65536)
        self.assertLessEqual(len(body), _MAX_REQUEST)
        self.assertEqual(self.request("POST", "/events", body)[0], 200)

    def test_client_disconnect_during_headers_or_body_closes_connection(self):
        from pysual.backends._web_native import _Handler
        from unittest.mock import Mock

        for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            for phase in ("headers", "body"):
                with self.subTest(error=error, phase=phase):
                    handler = Mock(close_connection=False)
                    write = handler.end_headers if phase == "headers" else handler.wfile.write
                    write.side_effect = error("closed tab")
                    _Handler._send(handler, 200, "{}")
                    self.assertTrue(handler.close_connection)
                    if phase == "headers":
                        handler.wfile.write.assert_not_called()

    def test_disconnected_keep_alive_read_closes_connection(self):
        from pysual.backends._web_native import _Handler
        from unittest.mock import Mock

        for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            with self.subTest(error=error):
                handler = object.__new__(_Handler)
                handler.rfile = Mock()
                handler.rfile.readline.side_effect = error("closed tab")
                handler.handle()
                self.assertTrue(handler.close_connection)
                handler.rfile.readline.assert_called_once()

    def test_disconnect_handling_does_not_hide_unexpected_errors(self):
        from pysual.backends._web_native import _Handler
        from unittest.mock import Mock

        handler = object.__new__(_Handler)
        handler.rfile = Mock()
        handler.rfile.readline.side_effect = RuntimeError("unexpected handler failure")
        with self.assertRaisesRegex(RuntimeError, "unexpected handler failure"):
            handler.handle()

    def test_live_gradient_uses_the_device_aligned_gradient_bands(self):
        from pysual.backends._gradient import gradient_colors

        self.host._viewport = Viewport(320, 240, scale=1.5)
        self.host.begin("#ffffff")
        self.host.gradient_rect(
            Rect(10.25, 20, 101.5, 30), "#10203040", "#a0b0c0d0", "horizontal", 8
        )
        self.host.present()
        root = ET.fromstring(self.host.export_svg())
        gradient = root.find(".//s:linearGradient[@href]", NS)
        self.assertIsNotNone(gradient)
        reference = gradient.attrib["href"]
        self.assertTrue(reference.startswith("#"))
        banks = root.findall(f".//s:linearGradient[@id='{reference[1:]}']", NS)
        self.assertEqual(len(banks), 1)
        stops = banks[0].findall("s:stop", NS)
        self.assertEqual(len(stops), 192)
        self.assertEqual(gradient.attrib["gradientUnits"], "userSpaceOnUse")
        self.assertEqual(float(gradient.attrib["x1"]), 10)
        self.assertEqual(float(gradient.attrib["x2"]), 112)
        for index, color in enumerate(gradient_colors("#10203040", "#a0b0c0d0", 96)):
            for side in range(2):
                stop = stops[index * 2 + side]
                self.assertAlmostEqual(
                    float(stop.attrib["offset"]),
                    (153 * (index + side) // 96) / 153,
                    places=3,
                )
                self.assertEqual(
                    stop.attrib["stop-color"], "#" + "".join(f"{v:02x}" for v in color)
                )

    def test_svg_and_html_exports_embed_fonts_images_license_and_escape_text(self):
        self.frame()
        self.host.clip(Rect(2, 3, 20, 10))
        self.host.image(
            "data:image/png;base64," + base64.b64encode(PNG).decode(), Rect(1, 2, 3, 4)
        )
        self.host.present()
        with tempfile.TemporaryDirectory() as folder:
            svg = self.host.export_svg(Path(folder) / "app.svg")
            html = self.host.export_html(Path(folder) / "app.html")
            self.assertEqual(
                (Path(folder) / "app.svg").read_text(encoding="utf-8"), svg
            )
            self.assertEqual(
                (Path(folder) / "app.html").read_text(encoding="utf-8"), html
            )
        root = ET.fromstring(svg)
        self.assertEqual(root.find("s:title", NS).text, "Document <test>")
        self.assertEqual(root.find("s:text", NS).text, "AV & <text>")
        self.assertEqual(svg.count("data:font/ttf;base64,"), 2)
        self.assertIn("Bitstream", svg)
        self.assertIn("data:image/png;base64,", svg)
        self.assertIn("Live interaction requires the Python application", html)
        self.assertNotIn("<script", html)
        self.assertNotIn(self.host._token, html)
        self.assertNotIn(self.host._origin, html)
        self.assertEqual(self.request("GET", "/export.svg")[1].decode(), svg)

    def test_local_images_are_embedded_without_exposing_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "image.png"
            path.write_bytes(PNG)
            self.host.begin("#000000")
            self.host.image(path, Rect(0, 0, 1, 1))
            self.host.present()
            svg = self.host.export_svg()
            self.assertNotIn(str(path), svg)
            self.assertIn("data:image/png;base64,", svg)
            self.host.image(Path(folder) / "missing.png", Rect(0, 0, 1, 1))
            self.assertEqual(self.host.poll()[0].kind, "resource_error")

    def test_browser_decode_errors_are_bounded_source_specific_and_session_scoped(self):
        # A complete IHDR is valid portable input but cannot be decoded alone.
        source = "data:image/png;base64," + base64.b64encode(PNG[:33]).decode()
        self.host.image(source, Rect(0, 0, 20, 20))
        identifier = self.host._nodes[-1]["attrs"]["data-pysual-image"]
        surface = self.host.surface_create(Rect(0, 0, 20, 20))
        self.host.surface_begin(surface)
        self.host.image(source, Rect(0, 0, 20, 20))
        self.host.surface_end()
        self.host.surface_blit(surface)
        self.host.present()
        images = ET.fromstring(self.host.export_svg()).findall(".//s:image", NS)
        self.assertEqual([node.get("data-pysual-image") for node in images], [identifier] * 2)
        envelope = {"events": [{"kind": "image_error", "text": identifier}]}
        self.assertEqual(self.request("POST", "/events", json.dumps(envelope))[0], 200)
        self.host._receive(envelope)
        errors = self.host.poll()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].kind, "resource_error")
        self.assertIn(
            f"Browser could not decode embedded image {identifier}", errors[0].text
        )
        self.assertNotIn(source, errors[0].text)
        self.host._receive({"events": [{"kind": "image_error", "text": "9999"}]})
        self.assertEqual(self.host.poll(), [])
        token = self.host._token
        self.host.close()
        self.host.open("Reopened", 320, 240, True, None)
        self.assertFalse(self.host._receive(envelope, token=token))
        self.host.image(source, Rect(0, 0, 20, 20))
        self.host._receive(envelope)
        self.assertEqual(len(self.host.poll()), 1)
        for text in ("-1", "1" * 21, "１", "not an identifier"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.host._receive({"events": [{"kind": "image_error", "text": text}]})

    def test_png_paths_and_embedded_sources_share_portable_image_limits(self):
        oversized = bytearray(PNG)
        oversized[16:24] = (4096).to_bytes(4, "big") * 2
        oversized[29:33] = zlib.crc32(oversized[12:29]).to_bytes(4, "big")
        with tempfile.TemporaryDirectory() as folder:
            for index, data in enumerate(
                (bytes(oversized), PNG + b"x" * MAX_IMAGE_BYTES)
            ):
                path = Path(folder) / f"oversized-{index}.png"
                path.write_bytes(data)
                for source in (
                    str(path),
                    "data:image/png;base64," + base64.b64encode(data).decode(),
                ):
                    with self.subTest(index=index, embedded=source.startswith("data:")):
                        self.host.image(source, Rect(0, 0, 20, 20))
                        self.assertFalse(self.host._nodes)
                        self.assertFalse(self.host._images)
            errors = self.host.poll()
            self.assertTrue(any("32 MiB" in event.text for event in errors))
            self.assertTrue(any("8 MiB" in event.text for event in errors))

    def test_other_browser_formats_report_decode_failures_with_bounded_labels(self):
        for mime in ("jpeg", "gif", "webp", "svg+xml"):
            source = f"data:image/{mime};base64," + base64.b64encode(b"corrupt").decode()
            self.host.image(source, Rect(0, 0, 20, 20))
            identifier = self.host._nodes[-1]["attrs"]["data-pysual-image"]
            self.host._receive({"events": [{"kind": "image_error", "text": identifier}]})
        self.assertEqual(len(self.host.poll()), 4)
        for index in range(300):
            source = "data:image/gif;base64," + base64.b64encode(str(index).encode()).decode()
            self.host.image(source, Rect(0, 0, 20, 20))
            identifier = self.host._nodes[-1]["attrs"]["data-pysual-image"]
            self.host._receive({"events": [{"kind": "image_error", "text": identifier}]})
        self.assertLessEqual(len(self.host._image_labels), 256)
        self.assertLessEqual(len(self.host._image_errors), 32)
        self.assertLessEqual(len(self.host.poll()), 32)

    def test_surface_build_reuse_release_and_clip_reset(self):
        self.host.begin("#111111")
        surface = self.host.surface_create(Rect(0, 0, 50, 60))
        self.host.clip(Rect(0, 0, 10, 10))
        self.host.surface_begin(surface)
        self.host.rect(Rect(5, 5, 20, 20), "#ff0000")
        self.host.surface_end()
        self.assertEqual(self.host._clip, Rect(0, 0, 10, 10))
        self.host.clip(None)
        self.host.surface_blit(surface)
        self.host.present()
        first = ET.fromstring(self.host.export_svg())
        self.assertIsNotNone(first.find(".//s:rect[@fill='#ff0000']", NS))
        self.host.surface_begin(surface)
        self.host.text("fresh", 0, 0, "#fff", 14)
        self.host.surface_end()
        self.host.begin("#111111")
        self.host.surface_blit(surface)
        self.host.present()
        second = ET.fromstring(self.host.export_svg())
        self.assertIsNone(second.find(".//s:rect[@fill='#ff0000']", NS))
        self.host.surface_release(surface)
        with self.assertRaises(ValueError):
            self.host.surface_blit(surface)

    def test_gradient_border_preserves_transparent_interior(self):
        self.host.begin("#111111")
        self.host.gradient_rect(
            Rect(0, 0, 30, 20), "#ff0000", "#0000ff", "horizontal", 4, 2
        )
        self.host.present()
        root = ET.fromstring(self.host.export_svg())
        border = root.find("s:g/s:rect", NS)
        self.assertEqual(border.get("fill"), "none")
        self.assertEqual(border.get("stroke-width"), "2")
        self.assertEqual(border.get("x"), "1.0")

    def test_translucent_border_keeps_full_fill_and_one_frame_primitive(self):
        for radius in (0, 8):
            with self.subTest(radius=radius):
                self.host.begin("#ffffff")
                self.host.rect(
                    Rect(10, 10, 80, 40), "#ff000080", radius, "#0000ff80", 4
                )
                self.host.present()
                root = ET.fromstring(self.host.export_svg())
                fill, border = root.findall("s:g/s:rect", NS)
                self.assertEqual(
                    [fill.get(name) for name in ("x", "y", "width", "height")],
                    ["10", "10", "80", "40"],
                )
                self.assertEqual(fill.get("fill"), "#ff000080")
                self.assertEqual(fill.get("rx"), str(radius))
                self.assertEqual(border.get("fill"), "none")
                self.assertEqual(border.get("x"), "12.0")
                self.assertEqual(border.get("width"), "76")
                self.assertEqual(border.get("stroke-width"), "4")
                self.assertEqual(float(border.get("rx")), max(0, radius - 2))
                self.assertEqual(self.host._frame_packet(-1)["length"], 2)

    def test_disconnect_grace_allows_reload_and_close_stops_listener(self):
        self.host._receive({"disconnect": True})
        self.assertIsNotNone(self.host._disconnect_at)
        self.host._frame_packet(-1)
        self.assertIsNone(self.host._disconnect_at)
        self.host._disconnect_at = time.monotonic() - 1
        self.assertEqual(self.host.poll(), [Input("close")])
        server, thread = self.host._server, self.host._thread
        self.host.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(server.socket.fileno(), -1)
        self.host.close()

    def test_existing_frame_requests_preserve_disconnect_and_still_wait_when_idle(self):
        self.frame()
        revision = self.host._frame_packet(-1)["revision"]
        self.host._receive({"disconnect": True})
        deadline = self.host._disconnect_at
        with patch.object(self.host._condition, "wait_for", return_value=False) as wait:
            self.host._frame_packet(revision)
        wait.assert_called_once()
        self.assertEqual(wait.call_args.kwargs, {"timeout": 15})
        self.assertFalse(wait.call_args.args[0]())
        self.assertEqual(self.host._disconnect_at, deadline)
        with patch("pysual.backends._web_native.time.monotonic", return_value=deadline + .1):
            self.assertEqual(self.host.poll(), [Input("close")])

    def test_input_only_stream_wakes_wait_until_metadata_changes(self):
        self.frame()
        endpoint = urlsplit(self.host.url)
        connection = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/stream?token=" + endpoint.fragment)
        response = connection.getresponse()

        def read_event():
            data = b""
            while b"\n\n" not in data:
                piece = response.read(1)
                self.assertTrue(piece, "Event stream ended before its next frame")
                data += piece
            return data

        read_event()
        received, packets = threading.Event(), []

        def read_next():
            packets.append(read_event())
            received.set()

        worker = threading.Thread(target=read_next, daemon=True)
        worker.start()
        for x in range(3):
            self.host._receive({"events": [{"kind": "pointer_move", "x": x, "y": 5}]})
            self.assertFalse(received.wait(.03), "Input alone published an unchanged packet")
        self.host.set_title("Actual metadata change")
        self.assertTrue(received.wait(1))
        worker.join(1)
        self.assertIn(b"Actual metadata change", packets[0])
        self.assertIn(b'"updates":[]', packets[0])

    def test_spurious_wakes_do_not_extend_the_frame_heartbeat(self):
        self.frame()
        revision = self.host._frame_packet(-1)["revision"]
        # Condition.wait_for uses an absolute deadline across notifications.
        with (
            patch("threading._time", side_effect=[100, 105, 115]),
            patch.object(self.host._condition, "wait", return_value=True) as wait,
        ):
            packet = self.host._frame_packet(revision)
        self.assertEqual([call.args for call in wait.call_args_list], [(15,), (10,)])
        self.assertEqual(packet["updates"], [])
        self.assertEqual(packet["revision"], revision)

    def test_failed_browser_launch_keeps_the_printed_listener_available(self):
        host = LiveSVGHost(open_browser=True)
        self.addCleanup(host.close)
        output = StringIO()
        with (
            patch("pysual.backends._web_native.webbrowser.open", return_value=False),
            patch("sys.stderr", output),
        ):
            host.open("Manual browser", 320, 240, True, None)
        self.assertIn(host.url, output.getvalue())
        self.assertFalse(host._closed)
        self.assertTrue(host._thread.is_alive())
        endpoint = urlsplit(host.url)
        connection = http.client.HTTPConnection(
            endpoint.hostname, endpoint.port, timeout=3
        )
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        self.assertEqual(connection.getresponse().status, 200)

    def test_reopen_resets_frame_focus_errors_and_rejects_old_launch_tokens(self):
        self.frame()
        self.host.text_input(Rect(10, 20, 80, 24))
        self.host._image_errors.add("previous failure")
        token = self.host._token
        saved = self.host.export_svg()
        self.host.close()
        self.assertEqual(self.host.export_svg(), saved)
        self.host.open("Fresh launch", 400, 300, True, None)
        packet = self.host._frame_packet(-1)
        self.assertEqual(packet["length"], 0)
        self.assertIsNone(packet["text_input"])
        self.assertEqual(self.host._image_errors, set())
        self.assertNotIn("AV &amp;", self.host.export_svg())
        self.assertFalse(
            self.host._receive({"events": [{"kind": "close"}]}, token=token)
        )
        self.assertEqual(self.host.poll(), [])
        self.assertTrue(self.host._frame_packet(-1, token=token)["closed"])
        for exporter in (self.host.export_html, self.host.export_svg):
            with self.assertRaises(CapabilityError):
                exporter(_token=token)

    def test_authorized_old_post_body_cannot_cross_launches(self):
        from pysual.backends import _web_native as adapter

        full_policy = self.request("GET", "/")[2]["Content-Security-Policy"]
        old = urlsplit(self.host.url)
        connection = http.client.HTTPConnection(old.hostname, old.port, timeout=3)
        self.addCleanup(connection.close)
        accepted = threading.Event()
        original = adapter._Handler._allowed

        def allowed(handler, *args):
            result = original(handler, *args)
            if result and handler.command == "POST":
                accepted.set()
            return result

        body = b'{"events":[{"kind":"close"}]}'
        with patch.object(adapter._Handler, "_allowed", allowed):
            connection.putrequest("POST", "/events")
            connection.putheader("X-Pysual-Token", old.fragment)
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders()
            self.assertTrue(accepted.wait(1))
            self.host.close()
            self.host.open("New app", 320, 240, True, None)
            connection.send(body)
            response = connection.getresponse()
            self.assertEqual(response.status, 410)
            self.assertEqual(response.read(), b"{}")
            self.assertEqual(response.getheader("Content-Security-Policy"), full_policy)
        self.assertEqual(self.host.poll(), [])

    def test_reopen_session_writes_work_across_distinct_asyncio_loops(self):
        async def write_pair(value):
            await asyncio.gather(
                self.host.write_session("reopen-first", value),
                self.host.write_session("reopen-second", value),
            )
            return await self.host.read_session("reopen-second")

        with tempfile.TemporaryDirectory() as folder:
            with patch.object(
                LiveSVGHost, "_session_directory", return_value=Path(folder)
            ):
                first_lock = self.host._session_lock
                self.assertEqual(
                    asyncio.run(write_pair("first launch")), "first launch"
                )
                self.host.close()
                self.host.open("Second launch", 320, 240, True, None)
                self.assertIsNot(self.host._session_lock, first_lock)
                self.assertEqual(
                    asyncio.run(write_pair("second launch")), "second launch"
                )

    def test_authorized_old_frame_request_cannot_read_a_new_launch(self):
        self.frame()
        arrived, release = threading.Event(), threading.Event()
        original = self.host._frame_packet

        def delayed(revision, *, token=None):
            arrived.set()
            self.assertTrue(release.wait(3))
            return original(revision, token=token)

        old = urlsplit(self.host.url)
        connection = http.client.HTTPConnection(old.hostname, old.port, timeout=3)
        self.addCleanup(connection.close)
        with patch.object(self.host, "_frame_packet", delayed):
            connection.request(
                "GET", "/frame?revision=-1", headers={"X-Pysual-Token": old.fragment}
            )
            self.assertTrue(arrived.wait(1))
            self.host.close()
            self.host.open("Private new launch", 320, 240, True, None)
            self.frame("#123456")
            release.set()
            response = connection.getresponse()
            packet = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertTrue(packet["closed"])
        self.assertEqual(packet["updates"], [])
        self.assertNotEqual(packet["title"], "Private new launch")

    def test_clean_close_is_delivered_between_browser_long_polls(self):
        from pysual.backends import _web_native as adapter

        self.frame()
        first = json.loads(self.request("GET", "/frame?revision=-1")[1])
        finish_send = threading.Event()
        original = adapter._Handler._send

        def send_then_wait(handler, status, body=b"", *args, **kwargs):
            original(handler, status, body, *args, **kwargs)
            if isinstance(body, str) and '"closed":true' in body:
                # Receiving bytes does not imply the server write has returned.
                # Force that ordering so this check never relies on scheduling.
                finish_send.wait(2)

        closing = threading.Thread(target=self.host.close)
        with patch.object(adapter._Handler, "_send", send_then_wait):
            closing.start()
            try:
                packet = json.loads(
                    self.request("GET", f"/frame?revision={first['revision']}")[1]
                )
                self.assertTrue(packet["closed"])
                finish_send.set()
                self.assertTrue(self.host._closed_delivered.wait(2))
            finally:
                finish_send.set()
                closing.join(timeout=2)
        self.assertFalse(closing.is_alive())


class WebLiveServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.host = LiveSVGHost(open_browser=False)
        self.host.open("Service test", 320, 240, True, None)
        self.addCleanup(self.host.close)

    async def respond(self, task, value=None, error=None):
        await asyncio.sleep(0)
        self.assertEqual(len(self.host._commands), 1)
        operation = next(iter(self.host._commands))
        reply = {"id": operation, "value": value}
        if error:
            reply["error"] = error
        # Requests run on HTTP workers, completion returns to the owning loop.
        await asyncio.to_thread(self.host._receive, {"replies": [reply]})
        return await task

    async def test_browser_clipboard_files_urls_and_denial(self):
        value = await self.respond(
            asyncio.create_task(self.host.clipboard_read()), "\u2603 text"
        )
        self.assertEqual(value, "\u2603 text")
        await self.respond(asyncio.create_task(self.host.clipboard_write("copy")), True)
        with self.assertRaises(CapabilityError):
            await self.respond(
                asyncio.create_task(self.host.clipboard_write("cancelled")), None
            )
        imported = await self.respond(
            asyncio.create_task(self.host.open_text_file()),
            {"name": "a.txt", "text": "hello", "location": "/ignored"},
        )
        self.assertEqual(
            (imported.name, imported.text, imported.location), ("a.txt", "hello", None)
        )
        saved = await self.respond(
            asyncio.create_task(self.host.save_text_file("saved", "../a.txt", None)),
            True,
        )
        self.assertEqual(saved.name, "a.txt")
        renamed = await self.respond(
            asyncio.create_task(self.host.save_text_file("saved", "a.txt")),
            "chosen.txt",
        )
        self.assertEqual(
            (renamed.name, renamed.text, renamed.location), ("chosen.txt", "saved", None)
        )
        self.assertIsNone(
            await self.respond(
                asyncio.create_task(self.host.save_text_file("cancelled", "a.txt")),
                None,
            )
        )
        await self.respond(
            asyncio.create_task(self.host.open_url("https://example.org")), True
        )
        with self.assertRaises(CapabilityError):
            await self.respond(
                asyncio.create_task(self.host.clipboard_read()),
                error="Permission denied",
            )
        with self.assertRaises(ValueError):
            await self.host.open_url("javascript:alert(1)")
        self.assertEqual(self.host._pending, {})
        self.assertEqual(self.host._commands, {})

    async def test_pending_services_cancel_and_close_without_late_results(self):
        task = asyncio.create_task(self.host.open_text_file())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.host._pending, {})
        self.assertEqual(self.host._commands, {})
        task = asyncio.create_task(self.host.clipboard_read())
        await asyncio.sleep(0)
        self.host.close()
        with self.assertRaises(CapabilityError):
            await task

    async def test_rpc_validates_file_results_and_payload_limits(self):
        with self.assertRaises(CapabilityError):
            await self.respond(
                asyncio.create_task(self.host.open_text_file()),
                {"name": "bad", "text": 3},
            )
        with self.assertRaises(ValueError):
            await self.host.save_text_file("x" * (MAX_TEXT_BYTES + 1), "a.txt")
        with self.assertRaises(CapabilityError):
            await self.host.clipboard_write("x" * (MAX_TEXT_BYTES + 1))
        self.assertIsNone(
            await self.respond(asyncio.create_task(self.host.open_text_file()), None)
        )

    async def test_sessions_survive_new_listener_ports(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(
                LiveSVGHost, "_session_directory", return_value=Path(folder)
            ):
                await self.host.write_session("document-test", "\ufeffretained \u2603")
                another = LiveSVGHost(open_browser=False)
                self.assertEqual(
                    await another.read_session("document-test"), "\ufeffretained \u2603"
                )
                await another.write_session("document-test", None)
                self.assertIsNone(await self.host.read_session("document-test"))


if __name__ == "__main__":
    unittest.main()
