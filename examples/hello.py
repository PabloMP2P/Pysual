"""A small interface with automatically bound synchronous and async events."""

import asyncio
from pysual import App, Button, Label, TextBox


class Hello(App):
    def build(self):
        self.title = "Hello, Pysual"
        self.width, self.height = 480, 300
        self.layout, self.padding, self.spacing = "stack", 24, 12
        self.name_entry = TextBox(placeholder="Your name", height=40)
        self.greet = Button(text="Say hello", height=40)
        self.message = Label(text="One Python app. Three places to run it.")
        self.finish = Button(text="Close", height=40)

    async def greet_on_click(self, event):
        self.message.text = "Working…"
        await asyncio.sleep(0.15)
        self.message.text = f"Hello, {self.name_entry.text.strip() or 'world'}!"

    def finish_on_click(self, event):
        self.close()


if __name__ == "__main__":
    from pysual import autoconfig
    from argparse import ArgumentParser

    ArgumentParser(description=__doc__).parse_args()
    Hello().run_blocking()
