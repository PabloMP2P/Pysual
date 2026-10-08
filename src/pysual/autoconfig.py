"""Opt into Pysual's CLI/environment defaults before parsing application args.

    from pysual import autoconfig

Normal Pysual imports never activate this module. Reimport/reload is harmless;
the successful activation record belongs to the configuration owner.
"""

from ._config import activate as _activate

_activate()
