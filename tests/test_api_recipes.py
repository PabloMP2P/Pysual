"""Execute the collection recipes as published, including their event contracts."""
from pathlib import Path
import asyncio
import re

import pytest

from pysual import App
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
