"""Build a real Pyodide asset smoke page using an already downloaded runtime.

python tests/web/build_asset_smoke.py --runtime path/to/pyodide --output dist/assets
Serve that output directory over HTTP. All four blue squares should be visible;
enter a name and click Say hello to verify Python events and text measurement.
"""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from pysual._png import encode_png
from pysual.bundle import build_web


APP = '''import asyncio
from pathlib import Path
from js import window
from pysual import App, Button, Image, Label, Style, TextBox, Theme, Toggle, Tokens
from pysual.host import CapabilityError


class AssetSmoke(App):
    def build(self):
        self.title = "Standalone included images"
        self.width, self.height = 660, 380
        self.layout = "absolute"
        self.theme = Theme(tokens=Tokens(background="#f1f4fa")).styled(
            Button, Style(fill="#ff0000")
        ).styled(Button, Style(fill="#00ff00"), state="hover").styled(
            Label, Style(foreground="#1f293b")
        ).styled(Toggle, Style(foreground="#1f293b"))
        Label(parent=self, text="Four matching blue squares: relative and __file__ paths",
              left=24, top=20, width=610, height=30)
        for index, (name, absolute) in enumerate((
            ("image.png", False), ("image.png", True),
            ("image.svg", False), ("image.svg", True),
        )):
            source = str(Path(__file__).resolve().parent / name) if absolute else name
            Label(parent=self, text=("Absolute " if absolute else "Relative ") + name[-3:].upper(),
                  left=24 + 158 * index, top=64, width=150, height=30)
            Image(parent=self, source=source, left=24 + 158 * index, top=102,
                  width=100, height=100)
        self.entry = TextBox(placeholder="Your name", left=24, top=230, width=280, height=40)
        self.greet = Button(text="Say hello", left=322, top=230, width=150, height=40)
        self.message = Label(text="Ready", left=24, top=292, width=600, height=40)
        self.accents = TextBox(text="e\\u0301" * 10, font_family="mono", font_size=16,
                              left=24, top=340, width=450, height=40)
        self.enabled_switch = Toggle(text="Enabled", left=500, top=340, width=140, height=40)

    async def greet_on_click(self, event):
        self.message.text = "Working…"
        await asyncio.sleep(0.15)
        self.message.text = f"Hello, {self.entry.text.strip() or 'world'}!"


class SecondWindow(App):
    def build(self):
        self.message = Label(text="Second window")


app = AssetSmoke()
app.run(backend="web")
try:
    SecondWindow().run(backend="web")
except CapabilityError as error:
    window.secondOpeningError = str(error)
else:
    raise AssertionError("The second browser surface should be rejected")
app.message.text = "Ready after rejected second opening"
app.wait()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    script = output / "asset_smoke.py"
    script.write_text(APP, encoding="utf-8")
    (output / "image.png").write_bytes(encode_png(1, 1, bytes((59, 130, 246, 255))))
    (output / "image.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<rect width="100" height="100" fill="#3b82f6"/></svg>', encoding="utf-8")
    print(build_web(script, output / "asset_smoke.html", options.runtime,
                    includes=("image.png", "image.svg")))


if __name__ == "__main__":
    main()
