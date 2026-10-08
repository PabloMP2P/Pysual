"""Stable imports for built-in widgets; implementations live in control families."""

from __future__ import annotations
from typing import ClassVar, Literal
from .controls import Control, Container
from .events import ChangeEvent, ClosingEvent, Event
from .presentation import Outcome, Presentation
from .schema import Dirty
from .text import TextBox as TextBox
from ._controls.actions import CheckBox as CheckBox
from ._controls.ranges import Slider as Slider
from ._controls.viewports import ScrollArea as ScrollArea
from ._controls.lists import ListView as ListView
from ._controls.media import Image as Image
from ._controls.windows import SubWindow as SubWindow
