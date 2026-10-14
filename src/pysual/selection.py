"""Stable imports for selection controls, implemented in focused control families.

Published controls set ``__module__`` to this module. Their annotations are
resolved against these bindings, so the names stay imported even when this
file does not mention them in executable code.
"""

from typing import Callable as Callable, ClassVar as ClassVar

from .events import ChangeEvent as ChangeEvent, Event as Event
from .schema import Dirty as Dirty
from ._controls.actions import Toggle as Toggle, RadioButton as RadioButton
from ._controls.ranges import ProgressBar as ProgressBar, Rating as Rating
from ._controls.choices import Dropdown as Dropdown, ComboBox as ComboBox
from ._controls.choices import _Choices as _Choices, _open_choices as _open_choices
