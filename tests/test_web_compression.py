"""Negotiated scene compression over the real loopback HTTP transport."""

import gzip
import http.client
import io
import json
from types import SimpleNamespace
import unittest
from urllib.parse import urlsplit
import zlib

from pysual import Rect
from pysual.backends._web_native import LiveSVGHost, _Handler


class _Events:
    def __init__(self, response):
        self.response = response
        self.decoder = (
            zlib.decompressobj(wbits=31)
            if response.getheader("Content-Encoding") == "gzip"
            else None
        )
        self.pending = b""

    def feed(self, data):
        self.pending += self.decoder.decompress(data) if self.decoder else data

    def read(self):
        while b"\n\n" not in self.pending:
            # read1 returns currently available chunk data. The socket timeout
            # fails the test if an event waits for another event or stream EOF.
            data = self.response.read1(65536)
            if not data:
                raise AssertionError("Stream ended before a complete SSE event")
            self.feed(data)
        event, self.pending = self.pending.split(b"\n\n", 1)
        if not event.startswith(b"data: "):
            raise AssertionError("Unexpected SSE event")
        return json.loads(event[6:])


class WebCompressionTests(unittest.TestCase):
    def setUp(self):
        self.host = LiveSVGHost(open_browser=False)
        self.host.open("Compressed \u00e9 scene", 320, 240, True, None)
        self.addCleanup(self.host.close)
        self.endpoint = urlsplit(self.host.url)
        self.frame()

    def frame(self, color="#112233"):
        self.host.begin("#111111")
        self.host.rect(Rect(10, 10, 100, 30), color)
        self.host.text("Repeated scene text " * 40, 12, 12, "#ffffff", 14)
        self.host.present()

    def connection(self):
        connection = http.client.HTTPConnection(
            self.endpoint.hostname, self.endpoint.port, timeout=3
        )
        self.addCleanup(connection.close)
        return connection

    def request(self, path, encoding=None, *, method="GET", body=None, auth=True):
        connection = self.connection()
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["X-Pysual-Token"] = self.endpoint.fragment
        if encoding is not None:
            headers["Accept-Encoding"] = encoding
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response, data

    def stream(self, encoding):
        connection = self.connection()
        headers = {"Accept-Encoding": encoding}
        connection.request(
            "GET", "/stream?token=" + self.endpoint.fragment, headers=headers
        )
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Vary"), "Accept-Encoding")
        self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
        self.assertIsNone(response.getheader("Content-Length"))
        return connection, response, _Events(response)

    def test_chunk_boundaries_round_trip_through_http_decoder(self):
        # Exercise both sides of the copy bound, embedded framing-like bytes,
        # consecutive chunks and the terminal chunk with a standard HTTP reader.
        payloads = [
            (b"\x00\r\nFF\r\n\xff" * (size // 8 + 1))[:size]
            for size in (1, 65535, 65536, 65537, 1024 * 1024)
        ]
        wire = io.BytesIO()
        wire.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
        handler = SimpleNamespace(wfile=wire)
        for payload in payloads:
            _Handler._write_chunk(handler, payload)
        _Handler._write_chunk(handler, b"")
        wire.seek(0)
        response = http.client.HTTPResponse(
            SimpleNamespace(makefile=lambda *args: wire)
        )
        response.begin()
        self.assertEqual(response.read(), b"".join(payloads))
        self.assertTrue(response.isclosed())

    def test_frame_negotiates_quality_and_wildcard_without_encoding_unwilling_clients(self):
        cases = (
            (None, False),
            ("", False),
            ("identity", False),
            ("br, deflate", False),
            ("gzip", True),
            ("br, GZip ; Q=0.5", True),
            ("gzip;q=0.001", True),
            ("gzip;q=1.000", True),
            ("gzip;q=0", False),
            ("gzip;q=0.000", False),
            ("gzip;q=0, *;q=1", False),
            ("*;q=0.5", True),
            ("*;q=0", False),
            ("gzip;q=0.5, identity;q=1", False),
            ("gzip;q=0.5, identity;q=0", True),
            ("gzip, gzip;q=0", False),
            ("gzip;q=0, gzip", False),
        )
        for encoding, compressed in cases:
            with self.subTest(encoding=encoding):
                response, data = self.request("/frame?revision=-1", encoding)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Vary"), "Accept-Encoding")
                self.assertEqual(int(response.getheader("Content-Length")), len(data))
                self.assertEqual(
                    response.getheader("Content-Encoding"),
                    "gzip" if compressed else None,
                )
                packet = json.loads(gzip.decompress(data) if compressed else data)
                self.assertEqual(packet["title"], "Compressed \u00e9 scene")
                self.assertTrue(packet["reset"])
                self.assertFalse(packet["closed"])

    def test_malformed_gzip_quality_does_not_fall_through_to_wildcard(self):
        for quality in ("", "NaN", "inf", "-1", "2", ".5", "0.1234", "1.001"):
            with self.subTest(quality=quality):
                response, data = self.request(
                    "/frame?revision=-1", f"gzip;q={quality}, *;q=1"
                )
                self.assertIsNone(response.getheader("Content-Encoding"))
                self.assertTrue(json.loads(data)["reset"])
        for value in ("gzip;q=0.5;q=1", "gzip;level=1", 'gzip;q="1"'):
            with self.subTest(value=value):
                response, data = self.request("/frame?revision=-1", value)
                self.assertIsNone(response.getheader("Content-Encoding"))
                self.assertTrue(json.loads(data)["reset"])

    def test_repeated_accept_encoding_headers_preserve_gzip_exclusion(self):
        connection = self.connection()
        connection.putrequest("GET", "/frame?revision=-1", skip_accept_encoding=True)
        connection.putheader("X-Pysual-Token", self.endpoint.fragment)
        connection.putheader("Accept-Encoding", "gzip")
        connection.putheader("Accept-Encoding", "gzip;q=0, *;q=1")
        connection.endheaders()
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertTrue(json.loads(response.read())["reset"])

    def test_compression_is_limited_to_successful_scene_delivery(self):
        cases = (
            ("/", "GET", None, True, 200),
            ("/events", "POST", "{}", True, 200),
            ("/frame?revision=bad", "GET", None, True, 400),
            ("/frame?revision=-1", "GET", None, False, 403),
            ("/stream?token=wrong", "GET", None, True, 403),
        )
        for path, method, body, auth, status in cases:
            with self.subTest(path=path):
                response, data = self.request(
                    path, "gzip", method=method, body=body, auth=auth
                )
                self.assertEqual(response.status, status)
                self.assertIsNone(response.getheader("Content-Encoding"))
                self.assertIsNone(response.getheader("Vary"))
                self.assertTrue(data)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")

    def test_stream_flushes_each_event_and_finishes_gzip_before_terminal_chunk(self):
        _, response, events = self.stream("gzip")
        self.assertEqual(response.getheader("Content-Encoding"), "gzip")
        first = events.read()
        self.assertTrue(first["reset"])
        self.assertFalse(events.decoder.eof)
        self.frame("#abcdef")
        second = events.read()
        self.assertGreater(second["revision"], first["revision"])
        self.assertIn("#abcdef", json.dumps(second))
        self.assertFalse(events.decoder.eof)
        self.host.set_title("Metadata \u03a9 without repaint")
        metadata = events.read()
        self.assertEqual(metadata["revision"], second["revision"])
        self.assertEqual(metadata["title"], "Metadata \u03a9 without repaint")
        self.assertEqual(metadata["updates"], [])
        self.host.close()
        self.assertTrue(events.read()["closed"])
        events.feed(response.read())
        self.assertTrue(events.decoder.eof, "Missing gzip trailer")
        self.assertEqual(events.decoder.unused_data, b"")
        self.assertEqual(events.pending, b"")
        self.assertTrue(response.isclosed(), "Missing terminating HTTP chunk")
        self.assertTrue(self.host._closed_delivered.is_set())

    def test_stream_declines_zero_quality_and_terminates_plain_sse(self):
        _, response, events = self.stream("gzip;q=0, *;q=1")
        self.assertIsNone(response.getheader("Content-Encoding"))
        self.assertTrue(events.read()["reset"])
        self.host.close()
        self.assertTrue(events.read()["closed"])
        self.assertEqual(response.read(), b"")
        self.assertTrue(response.isclosed())

    def test_simultaneous_and_reconnected_streams_have_independent_gzip_state(self):
        first_connection, first_response, first_events = self.stream("gzip")
        first = first_events.read()
        _, second_response, second_events = self.stream("gzip")
        second = second_events.read()
        self.assertEqual(first["revision"], second["revision"])
        self.assertTrue(second["reset"])
        self.host.set_title("Both streams receive metadata")
        self.assertEqual(first_events.read()["title"], "Both streams receive metadata")
        self.assertEqual(second_events.read()["title"], "Both streams receive metadata")
        first_response.close()
        first_connection.close()
        _, _, reconnected = self.stream("gzip")
        self.assertTrue(reconnected.read()["reset"])
        self.frame("#778899")
        self.assertIn("#778899", json.dumps(second_events.read()))
        self.assertIn("#778899", json.dumps(reconnected.read()))
        self.assertFalse(second_events.decoder.eof)
        self.assertFalse(second_response.isclosed())


if __name__ == "__main__":
    unittest.main()
