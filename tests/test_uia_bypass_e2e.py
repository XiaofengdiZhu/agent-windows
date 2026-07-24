"""Opt-in e2e: background click on UWP Calculator does not steal foreground.

Skips unless ``AGENT_WINDOWS_E2E=1`` (mirrors test_e2e_daemon.py /
test_uwp_activation.py). Mutates the real desktop — launches Notepad as a
foreground anchor and activates Calculator.

Flow: anchor the foreground on Notepad, activate + snapshot Calculator to get
an ``@eN`` ref for a number key, restore Notepad to the foreground, then
``click @eN --background`` and assert the foreground did NOT move to Calculator.
"""
import os
import subprocess
import time

import pytest

from agent_windows.core import uwp_activation as uwp
from agent_windows.core.tree import foreground_window
from agent_windows.ipc.client import call_daemon

pytestmark = pytest.mark.e2e

CALC_AUMID = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"

# Calculator's number-pad buttons are named "One".."Nine" on en-US builds but
# may surface as "1".."9" on other locales/Windows versions — accept both so the
# e2e actually finds a key across installs (otherwise it would skip live).
_NUMBER_NAMES = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
}


def _enabled() -> bool:
    return bool(os.environ.get("AGENT_WINDOWS_E2E"))


def _find_number_button_ref(snapshot_output: str) -> str | None:
    """Scan the snapshot's Interactive Elements table for a Calculator number
    button and return its ``@eN`` ref, or ``None``.

    ``call_daemon("snapshot", {})`` returns ``{"output": <markdown>}`` — the
    tree is a rendered string (an ``## Interactive Elements`` table), NOT a
    structured dict with an ``interactive_elements`` key. (That key lives only
    on the in-memory ``snapshot_diff`` baseline, never on the wire.) So we parse
    the table rows the same way an agent reads a snapshot: find a Button whose
    Name is a digit / spelled-out number and take its Ref cell.
    """
    for line in snapshot_output.splitlines():
        stripped = line.strip()
        # Data rows look like: | @e3 | Button | click | 7 | Calculator | (...) | |
        if not stripped.startswith("| @e"):
            continue
        cols = [c.strip() for c in stripped.split("|")]
        # split("|") -> ["", ref, type, action, name, window, position, state, ""]
        if len(cols) < 5:
            continue
        ref, type_name, name = cols[1], cols[2], cols[4]
        if type_name == "Button" and name.lower() in _NUMBER_NAMES:
            return ref
    return None


def _launch_notepad() -> subprocess.Popen:
    """Launch Notepad non-blocking and return its Popen handle.

    ``subprocess.run`` waits for the process to EXIT — Notepad stays open, so
    ``run`` would block until its timeout, the TimeoutExpired would be swallowed
    by a bare ``except``, and the test would spuriously skip every time.
    ``Popen`` returns immediately while still bringing Notepad to the foreground
    (the anchor we need). Caller terminates the handle in a ``finally``.
    """
    return subprocess.Popen(["notepad.exe"])


def _terminate(handles: list[subprocess.Popen]) -> None:
    """Best-effort cleanup of every Notepad we launched."""
    for proc in handles:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def test_background_click_keeps_foreground():
    """A background click on a Calculator number key must not steal foreground."""
    if not _enabled():
        pytest.skip("Set AGENT_WINDOWS_E2E=1 to run the uia-bypass e2e")

    launched: list[subprocess.Popen] = []
    import comtypes

    try:
        # 1. Anchor the foreground on Notepad (something OTHER than Calculator).
        try:
            launched.append(_launch_notepad())
        except Exception:
            pytest.skip("could not launch notepad as the foreground anchor")
        time.sleep(1.0)
        before = foreground_window().get("title", "")

        # 2. Activate Calculator and snapshot it for an @eN ref.
        #    activate_uwp_app calls CoCreateInstance, so the calling thread must
        #    be COM-initialized — the daemon's STA worker is, this test process
        #    is not until we init here (mirrors test_uwp_activation.py).
        comtypes.CoInitializeEx()
        try:
            uwp.activate_uwp_app(CALC_AUMID)
        finally:
            comtypes.CoUninitialize()
        time.sleep(1.0)

        snap = call_daemon("snapshot", {})
        ref = _find_number_button_ref(snap.get("output", ""))
        if ref is None:
            pytest.skip("no Calculator number button found in snapshot")

        # 3. Restore Notepad to the foreground, then background-click the key.
        #    The click must NOT move the foreground off Notepad to Calculator.
        launched.append(_launch_notepad())
        time.sleep(0.8)

        res = call_daemon("click", {"selector": ref, "background": True})

        after = foreground_window().get("title", "")
        assert "Calculator" not in after, (
            f"background click stole foreground to Calculator: {after!r}; "
            f"click output: {res.get('output')!r}")
    finally:
        _terminate(launched)
