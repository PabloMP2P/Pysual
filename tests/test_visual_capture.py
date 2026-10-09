"""Visual fixtures retain exact dimensions at the configured capture scale."""

import json
import sys

from PIL import Image
import pytest

from tools import check_visual


@pytest.mark.parametrize("arguments,scale", [([], 1.5), (["--native-scale", "1"], 1)])
def test_native_capture_scale_preserves_logical_fixture(tmp_path, monkeypatch, arguments, scale):
    captured = []

    def capture(theme, path, *, scale):
        captured.append((theme, scale))
        Image.new("RGB", tuple(int(value * scale) for value in check_visual.SIZE)).save(path)
        return {"renderer": "test", "scale": scale}

    monkeypatch.setattr(check_visual, "capture_native", capture)
    monkeypatch.setattr(sys, "argv", ["check_visual", "--backend", "native", "--mode", "capture",
                                    "--output", str(tmp_path), *arguments])
    assert check_visual.main() == 0
    assert captured == [(theme, scale) for theme in check_visual.THEMES]
    environment = json.loads((tmp_path / "native/environment.json").read_text(encoding="utf-8"))
    assert environment["size"] == [800, 560]
    assert environment["scale"] == scale


def test_native_capture_rejects_clamping_instead_of_trusting_reported_scale(tmp_path, monkeypatch):
    def capture(theme, path, *, scale):
        Image.new("RGB", (400, 280)).save(path)
        return {"renderer": "test", "scale": .5}

    monkeypatch.setattr(check_visual, "capture_native", capture)
    monkeypatch.setattr(sys, "argv", ["check_visual", "--backend", "native", "--mode", "capture",
                                    "--native-scale", "1", "--output", str(tmp_path)])
    with pytest.raises(AssertionError, match="800, 560"):
        check_visual.main()
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert not result["success"]
