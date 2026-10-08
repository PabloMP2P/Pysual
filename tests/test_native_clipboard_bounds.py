"""Compare the C clipboard preflight with real cJSON output, without SDL/clipboard I/O."""
from pathlib import Path
import os
import subprocess

import pytest
from tools.native import ROOT, compiler_environment


def test_c_clipboard_preflight_matches_json_reply_bounds(tmp_path):
    try:
        compiler, environment = compiler_environment()
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        pytest.skip(f"C compiler unavailable: {error}")
    source = tmp_path / "clipboard_bounds.c"
    source.write_text(r'''
#include "clipboard.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int check(const char *text, int expected) {
    cJSON *reply = cJSON_CreateObject();
    char *wire;
    int actual, serialized;
    cJSON_AddBoolToObject(reply, "ok", 1);
    cJSON_AddStringToObject(reply, "result", text);
    wire = cJSON_PrintUnformatted(reply);
    actual = clipboard_fits(text);
    serialized = strlen(text) <= PX_MAX_CLIPBOARD && strlen(wire) <= PX_MAX_PAYLOAD;
    if (actual != expected || actual != serialized) {
        fprintf(stderr, "source=%zu wire=%zu got=%d expected=%d\n",
                strlen(text), strlen(wire), actual, expected);
        return 0;
    }
    cJSON_free(wire);
    cJSON_Delete(reply);
    return 1;
}

int main(void) {
    char *text = malloc(PX_MAX_CLIPBOARD + 2u);
    size_t i, n;
    unsigned int byte;
    if (!text || !check("", 1) || !check("caf\xc3\xa9 \xe9\x9b\xaa", 1)) return 1;
    /* Every escaping class, including non-ASCII UTF-8, fits when short. */
    for (byte = 1; byte < 128; byte++) {
        memset(text, byte, 100); text[100] = 0;
        if (!check(text, 1)) return 2;
    }
    memset(text, 'a', PX_MAX_CLIPBOARD + 1u);
    text[PX_MAX_CLIPBOARD] = 0;
    if (!check(text, 1)) return 3;
    text[PX_MAX_CLIPBOARD] = 'a'; text[PX_MAX_CLIPBOARD + 1u] = 0;
    if (!check(text, 0)) return 4;
    for (i = 0; i < PX_MAX_CLIPBOARD; i += 2) {
        text[i] = (char)0xc3; text[i + 1] = (char)0xa9;
    }
    text[PX_MAX_CLIPBOARD] = 0;
    if (!check(text, 1)) return 5;
    for (byte = 1; byte <= 2; byte++) {
        /* U+0001 expands sixfold; newline expands twofold. */
        n = (PX_MAX_PAYLOAD - 23u) / (byte == 1 ? 6u : 2u);
        memset(text, byte == 1 ? 1 : '\n', n + 1);
        text[n] = 0;
        if (!check(text, 1)) return 6;
        text[n] = byte == 1 ? 1 : '\n'; text[n + 1] = 0;
        if (!check(text, 0)) return 7;
    }
    free(text);
    return 0;
}
''', encoding="utf-8")
    executable = tmp_path / ("clipboard_bounds.exe" if os.name == "nt" else "clipboard_bounds")
    includes = (ROOT / "native", ROOT / "native/vendor")
    sources = (source, ROOT / "native/vendor/cJSON.c")
    if Path(compiler).stem.lower() == "cl":
        command = [compiler, "/nologo", "/std:c11", "/O2", "/DCJSON_HIDE_SYMBOLS",
                   *(f"/I{path}" for path in includes), *map(str, sources), f"/Fe:{executable}"]
    else:
        command = [compiler, "-std=c11", "-O2", "-DCJSON_HIDE_SYMBOLS",
                   *(f"-I{path}" for path in includes), *map(str, sources), "-o", str(executable), "-lm"]
    subprocess.run(command, cwd=tmp_path, env=environment, check=True, capture_output=True)
    subprocess.run([str(executable)], check=True, capture_output=True, timeout=15)
