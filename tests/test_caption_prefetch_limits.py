"""Caption batches stay inside the actual native JSON transport's limits."""

from collections import Counter
import json

from _ui_testcase import UIOwnerTestCase, ui_timeout

from pysual import Container, Label
from pysual.backends._native_client import MAX_PAYLOAD, NativeClient
from pysual.backends.native import NativeHost
from pysual.layout import arrange
from pysual.painting import RetainedPaintTree, _prefetch_caption_metrics, paint_tree


class SerializedMetricTransport(NativeClient):
    """Keep request serialization and size checks; replace only the process IO."""

    def __init__(self):
        super().__init__()
        self._process = object()  # The synthetic transport needs no child process.
        self.requests = []
        self.measured = Counter()

    def _request(self, opcode, payload=b"", *, timeout):
        request = json.loads(payload)
        op = request["op"]
        texts = request.get("texts", ())
        self.requests.append((op, len(payload), len(texts)))
        if op == "measure_many":
            self.measured.update(texts)
            return [[len(text) * 8, 16] for text in texts]
        if op == "measure":
            text = request["text"]
            self.measured[text] += 1
            return [len(text) * 8, 16]
        return None

    def close(self):
        self._process = None
        super().close()


def astral_captions():
    return [chr(0x10000 + index) + "\U0001f600" * 8191 for index in range(512)]


class CaptionPrefetchLimitsTests(UIOwnerTestCase):
    def fixture(self, captions):
        host = NativeHost()
        transport = SerializedMetricTransport()
        host._client = transport
        panel = Container(layout="grid", columns=32, width=960, height=640,
                          spacing=0, padding=0)
        labels = [Label(parent=panel, text=text, width=30, height=40)
                  for text in captions]
        self.addCleanup(host.close)
        self.addCleanup(panel.destroy)
        arrange(panel, host)
        self.assertEqual(transport.requests, [])
        return host, transport, panel, labels

    def assert_bounded_prefetch(self, captions):
        host, transport, _, labels = self.fixture(captions)
        _prefetch_caption_metrics(host, labels)
        self.assertEqual(transport.measured, Counter(captions))
        self.assertTrue(all(op == "measure_many" for op, _, _ in transport.requests))
        self.assertTrue(all(size <= 1024 * 1024 and count <= 512
                            for _, size, count in transport.requests))
        self.assertGreater(len(transport.requests), 1)

    def test_500_short_captions_still_share_one_request(self):
        captions = [f"Caption {index}" for index in range(500)]
        host, transport, _, labels = self.fixture(captions)
        _prefetch_caption_metrics(host, labels)
        self.assertEqual(transport.measured, Counter(captions))
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.requests[0][::2], ("measure_many", 500))

    def test_caption_count_remains_bounded(self):
        captions = [f"Caption {index}" for index in range(1025)]
        host, transport, _, labels = self.fixture(captions)
        _prefetch_caption_metrics(host, labels)
        self.assertEqual(transport.measured, Counter(captions))
        self.assertEqual([count for _, _, count in transport.requests], [512, 512, 1])

    def test_utf8_captions_are_bounded_and_measured_once(self):
        self.assert_bounded_prefetch(astral_captions())

    def test_json_escaped_captions_are_bounded_and_measured_once(self):
        suffix = ("\x01\x02\x03\"\\" * 1638)[:8188]
        captions = [f"{index:04d}" + suffix for index in range(512)]
        self.assertTrue(all(len(text) == 8192 for text in captions))
        self.assert_bounded_prefetch(captions)

    @ui_timeout(30)
    def test_large_captions_complete_a_retained_native_scene(self):
        host, transport, panel, _ = self.fixture(astral_captions())
        retained = RetainedPaintTree(host)
        self.addCleanup(retained.clear)
        paint_tree(panel, host, retained=retained)
        self.assertEqual(sum(op == "patch" for op, _, _ in transport.requests), 1)
        self.assertTrue(all(size <= MAX_PAYLOAD for _, size, _ in transport.requests))
        self.assertTrue(all(size <= 1024 * 1024 and count <= 512
                            for op, size, count in transport.requests
                            if op == "measure_many"))
