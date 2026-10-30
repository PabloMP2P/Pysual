"""Backend choice is small, explicit, and usable without graphics dependencies."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pysual.backends import BACKEND_NAMES, backend_override, resolve_backend


def isolated(source, *arguments, env=None):
    environment = dict(os.environ)
    for key in ('PYSUAL_BACKEND', 'PYSUAL_TERMINAL', 'PYSUAL_THEME', 'PYSUAL_SCALE'):
        environment.pop(key, None)
    environment.update(env or {})
    path = str(Path(__file__).resolve().parents[1]/'src')
    result = subprocess.run([sys.executable, '-I', '-B', '-c',
        f'import sys;sys.path.insert(0,{path!r});\n'+source, *arguments],
        capture_output=True, text=True, env=environment, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_only_three_backend_names_and_nested_build_overrides():
    assert BACKEND_NAMES == ('window', 'terminal', 'web')
    assert resolve_backend() == 'window'
    with backend_override('web'):
        assert resolve_backend('window') == 'web'
        with backend_override('terminal'):
            assert resolve_backend('window') == 'terminal'
        assert resolve_backend() == 'web'
    assert resolve_backend() == 'window'
    with pytest.raises(ValueError):
        resolve_backend('unknown')


def test_import_does_not_consume_args_or_start_resources():
    result = isolated('''
import json,pysual
from pysual._engine import _instance
print(json.dumps([sys.argv[1:],_instance is None]))
''', '--backend', 'web')
    assert result == [['--backend', 'web'], True]


def test_import_registers_the_declared_catalog_and_legacy_factories():
    # A fresh process excludes application registrations made by other tests.
    result = isolated('''
import json,pysual
from pysual._catalog import BUILTIN_CONTROLS
expected = {entry.name: entry.control_type for entry in BUILTIN_CONTROLS}
assert len(expected) == len(BUILTIN_CONTROLS), "Duplicate built-in catalog names"
catalog = dict(pysual.registered_controls())
assert catalog == expected, catalog
factories = dict(pysual.registered_controls(factories_only=True))
assert factories == {entry.name: entry.control_type for entry in BUILTIN_CONTROLS
                     if entry.legacy_factory}, factories
for entry in BUILTIN_CONTROLS:
    assert getattr(pysual, entry.control_type.__name__) is entry.control_type
assert catalog["step"] is pysual.Step
assert catalog["stepper"] is pysual.Stepper
assert {"Step", "Stepper"}.issubset(pysual.__all__)
assert not {"step", "stepper"}.intersection(factories)
print(json.dumps(True))
''')
    assert result is True


def test_opted_in_cli_preserves_application_args_and_selects_python_terminal():
    result = isolated('''
import json
from pysual import autoconfig,terminal
from pysual._config import current
print(json.dumps([current().backend,terminal().requested_renderer,sys.argv[1:]]))
''', '--backend', 'terminal', '--terminal', 'python', '--app-option', 'value',
        env={'PYSUAL_BACKEND':'window','PYSUAL_TERMINAL':'c'})
    assert result == ['terminal','python',['--app-option','value']]


def test_default_configuration_reaches_real_terminal_factory():
    result = isolated('''
import json
from pysual import configure,terminal
configure(backend='terminal',terminal_renderer='python')
print(json.dumps(terminal().requested_renderer))
''')
    assert result == 'python'


def test_every_canonical_theme_reaches_window_defaults():
    result = isolated('''
import json
from pysual import configure,get_theme,theme_names
from pysual._config import window_defaults
resolved = []
for name in theme_names():
    configure(theme=name)
    resolved.append(window_defaults()['theme'] == get_theme(name))
print(json.dumps(resolved))
''')
    assert result == [True] * 18


@pytest.mark.parametrize('source', ['configure', 'cli', 'environment'])
def test_startup_configuration_rejects_removed_theme_names(source):
    result = isolated('''
import json,os
from pysual import configure
from pysual._config import activate
source = sys.argv[1]
rejected = []
for name in ('midnight','daylight','forest','paper','slate','arcade',
             'glass','classic','blueprint','dark','light'):
    sys.argv = ['test']
    try:
        if source == 'configure':
            configure(theme=name)
        else:
            if source == 'cli':
                sys.argv.extend(['--pysual-theme',name])
            else:
                os.environ['PYSUAL_THEME'] = name
            activate()
    except ValueError:
        rejected.append(name)
print(json.dumps(rejected))
''', source)
    assert result == ['midnight', 'daylight', 'forest', 'paper', 'slate', 'arcade',
                      'glass', 'classic', 'blueprint', 'dark', 'light']


@pytest.mark.parametrize('source', ['cli', 'environment'])
@pytest.mark.parametrize('name', ['modern', 'modern_dark'])
def test_opted_in_theme_configuration_uses_canonical_names(source, name):
    result = isolated('''
import json
from pysual import autoconfig,get_theme
from pysual._config import current,window_defaults
print(json.dumps([current().theme,window_defaults()['theme'] == get_theme(current().theme)]))
''', *(['--pysual-theme', name] if source == 'cli' else []),
        env={'PYSUAL_THEME': name} if source == 'environment' else {})
    assert result == [name, True]
