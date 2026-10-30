"""A form with horizontal/vertical steppers and application-owned validation.

Run: python examples/stepper.py --backend web
"""

from pysual import (
    App, Button, ChangeEvent, CheckBox, ClickEvent, Container, Label,
    SegmentedControl, Step, Stepper, TextBox, ValidationEvent,
)


class Demo(App):
    def build(self) -> None:
        self.title = "Pysual · Steppers"
        self.width, self.height = 860, 680
        self.layout, self.padding, self.spacing = "stack", 24, 12
        Label(parent=self, text="Create your workspace", font_size=26, height=36)
        Label(parent=self, text="Choose a numbered step to navigate.", height=24)
        self.settings = Container(layout="flow", direction="horizontal", spacing=12)
        self.orientation_choice = SegmentedControl(
            parent=self.settings, items=("Horizontal", "Vertical"), selected_index=0,
            width=280, height=40,
        )
        self.linear_choice = CheckBox(
            parent=self.settings, text="Linear flow", checked=True, width=140, height=40,
        )
        self.wizard = Stepper(flex=1)

        self.account = Step(parent=self.wizard, title="Account")
        Label(parent=self.account, text="Give your workspace a name.", height=26)
        self.name_entry = TextBox(parent=self.account, placeholder="Workspace name", height=40)

        self.preferences = Step(parent=self.wizard, title="Preferences", optional=True)
        Label(parent=self.preferences, text="Add an optional note.", height=26)
        self.note_entry = TextBox(parent=self.preferences, placeholder="A note for your team", height=40)

        self.review = Step(parent=self.wizard, title="Review", requires=[self.account])
        self.summary = Label(parent=self.review, text="Review your workspace.", word_wrap=True)
        self.note_summary = Label(parent=self.review, text="No note added.", word_wrap=True)

        self.reset_form = Button(text="Start over", width=120, height=40)

    def account_on_validating(self, event: ValidationEvent) -> None:
        if not self.name_entry.text.strip():
            event.reject("Enter a workspace name")

    def wizard_on_changed(self, event: ChangeEvent[int]) -> None:
        self.summary.text = f"Workspace: {self.name_entry.text.strip() or '(name required)'}"
        self.note_summary.text = self.note_entry.text.strip() or "No note added."

    def reset_form_on_click(self, event: ClickEvent) -> None:
        self.name_entry.text = self.note_entry.text = ""
        self.wizard.reset()

    def orientation_choice_on_changed(self, event: ChangeEvent[int]) -> None:
        self.wizard.orientation = "horizontal" if event.new_value == 0 else "vertical"

    def linear_choice_on_changed(self, event: ChangeEvent[bool]) -> None:
        self.wizard.linear = event.new_value


if __name__ == "__main__":
    from pysual import autoconfig as autoconfig
    from argparse import ArgumentParser

    ArgumentParser(description=__doc__).parse_args()
    Demo().run_blocking()
