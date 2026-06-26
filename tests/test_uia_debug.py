"""UIA DEBUG_* constants read the global debug state at module import (TODO #21-3).

The constants are evaluated once at import time (module-level), so the env var
must be set BEFORE the uia module is first imported. Each test forces a fresh
import in a subprocess to isolate the module-level evaluation from the
already-imported modules in this process.
"""
import subprocess
import sys
import textwrap


def _uia_debug_value(env_set: bool) -> bool:
    code = textwrap.dedent(f"""
        import os
        {'os.environ["AGENT_WINDOWS_DEBUG"] = "1"' if env_set else 'pass'}
        import agent_windows.uia.enums as e
        print(e.DEBUG_SEARCH_TIME)
    """)
    out = subprocess.check_output([sys.executable, "-c", code], text=True)
    return out.strip() == "True"


class TestUiaDebugConstants:
    def test_debug_on_when_env_set_before_import(self):
        assert _uia_debug_value(env_set=True) is True

    def test_debug_off_when_env_unset(self):
        assert _uia_debug_value(env_set=False) is False
