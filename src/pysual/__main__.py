"""`python -m pysual` builds or runs an application; see `pysual --help`."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
