"""Incoming binding checks preserve pair ambiguity without scanning siblings."""

from itertools import product

from _ui_testcase import UIOwnerTestCase
from pysual import Button, ClickEvent, Container, Control, Event
from pysual.errors import BindingError


class ActionEvent(Control):
    action_on_click = Event(ClickEvent)


class OverlapEvent(Control):
    on_click = Event(ClickEvent)


class FalseyEvent(OverlapEvent):
    def __bool__(self):
        return False


class EventOwner(Container):
    action_on_click = Event(ClickEvent)


class BindingAmbiguityTests(UIOwnerTestCase):
    def test_collisions_are_atomic_in_both_orders_including_overlapping_separators(self):
        for name, kind, other_name, other_kind in (
            ("item", ActionEvent, "item_on_action", Button),
            ("item_on_action", Button, "item", ActionEvent),
            ("item", OverlapEvent, "item_on", Button),
            ("item_on", Button, "item", OverlapEvent),
            ("item", FalseyEvent, "item_on", Button),
        ):
            with self.subTest(existing=name, incoming=other_name, kind=kind):
                owner, candidate = Container(), other_kind()
                try:
                    setattr(owner, name, kind())
                    previous = owner.children, dict(owner._bindings)
                    with self.assertRaisesRegex(BindingError, "Ambiguous"):
                        setattr(owner, other_name, candidate)
                    self.assertEqual((owner.children, owner._bindings), previous)
                    self.assertIsNone(candidate.parent)
                    self.assertIsNone(candidate.name)
                    self.assertEqual(candidate._channels, {})
                finally:
                    owner.destroy()
                    candidate.destroy()

    def test_container_event_prefix_participates_but_aliases_do_not(self):
        owner, candidate = EventOwner(), Button()
        try:
            with self.assertRaisesRegex(BindingError, "Ambiguous"):
                owner.EventOwner_on_action = candidate
            self.assertIsNone(candidate.parent)
            self.assertIsNone(candidate.name)
            owner.primary = ActionEvent()
            owner.alias = owner.primary
            owner.alias_on_action = candidate
            self.assertEqual(candidate.name, "alias_on_action")
            self.assertNotIn("alias", owner._bindings)
            self.assertEqual(owner.primary.name, "primary")
        finally:
            owner.destroy()
            candidate.destroy()

    def test_assignment_matches_exhaustive_pair_oracle_for_ambiguous_names_and_events(self):
        event_names = ("click", "on_click", "action_on_click", "changed", "action_on_changed")
        kinds = tuple(type(f"EventKind{index}", (Control,), {name: Event(ClickEvent)})
                      for index, name in enumerate(event_names))
        names = ("item", "item_on", "item_on_action", "other", "other_on_action")
        for first_name, next_name, first_kind, next_kind in product(names, names, kinds, kinds):
            if first_name == next_name:
                continue  # Replacing a canonical binding has its own ownership contract.
            owner, candidate = Container(), next_kind()
            try:
                setattr(owner, first_name, first_kind())
                # Independent reference: enumerate the complete old pair set.
                previous = {f"{name}_on_{event}" for name, target in owner._bindings.items()
                            for event in target.events()}
                previous.update(f"{type(owner).__name__}_on_{event}" for event in owner.events())
                ambiguous = bool(previous & {f"{next_name}_on_{event}" for event in candidate.events()})
                with self.subTest(first=first_name, incoming=next_name,
                                  first_kind=first_kind, incoming_kind=next_kind):
                    if ambiguous:
                        with self.assertRaisesRegex(BindingError, "Ambiguous"):
                            setattr(owner, next_name, candidate)
                        self.assertIsNone(candidate.parent)
                        self.assertIsNone(candidate.name)
                    else:
                        setattr(owner, next_name, candidate)
                        self.assertIs(candidate.parent, owner)
                        self.assertEqual(candidate.name, next_name)
            finally:
                owner.destroy()
                candidate.destroy()
