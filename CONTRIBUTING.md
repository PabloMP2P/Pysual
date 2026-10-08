# Contributing

Pysual is the library, the `pysual` import, and the `pysual` installable package.
The repository is [Pysual](https://github.com/PabloMP2P/Pysual).

## Setup

```sh
git clone https://github.com/PabloMP2P/Pysual.git
cd Pysual
python -m venv .venv
# Activate .venv using your shell, then:
python -m pip install -e '.[dev,build]'
python -m pytest
```

The [README](README.md#develop) covers the factory-type generator.
[Building](docs/building.md) covers the typing check, native build, compilers,
and packaging.

## Style

Python follows [PEP 8](https://peps.python.org/pep-0008/). Match the module you
are editing, including its import order and docstring style.

Hand-written C in `native/host.c`, `native/sdl_renderer.c`, and
`native/terminal_renderer.c` follows `.clang-format`. Leave generated Unicode
tables and vendor sources unchanged.

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/):

```
type: short imperative summary
```

An optional scope is fine: `fix(terminal): keep selection inside the viewport`.

Use `feat`, `fix`, `docs`, `test`, `refactor`, `build`, `ci`, or `chore`.
Write the summary in the imperative, lowercase, with no trailing period.
Keep each commit to one logical change.
