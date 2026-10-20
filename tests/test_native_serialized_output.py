"""The exact drawing bytes are compared and sent through one request lifecycle."""

import json
from unittest.mock import Mock

import pytest

from pysual.backends import _native_client as protocol
from pysual.backends.native import NativeHost
from pysual.geometry import Rect


@pytest.fixture
def bridge():
    client = protocol.NativeClient()
    client._process = Mock()
    client._request = Mock(return_value=None)
    host = NativeHost()
    host._client = client
    yield host, client
    host._client = None
    host.close()


def draw(host, *texts):
    host.begin_scene("#123456")
    for index, text in enumerate(texts):
        host.begin_segment(str(index), Rect(index * 100, 0, 100, 30))
        host.text(text, index * 100, 0, "#ffffff", 16)
        host.end_segment()
    host.end_scene(order=[str(index) for index in range(len(texts))])


def wire(client):
    assert client._request.call_args.args[0] == 9
    return json.loads(client._request.call_args.args[1])


def test_patch_encodes_once_and_sends_the_same_bytes(bridge, monkeypatch):
    host, client = bridge
    encode = Mock(wraps=protocol._NATIVE_JSON.encode)
    monkeypatch.setattr(protocol._NATIVE_JSON, "encode", encode)
    draw(host, "One", "Two")
    assert encode.call_count == 1
    assert [segment["commands"][0][1] for segment in wire(client)["upsert"]] == ["One", "Two"]
    assert host._output is client._request.call_args.args[1]
    client._request.reset_mock()
    draw(host, "One", "Changed")
    assert [segment["id"] for segment in wire(client)["upsert"]] == ["0", "1"]
    client._request.reset_mock()
    draw(host, "One", "Changed")
    client._request.assert_not_called()


def test_frame_encodes_commands_once_and_preserves_transition(bridge, monkeypatch):
    host, client = bridge
    encode = Mock(wraps=protocol._NATIVE_JSON.encode)
    monkeypatch.setattr(protocol._NATIVE_JSON, "encode", encode)
    host.begin("#123456")
    host.text("One", 0, 0, "#fff", 16)
    host.request_transition(.2)
    host.present()
    assert encode.call_count == 1
    assert wire(client)["commands"] == encode.call_args.args[0]["commands"]
    assert host._output is client._request.call_args.args[1]
    assert wire(client)["transition_seconds"] == .2


@pytest.mark.parametrize("hook", ["host_instance", "host_class", "client_instance", "client_class"])
def test_overridden_request_hooks_receive_the_original_fields(bridge, monkeypatch, hook):
    host, client = bridge
    seen = []
    if hook.startswith("host"):
        original = NativeHost._request
        def request(self, op, **fields):
            seen.append((op, fields))
            return original(self, op, **fields)
        if hook == "host_instance":
            monkeypatch.setattr(host, "_request", request.__get__(host))
        else:
            monkeypatch.setattr(NativeHost, "_request", request)
    else:
        original = protocol.NativeClient.request
        def request(self, op, **fields):
            seen.append((op, fields))
            return original(self, op, **fields)
        if hook == "client_instance":
            monkeypatch.setattr(client, "request", request.__get__(client))
        else:
            monkeypatch.setattr(protocol.NativeClient, "request", request)
    draw(host, "Custom")
    assert len(seen) == 1 and seen[0][0] == "patch"
    assert seen[0][1]["upsert"][0]["commands"][0][1] == "Custom"
    assert wire(client)["upsert"] == seen[0][1]["upsert"]


def test_custom_client_keeps_ordinary_request_api():
    class Client:
        def __init__(self):
            self.request = Mock(return_value={})
        def close(self):
            pass
    client = Client()
    host = NativeHost(client_factory=lambda **kwargs: client)
    try:
        host.open("Custom", 100, 100, False, None)
        client.request.reset_mock()
        draw(host, "Custom")
        assert client.request.call_args.args == ("patch",)
        assert client.request.call_args.kwargs["upsert"][0]["commands"][0][1] == "Custom"
    finally:
        host.close()


def test_failed_prepared_patch_does_not_publish_and_retry_keeps_opening_timeout(bridge):
    host, client = bridge
    client._request.side_effect = protocol.NativeHostError("rejected")
    with pytest.raises(protocol.NativeHostError, match="rejected"):
        draw(host, "Retry")
    rejected = client._request.call_args.args[1]
    assert client._request.call_args.kwargs["timeout"] is None
    assert host._output is None and client._opening
    client._request.side_effect = None
    host.end_scene()
    assert client._request.call_args.args[1] == rejected
    assert not client._opening and host._output is not None
    host.present()
    assert client._request.call_args.kwargs["timeout"] == client._timeout


def test_output_size_limit_rejects_before_sending_without_poisoning_retry(bridge, monkeypatch):
    host, client = bridge
    monkeypatch.setattr(protocol, "MAX_PAYLOAD", 120)
    with pytest.raises(ValueError, match="16 MiB"):
        draw(host, "A" * 30, "B" * 30)
    client._request.assert_not_called()
    assert host._output is None and client._opening
    monkeypatch.setattr(protocol, "MAX_PAYLOAD", 16 * 1024 * 1024)
    host.end_scene()
    assert len(wire(client)["upsert"]) == 2


def test_output_keeps_display_nul_policy_and_rejects_other_nuls(bridge):
    host, client = bridge
    host.begin_scene("#000000")
    host.begin_segment("0", Rect(0, 0, 100, 30))
    # Direct command recording also has to pass the native boundary policy.
    host._commands.append(["text", "A\0B", 0, 0, "#fff", 16, False])
    host.end_segment()
    host.end_scene(order=["0"])
    assert wire(client)["upsert"][0]["commands"][0][1] == "AB"
    client._request.reset_mock()
    host.begin_scene("#000000")
    host.begin_segment("bad\0id", Rect(0, 0, 100, 30))
    host.end_segment()
    with pytest.raises(ValueError, match="Embedded NUL"):
        host.end_scene()
    client._request.assert_not_called()


def test_public_open_and_prepared_content_share_startup_and_error_lifecycle():
    client = protocol.NativeClient(timeout=.5)
    def start(**kwargs):
        client._process = Mock()
    client._start = Mock(side_effect=start)
    client._request = Mock(return_value={"opened": True})
    assert client.request("open", backend="terminal") == {"opened": True}
    client._start.assert_called_once_with(terminal=True)
    assert client._request.call_args.kwargs["timeout"] is None
    client._request_serialized("frame", b'{"op":"frame","commands":[]}')
    assert client._request.call_args.kwargs["timeout"] is None
    client.request("present")
    assert client._request.call_args.kwargs["timeout"] == .5
    client._process = None
    client._abort = Mock()
    client._request.side_effect = protocol.NativeHostError("startup rejected")
    with pytest.raises(protocol.NativeHostError, match="startup rejected"):
        client.request("open", backend="terminal", hidden=True)
    client._start.assert_called_with(terminal=False)
    client._abort.assert_called_once_with("startup_failed")
