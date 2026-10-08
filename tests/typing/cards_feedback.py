from pysual import (
    AlertBanner, Badge, ChangeEvent, ChoiceCard, ClickEvent, Container,
    Disclosure, Label, Meter, UiEvent, blueprint,
)

panel = Container()
choice: ChoiceCard = panel.create(ChoiceCard, text="Focused", group="approach", checked=True)
details: Disclosure = panel.create(Disclosure, title="Advanced", expanded=True)
Label(parent=details.content, text="Independent content")
badge: Badge = panel.create(Badge, text="Ready", tone="success")
notice: AlertBanner = panel.create(AlertBanner, text="Review ready", action_text="Open")
level: Meter = panel.create(Meter, value=40, warning_at=75, danger_at=90, segments=20)
level.update(value=80, maximum=120)
level.set_range(0, 100, value=70)


def on_action(event: ClickEvent) -> None:
    print(event.source)


def on_dismissed(event: UiEvent) -> None:
    print(event.origin)


def on_changed(event: ChangeEvent[bool]) -> None:
    print(event.new_value)


notice.action.connect(on_action)
notice.dismissed.connect(on_dismissed)
choice.changed.connect(on_changed)


class Review(Container):
    status = blueprint(Badge, text="Ready")
    settings = blueprint(Disclosure, title="Settings")


ChoiceCard(checked="yes")  # error: reportArgumentType
Disclosure(expanded=1)  # error: reportArgumentType
Badge(tone="purple")  # error: reportArgumentType
AlertBanner(dismissible="yes")  # error: reportArgumentType
Meter(segments=2.5)  # error: reportArgumentType
Meter(warning_at="high")  # error: reportArgumentType
