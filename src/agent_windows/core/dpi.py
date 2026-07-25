"""DPI awareness setup with HRESULT verification + readback.

SetProcessDpiAwareness returns an HRESULT; ctypes.windll does NOT raise on
non-zero HRESULT by default, so a silent failure (E_ACCESSDENIED = "already
set") is invisible unless we read the return value AND confirm the actual
state via GetProcessDpiAwareness. This module surfaces that.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

# PROCESS_DPI_AWARENESS enum values
PER_MONITOR = 2  # PROCESS_DPI_AWARENESS_PER_MONITOR_AWARE

# HRESULTs
S_OK = 0
E_ACCESSDENIED = 0x80070005  # "DPI awareness already set"


@dataclass
class DpiResult:
    """Outcome of an ensure_process_dpi_awareness() attempt.

    Attributes:
        ok: True iff the process is actually at ``requested`` after the call
            (success OR already-at-target both count). False = real gap —
            UIA/SendInput coordinates may be virtualized on scaled displays.
        actual: readback awareness (0=unaware, 1=system, 2=permonitor),
            or -1 if GetProcessDpiAwareness itself failed.
        requested: the awareness value that was requested.
        detail: human-readable HRESULT/explanation string for logging.
    """
    ok: bool
    actual: int
    requested: int
    detail: str


def get_process_dpi_awareness() -> int:
    """Read the current process DPI awareness via GetProcessDpiAwareness.

    Returns:
        0 (unaware), 1 (system), or 2 (per-monitor), or -1 if the API call
        fails (unsupported platform / ctypes error).
    """
    try:
        shcore = ctypes.windll.shcore
        shcore.GetProcessDpiAwareness.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(ctypes.c_int)]
        shcore.GetProcessDpiAwareness.restype = ctypes.c_long  # HRESULT
        value = ctypes.c_int(0)
        hr = shcore.GetProcessDpiAwareness(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.pointer(value))
        if hr != S_OK:
            return -1
        return value.value
    except Exception:
        return -1


def ensure_process_dpi_awareness(target: int = PER_MONITOR) -> DpiResult:
    """Set process DPI awareness to ``target`` and verify via readback.

    SetProcessDpiAwareness returns an HRESULT; ctypes.windll does not raise on
    non-zero HRESULT, so we read it explicitly. ``E_ACCESSDENIED`` means
    awareness was already set (by manifest or a prior call) — not necessarily
    a failure, so we read back GetProcessDpiAwareness and judge by ``actual``.

    Args:
        target: desired PROCESS_DPI_AWARENESS value (default PER_MONITOR=2).

    Returns:
        DpiResult with ok=True iff the process is actually at ``target`` after
        the call. ok=False means UIA/SendInput coordinates may be virtualized
        on scaled displays — the silent failure this module exists to surface.
    """
    actual_before = get_process_dpi_awareness()
    detail = ""
    try:
        shcore = ctypes.windll.shcore
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long  # HRESULT
        hr = shcore.SetProcessDpiAwareness(target)
        if hr == S_OK:
            detail = "set S_OK"
        elif hr == E_ACCESSDENIED:
            detail = f"already set E_ACCESSDENIED (was={actual_before})"
        else:
            detail = f"HRESULT=0x{hr & 0xFFFFFFFF:08X}"
    except Exception as e:
        detail = f"ctypes error: {e}"

    actual = get_process_dpi_awareness()
    ok = (actual == target)
    return DpiResult(ok=ok, actual=actual, requested=target, detail=detail)
