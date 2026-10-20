"""Native submission equality observes output after ordinary drawing runs."""

from unittest.mock import Mock

import pytest

from pysual.backends.native import NativeHost
from pysual.geometry import Rect
from pysual.host import Viewport


@pytest.fixture
def host():
    value = NativeHost()
    value._request = Mock()
    yield value
    value.close()


def segment(host, identity, text="Same", bounds=Rect(0, 0, 100, 30)):
    host.begin_segment(identity, bounds)
    host.clip(bounds)
    host.text(text, 0, 0, "#ffffff", 16)
    host.end_segment()


def scene(host, texts=("Same",), *, order=None, background="#000000", remove=()):
    host.begin_scene(background)
    for index, text in enumerate(texts):
        segment(host, str(index), text)
    host.end_scene(order=order, remove=remove)


def test_equal_complete_output_skips_submission_after_painters_run(host):
    scene(host, ("First", "Second"), order=["0", "1"])
    scene(host, ("First", "Second"), order=["0", "1"])
    host._request.reset_mock()
    scene(host, ("First", "Second"), order=["0", "1"])
    host._request.assert_not_called()
    scene(host, ("Changed", "Second"), order=["0", "1"])
    host._request.assert_called_once()
    operation, = host._request.call_args.args
    fields = host._request.call_args.kwargs
    assert operation == "patch"
    assert [part["id"] for part in fields["upsert"]] == ["0", "1"]
    assert fields["order"] == ["0", "1"] and fields["background"] == "#000000"
    host._request.reset_mock()
    scene(host, ("Changed", "Second"), order=["0", "1"])
    host._request.assert_not_called()


def test_bounds_order_background_removal_and_readdition_are_not_suppressed(host):
    scene(host, ("First", "Second"), order=["0", "1"])
    host._request.reset_mock()
    host.begin_scene("#000000")
    segment(host, "0", "First", Rect(0, 0, 200, 30))
    host.end_scene()
    assert host._request.call_args.kwargs["upsert"][0]["bounds"] == [0, 0, 200, 30]
    scene(host, (), order=["1", "0"], background="#123456")
    assert host._request.call_args.kwargs["order"] == ["1", "0"]
    assert host._request.call_args.kwargs["background"] == "#123456"
    scene(host, (), order=["1"], remove=["0"], background="#123456")
    assert host._request.call_args.kwargs["remove"] == ["0"]
    scene(host, ("First",), order=["1", "0"], background="#123456")
    assert [part["id"] for part in host._request.call_args.kwargs["upsert"]] == ["0"]


def test_explicit_present_and_os_repaint_present_equal_output(host):
    scene(host)
    scene(host)
    host._request.reset_mock()
    host.present()
    host._request.assert_called_once_with("present", scheduled=False)
    for event in ("repaint", "resume"):
        host._request.reset_mock()
        host._on_event({"kind": event})
        host.poll()
        scene(host)
        host._request.assert_called_once_with("present", scheduled=True)
    host._request.reset_mock()
    scene(host)
    host._request.assert_not_called()


def test_failed_patch_and_presentation_leave_accepted_output_and_retry_intact(host):
    scene(host)
    scene(host)
    accepted = host._output
    host._request.side_effect = RuntimeError("rejected")
    with pytest.raises(RuntimeError, match="rejected"):
        scene(host, ("Changed",))
    assert host._output == accepted
    assert host._scene_pending is not None
    host._request.side_effect = None
    host._request.reset_mock()
    host.end_scene()
    assert host._request.call_args.args == ("patch",)
    assert host._output != accepted
    host._on_event({"kind": "repaint"})
    host.poll()
    host._request.side_effect = RuntimeError("present failed")
    with pytest.raises(RuntimeError, match="present failed"):
        scene(host, ("Changed",))
    assert host._repaint_pending
    host._request.side_effect = None
    host._request.reset_mock()
    host.end_scene()
    host._request.assert_called_once_with("present", scheduled=True)
    assert not host._repaint_pending


def test_abandoning_an_unsubmitted_recording_does_not_change_accepted_output(host):
    scene(host)
    scene(host)
    host.begin_scene("#123456")
    segment(host, "0", "Abandoned")
    host._request.reset_mock()
    scene(host)
    host._request.assert_not_called()


def test_frames_compare_output_but_still_present_and_switch_scene_modes(host):
    def frame():
        host.begin("#000000")
        host.text("Full", 0, 0, "#ffffff", 16)
        host.present()
    frame()
    host._request.reset_mock()
    frame()
    host._request.assert_called_once_with("present", scheduled=False)
    scene(host, order=["0"])
    assert host._scene_background is not None
    host._request.reset_mock()
    frame()
    assert host._request.call_args.args == ("frame",)
    assert host._scene_background is None
    host._request.reset_mock()
    scene(host, order=["0"])
    assert host._request.call_args.args == ("patch",)


def test_failed_frame_does_not_poison_accepted_output(host):
    host.begin("#000000")
    host.present()
    accepted = host._output
    host.begin("#123456")
    host._request.side_effect = RuntimeError("rejected")
    with pytest.raises(RuntimeError, match="rejected"):
        host.present()
    assert host._output == accepted
    host._request.side_effect = None
    host._request.reset_mock()
    host.present()
    assert host._request.call_args.args == ("frame",)
    assert host._output != accepted


def test_resource_revision_invalidates_comparisons_without_suppressing_repaint(host):
    host._native_resource_revision = 1
    scene(host, order=["0"])
    host._accept_viewport({"resource_revision": 2, "viewport": {"width": 960, "height": 640, "scale": 1}}, image_only=True)
    assert host._output is None
    host._request.reset_mock()
    scene(host)
    assert host._request.call_args.args == ("patch",)
    host._viewport = Viewport(960, 640, 1)
    host._accept_viewport({"resource_revision": 2, "viewport": {"width": 960, "height": 640, "scale": 2}})
    host._request.reset_mock()
    scene(host, ())
    assert host._request.call_args.args == ("patch",)
    assert host._request.call_args.kwargs["upsert"] == []


def test_equal_animation_commands_do_not_restart_the_native_timeline(host):
    def animated():
        host.begin_scene("#000000")
        host.begin_segment("animation", Rect(0, 0, 100, 20))
        host.animated_rect(Rect(0, 0, 10, 10), "#fff", from_value=0, to_value=80)
        host.end_segment()
        host.request_transition(.2)
        host.end_scene(order=["animation"])
    animated()
    animated()
    host._request.reset_mock()
    animated()
    host._request.assert_not_called()
    assert host._transition_seconds == 0


def test_duplicate_and_removed_upserts_remain_visible_to_native_validation(host):
    scene(host, order=["0"])
    host._request.reset_mock()
    host._request.side_effect = RuntimeError("native validation")
    host.begin_scene("#000000")
    segment(host, "0")
    segment(host, "0")
    with pytest.raises(RuntimeError, match="native validation"):
        host.end_scene()
    assert len(host._request.call_args.kwargs["upsert"]) == 2
    host.begin_scene("#000000")
    segment(host, "0")
    with pytest.raises(RuntimeError, match="native validation"):
        host.end_scene(remove=["0"])
    assert len(host._request.call_args.kwargs["upsert"]) == 1
    assert host._request.call_args.kwargs["remove"] == ["0"]


def test_boolean_number_edits_and_invalid_order_are_not_hidden(host):
    scene(host, order=["0"])
    host.begin_scene("#000000")
    host.begin_segment("0", Rect(0, 0, 100, 30))
    host.clip(Rect(0, 0, 100, 30))
    host.text("Same", False, 0, "#ffffff", 16)
    host.end_segment()
    host._request.reset_mock()
    host.end_scene()
    assert host._request.call_args.args == ("patch",)
    # An unchanged order cannot be removed when a new ID would make it invalid.
    host.begin_scene("#000000")
    segment(host, "new")
    host.end_scene(order=["0"])
    assert host._request.call_args.kwargs["order"] == ["0"]


def test_nonfinite_values_and_transition_fail_before_suppression(host):
    scene(host, order=["0"])
    host.begin_scene("#000000")
    host.begin_segment("0", Rect(0, 0, float("nan"), 30))
    host.end_segment()
    with pytest.raises(ValueError):
        host.end_scene()
    host.begin_scene("#000000")
    segment(host, "0")
    host.request_transition(float("inf"))
    with pytest.raises(ValueError):
        host.end_scene()


def test_accepted_signatures_do_not_alias_recorded_lists_and_close_releases_them(host):
    scene(host, order=["0"])
    accepted = host._output
    sent = host._request.call_args.kwargs["upsert"][0]
    sent["commands"][1][1] = "Mutated caller buffer"
    assert host._output == accepted
    host.close()
    assert host._output is None


def test_real_terminal_keeps_unchanged_scene_and_rejects_invalid_edits():
    from pysual.backends._native_client import NativeHostError, native_executable
    from pysual.backends.native import NativeTermTextHost

    if not native_executable().is_file():
        pytest.skip("Build the native helper")
    value = NativeTermTextHost(hidden=True)
    try:
        value.open("Output comparison", 160, 96, False, 1)
        scene(value, ("First", "Second"), order=["0", "1"])
        scene(value, ("First", "Second"), order=["0", "1"])
        before = value.snapshot()
        updates = value.native_stats()["scene_updates"]
        for _ in range(3):
            scene(value, ("First", "Second"), order=["0", "1"])
        assert value.native_stats()["scene_updates"] == updates
        assert value.snapshot() == before
        value.begin_scene("#000000")
        segment(value, "0", "First")
        segment(value, "0", "First")
        with pytest.raises(NativeHostError):
            value.end_scene()
        assert value.snapshot() == before
        value.begin_scene("#000000")
        segment(value, "0", "First")
        with pytest.raises(NativeHostError):
            value.end_scene(remove=["0"])
        assert value.snapshot() == before
        value.begin_scene("#000000")
        value.begin_segment("0", Rect(0, 0, 100, 30))
        value.clip(Rect(0, 0, 100, 30))
        value.text("First", False, 0, "#ffffff", 16)
        value.end_segment()
        with pytest.raises(NativeHostError):
            value.end_scene()
        assert value.snapshot() == before
        scene(value, ("Changed", "Second"))
        assert value.native_stats()["scene_updates"] == updates + 1
        assert value.snapshot() != before
    finally:
        value.close()


def test_real_terminal_never_suppresses_repeated_remove_and_insert():
    from pysual.backends._native_client import NativeHostError, native_executable
    from pysual.backends.native import NativeTermTextHost

    if not native_executable().is_file():
        pytest.skip("Build the native helper")
    value = NativeTermTextHost(hidden=True)
    try:
        value.open("Removal validation", 160, 96, False, 1)
        scene(value, order=["0"])
        def replace():
            value.begin_scene("#000000")
            segment(value, "new", "Inserted")
            value.end_scene(remove=["new"])
        replace()  # Removing a missing ID before inserting it is legal.
        before = value.snapshot()
        with pytest.raises(NativeHostError):
            replace()  # Now the same bytes remove and upsert an existing ID.
        assert value.snapshot() == before
    finally:
        value.close()


def test_only_the_last_acknowledged_output_can_be_suppressed(host):
    scene(host, ("A",))
    scene(host, ("B",))
    host._request.reset_mock()
    scene(host, ("A",))
    host._request.assert_called_once()
    scene(host, (), remove=["0"])
    host._request.reset_mock()
    scene(host, ("A",))
    host._request.assert_called_once()


def test_transition_only_changes_are_part_of_the_exact_output(host):
    scene(host)
    host._request.reset_mock()
    host.request_transition(.2)
    scene(host)
    assert host._request.call_args.kwargs["transition_seconds"] == .2


def test_empty_update_after_nonempty_scene_replays_without_submitting(host):
    scene(host, ("Visible",), order=["0"])
    accepted = host._output
    host._request.reset_mock()
    scene(host, ())
    host._request.assert_not_called()
    assert host._output is accepted
    for event in ("repaint", "resume"):
        host._on_event({"kind": event})
        host.poll()
        scene(host, ())
        host._request.assert_called_once_with("present", scheduled=True)
        assert host._output is accepted
        host._request.reset_mock()


def test_background_only_change_retries_and_explicit_empty_order_is_validated(host):
    scene(host, ("Visible",), order=["0"])
    accepted = host._output
    host._request.side_effect = RuntimeError("background rejected")
    with pytest.raises(RuntimeError, match="background rejected"):
        scene(host, (), background="#123456")
    assert host._output is accepted and host._scene_background == "#000000"
    host._request.side_effect = None
    host._request.reset_mock()
    host.end_scene()
    assert host._request.call_args.args == ("patch",)
    assert host._request.call_args.kwargs["background"] == "#123456"
    host._request.reset_mock()
    scene(host, (), background="#123456")
    host._request.assert_not_called()
    scene(host, (), background="#123456", order=[])
    assert host._request.call_args.kwargs["order"] == []


def test_empty_update_does_not_trust_custom_background_equality(host):
    class Color(str):
        def __eq__(self, other):
            return True
    scene(host, ("Visible",), background=Color("#000000"))
    assert type(host._scene_background) is str
    host._request.reset_mock()
    scene(host, (), background=Color("#123456"))
    assert host._request.call_args.args == ("patch",)
    assert str.__str__(host._request.call_args.kwargs["background"]) == "#123456"


def test_resource_reset_preserves_retained_present_and_revalidates_empty_update(host):
    host._native_resource_revision = 1
    scene(host, ("Visible",), order=["0"])
    host._accept_viewport({"resource_revision": 2, "viewport": {"width": 960, "height": 640, "scale": 1}})
    host._request.reset_mock()
    host.present()
    host._request.assert_called_once_with("present", scheduled=False)
    assert host._output is None
    host._request.reset_mock()
    scene(host, ())
    assert host._request.call_args.args == ("patch",)
