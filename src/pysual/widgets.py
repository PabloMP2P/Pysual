"""Stable imports for built-in widgets; implementations live in control families.

Published controls set ``__module__`` to this module. Their annotations are
resolved against these bindings, so the names stay imported even when this
file does not mention them in executable code.
"""

from __future__ import annotations

from typing import ClassVar as ClassVar, Literal as Literal

from .controls import Control as Control, Container as Container
from .events import ChangeEvent as ChangeEvent, ClosingEvent as ClosingEvent, Event as Event
from .presentation import Outcome as Outcome, Presentation as Presentation
from .schema import Dirty as Dirty
from .text import TextBox as TextBox
from ._controls.actions import CheckBox as CheckBox
from ._controls.ranges import Slider as Slider
from ._controls.viewports import ScrollArea as ScrollArea
from ._controls.lists import ListView as ListView
from ._controls.media import Image as Image
from ._controls.windows import SubWindow as SubWindow
