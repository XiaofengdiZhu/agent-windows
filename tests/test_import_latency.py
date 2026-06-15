"""M-12: importing the CLI app must stay light.

Importing ``agent_windows.cli.main`` registers every command, so any top-level
``import win32pipe`` / ``from PIL import Image`` / ``import openai`` in the
command → dispatcher → ipc chain would inflate every CLI invocation's startup
with pywin32 / comtypes / PIL / openai init (the original M-12 concern). The
code keeps these lazy (``ipc/pipe.py`` lazy-imports pywin32; the command modules
lazy-import grounding → PIL/openai), so merely importing the app loads none of
them — this test locks that discipline in. A fresh subprocess isolates the
check from the already-imported pytest process.
"""
import subprocess
import sys

# Module roots that must NOT be loaded merely by importing the CLI app. pywin32
# (win32* / pythoncom / pywintypes), comtypes, and the vision stack (PIL/openai)
# are all heavy and must stay lazy.
_HEAVY_ROOTS = (
    "win32pipe", "win32file", "win32security", "win32api", "win32event",
    "pythoncom", "pywintypes", "comtypes", "PIL", "openai",
)


def test_cli_import_loads_no_heavy_deps():
    roots = "(" + ",".join(repr(r) for r in _HEAVY_ROOTS) + ")"
    code = (
        "import sys, agent_windows.cli.main as _; "
        "heavy = sorted(m for m in sys.modules if m.split('.')[0] in " + roots
        + "); print('|'.join(heavy))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, (
        f"importing agent_windows.cli.main failed:\n{result.stderr}"
    )
    loaded = [m for m in result.stdout.strip().split("|") if m]
    assert loaded == [], (
        "Importing the CLI app loaded heavy deps that should be lazy-imported "
        f"(M-12 regression): {loaded}"
    )
