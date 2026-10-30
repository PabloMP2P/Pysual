"""Execute the collection recipes as published, including their event contracts."""
from pathlib import Path
import asyncio
import re

import pytest

from pysual import App, Container
from pysual._engine import call
from pysual.host import Input
from test_library import RecordingHost, eventually


GUIDE = Path(__file__).resolve().parents[1] / "docs/api.md"
RECIPES = dict(re.findall(
    r"<!-- recipe: ([\w-]+) -->\n```python\n(.*?)\n```",
    GUIDE.read_text(encoding="utf-8"), re.DOTALL,
))


def test_panel_factory_recipe():
    namespace = {}
    try:
        exec(compile(RECIPES["panel-factory"], "docs/api.md:panel-factory", "exec"), namespace)
        assert namespace["direct"].layout == namespace["factory"].layout == "stack"
        assert namespace["direct"].parent is namespace["factory"].parent is namespace["parent"]
    finally:
        if "parent" in namespace:
            namespace["parent"].destroy()


def test_stepper_recipe_validates_and_repairs_without_field_handlers():
    class Recipe(App):
        def build(self):
            namespace = {"panel": Container(parent=self, layout="stack", width=600, height=400)}
            exec(compile(RECIPES["stepper"], "docs/api.md:stepper", "exec"), namespace)
            self._recipe = namespace

    app = Recipe()
    try:
        app.run(backend=RecordingHost())
        namespace = app._recipe
        wizard, account, name = (namespace[key] for key in ("wizard", "account", "name"))

        def wait_until(predicate):
            asyncio.run(eventually(lambda: call(predicate)))

        def click_indicator(index):
            def click():
                app._runtime._arrange_scene()
                headers, _ = wizard._geometry(wizard.content_bounds(wizard.bounds))
                header = headers[index]
                x, y = header.x + 28, header.y + header.height / 2
                assert app._runtime.router.hit(app, x, y) is wizard
                for kind in ("pointer_down", "pointer_up"):
                    app._runtime._process_input(Input(kind, x=x, y=y))
            call(click)

        assert not account.error and not wizard.can_select(1)
        click_indicator(1)
        wait_until(lambda: account.error == "Enter your name")
        assert wizard.selected_index == 0 and not account.completed
        name.text = "Zoë's workspace"
        wait_until(lambda: account.completed and not account.error)
        assert wizard.selected_index == 0  # Repairing feedback does not navigate.
        click_indicator(1)
        wait_until(lambda: wizard.selected_index == 1)
        assert account.completed and not account.error
        assert not namespace["review"].completed
        click_indicator(0)
        wait_until(lambda: wizard.selected_index == 0)
        name.text = "   "
        wait_until(lambda: not account.completed and account.error == "Enter your name")
        assert not wizard.can_select(1)
        assert wizard.selected_index == 0
        name.text = "Repaired name"
        wait_until(lambda: account.completed and not account.error)
        click_indicator(1)
        wait_until(lambda: wizard.selected_index == 1)
        assert account.completed and not account.error
        assert not namespace["review"].completed
        wizard.reset()
        assert wizard.selected_index == 0 and not account.completed and not account.error
        assert name.text == "Repaired name"  # Reset clears progress, not application field values.
    finally:
        app.close()
        app.wait(timeout=5)
        app.destroy()


@pytest.mark.parametrize("name,control", [("grid-data", "grid"), ("tree-data", "tree")])
def test_collection_recipe(name, control):
    namespace = {}
    app = App()
    opened = False
    try:
        exec(compile(RECIPES[name], f"docs/api.md:{name}", "exec"), namespace)
        app.collection = namespace[control]
        app.run(backend=RecordingHost())
        opened = True
        if name == "grid-data":
            namespace["grid"].set_cell("cedar", "revenue", 700)
            asyncio.run(eventually(lambda: len(namespace["edits"]) == 1))
            event, = namespace["edits"]
            assert (event.row_key, event.column_key, event.old_value, event.new_value,
                    event.origin) == ("cedar", "revenue", 630, 700, "program")
        else:
            namespace["tree"].selected_key = "archive"
            asyncio.run(eventually(lambda: namespace["selections"] == ["archive"]))
    finally:
        if opened:
            app.close()
            app.wait(timeout=5)
        app.destroy()
        if control in namespace:
            namespace[control].destroy()
