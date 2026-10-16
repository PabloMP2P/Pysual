"""Exercise the actual Windows wait helpers with test-only API replacements."""
import os
from pathlib import Path
import subprocess

import pytest
from tools.native import ROOT, compiler_environment


@pytest.mark.skipif(os.name != "nt", reason="Windows message/socket wait")
def test_native_message_wait_failures_and_budgets(tmp_path):
    try:
        compiler, environment = compiler_environment()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        pytest.skip(f"C compiler unavailable: {error}")
    source = Path(__file__).with_name("native_message_wait.c")
    executable = tmp_path / "native_message_wait.exe"
    includes = (ROOT / "native", ROOT / "native/vendor")
    sources = (source, ROOT / "native/terminal_renderer.c", ROOT / "native/vendor/cJSON.c")
    if Path(compiler).stem.lower() == "cl":
        command = [compiler, "/nologo", "/std:c11", "/O2", "/DCJSON_HIDE_SYMBOLS",
                   *(f"/I{path}" for path in includes), *map(str, sources),
                   f"/Fe:{executable}", "/link", "Ws2_32.lib"]
    else:
        command = [compiler, "-std=c11", "-O2", "-DCJSON_HIDE_SYMBOLS",
                   *(f"-I{path}" for path in includes), *map(str, sources),
                   "-o", str(executable), "-lws2_32", "-static-libgcc"]
    compiled = subprocess.run(command, cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=120)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    checked = subprocess.run([str(executable)], env=environment, capture_output=True, text=True, timeout=15)
    assert checked.returncode == 0, checked.stdout + checked.stderr
