"""UIA DEBUG_* constants read the global debug state at module import (TODO #21-3).

The constants are evaluated once at import time (module-level), so the env var
must be set BEFORE the uia module is first imported. Each test forces a fresh
import in a subprocess to isolate the module-level evaluation from the
already-imported modules in this process.
"""
import os
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
    # Build an explicit, isolated env for the subprocess: start from a copy of
    # the parent env but STRIP AGENT_WINDOWS_DEBUG, so the child's module-level
    # eval reflects only the `env_set` decision above — not a value a prior test
    # in this process left behind via enable_debug(). Without this, the
    # subprocess inherits a polluted parent env and the "unset → False" case
    # spuriously reads True when run after test_debug.py in the same session.
    env = {k: v for k, v in os.environ.items() if k != "AGENT_WINDOWS_DEBUG"}
    out = subprocess.check_output([sys.executable, "-c", code], text=True, env=env)
    return out.strip() == "True"


class TestUiaDebugConstants:
    def test_debug_on_when_env_set_before_import(self):
        assert _uia_debug_value(env_set=True) is True

    def test_debug_off_when_env_unset(self):
        assert _uia_debug_value(env_set=False) is False
