"""A custom control with observable sizing and portable painting."""

from pysual import App, Control, Dirty, Label, Rect, Slider, prop


class Meter(Control):
    value: float = prop(default=0.5, minimum=0)
    caption: str = prop(default="Level", affects=Dirty.MEASURE | Dirty.PAINT)

    def _validate_update(self, name, value):
        super()._validate_update(name, value)
        if name == "value" and value > 1:
            raise ValueError("Meter value must be between 0 and 1")

    def measure(self, host):
        width, height = host.measure(self.caption, 16)
        return width + 24, max(32, height + 16)

    def paint(self, painter):
        area = Rect(0, 0, self.bounds.width, self.bounds.height)
        painter.rect(area, "#23334a", radius=10)
        filled = Rect(area.x, area.y, area.width * self.value, area.height)
        painter.rect(filled, "#127c6a", radius=10)
        _, text_height = painter.measure(self.caption, size=16, font_family="ui")
        painter.text(self.caption, 12, (area.height - text_height) / 2,
                     color="#ffffff", size=16, font_family="ui")


class Demo(App):
    def build(self):
        self.title = "Custom control"
        self.width, self.height = 560, 280
        self.layout, self.padding, self.spacing = "stack", 24, 16
        self.heading = Label(text="Move the slider", font_size=22)
        self.meter = Meter(caption="Output level", height=48)
        self.level = Slider(value=0.5, minimum=0, maximum=1, step=0.01)

    def level_on_changed(self, event):
        self.meter.value = float(event.new_value)


if __name__ == "__main__":
    from pysual import autoconfig
    from argparse import ArgumentParser

    ArgumentParser(description=__doc__).parse_args()
    Demo().run_blocking()
