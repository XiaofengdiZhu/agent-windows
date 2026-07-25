"""UIPI integrity-level preflight — detect SendInput silent-drop at elevated targets.

A medium-IL daemon ``SendInput``-ing at a higher-IL (elevated) window has its
input silently dropped by UIPI; the handler still returns success, leaving the
agent in a retry loop. This module reads the daemon's own integrity level
(cached once) and a target window's process IL, and reports whether a
``SendInput`` at that target would be dropped. Pure ctypes (no comtypes/uia),
mirroring ``core/dpi.py``.

The degrade contract is load-bearing: ANY token-read failure (NULL hwnd,
ACCESS_DENIED, GetTokenInformation fail, API missing, EDR exception) returns
``None`` so the caller treats the target as unknown and does NOT block the
command. The preflight must never turn a would-succeed command into a failure
because it could not read a token.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

# Mandatory integrity RIDs (SECURITY_MANDATORY_*_RID).
SECURITY_MANDATORY_LOW_RID = 0x1000
SECURITY_MANDATORY_MEDIUM_RID = 0x2000
SECURITY_MANDATORY_HIGH_RID = 0x3000
SECURITY_MANDATORY_SYSTEM_RID = 0x4000

_RID_NAMES = {
    SECURITY_MANDATORY_LOW_RID: "low",
    SECURITY_MANDATORY_MEDIUM_RID: "medium",
    SECURITY_MANDATORY_HIGH_RID: "high",
    SECURITY_MANDATORY_SYSTEM_RID: "system",
}


def il_name(rid: int | None) -> str:
    """Human-readable name for a mandatory-integrity RID.

    Returns 'low'/'medium'/'high'/'system' for the four canonical RIDs, or
    'unknown' for an unrecognized RID or ``None`` (a failed/degraded read).
    ``None`` is not a dict key, so ``_RID_NAMES.get(None, "unknown")``
    returns "unknown" with no extra ternary. Keeps ``_RID_NAMES`` private to
    this module — callers use this helper instead of touching the dict.
    """
    return _RID_NAMES.get(rid, "unknown")

# Win32 constants.
TOKEN_QUERY = 0x0008
TokenIntegrityLevel = 25  # TOKEN_INFORMATION_CLASS
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass
class UipiResult:
    """Outcome of a UIPI preflight check.

    Attributes:
        blocked: True iff own and target were both read AND target > own. A
            ``None`` on either side means "unknown" and forces False.
        own_il: the daemon's own RID, or None if the read failed.
        target_il: the target window process RID, or None if the read failed.
        detail: short human-readable note for logging.
    """
    blocked: bool
    own_il: int | None
    target_il: int | None
    detail: str


# ---- ctypes structs for TOKEN_MANDATORY_LABEL parsing ----------------------

class _SID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]


class _TOKEN_MANDATORY_LABEL(ctypes.Structure):
    _fields_ = [("Label", _SID_AND_ATTRIBUTES)]


# ---- layer 1: ctypes primitives --------------------------------------------

def _read_token_il(token_handle: int) -> int | None:
    """Read a token's integrity RID via GetTokenInformation + GetSidSubAuthority.

    Returns None on any failure (degrade). The caller owns the token handle.
    """
    try:
        advapi32 = ctypes.windll.advapi32
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_uint, ctypes.c_void_p,
            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

        needed = wintypes.DWORD(0)
        # First call: probe length. Expected to return FALSE (insufficient buffer).
        # pointer() (not byref()) so test fakes can write [0] (core/dpi.py lesson).
        advapi32.GetTokenInformation(
            token_handle, TokenIntegrityLevel, None, 0, ctypes.pointer(needed))
        if not needed.value:
            return None
        buf = (ctypes.c_char * needed.value)()
        got = wintypes.DWORD(0)
        if not advapi32.GetTokenInformation(
                token_handle, TokenIntegrityLevel, buf, needed.value,
                ctypes.pointer(got)):
            return None
        tml = ctypes.cast(buf, ctypes.POINTER(_TOKEN_MANDATORY_LABEL)).contents
        rid_ptr = advapi32.GetSidSubAuthority(tml.Label.Sid, 0)
        return int(rid_ptr[0])
    except (OSError, AttributeError, ValueError):
        return None


def _open_process_token_for_pid(pid: int) -> int | None:
    """OpenProcessToken(PROCESS_QUERY_LIMITED_INFORMATION) for a pid; None on fail."""
    try:
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL

        hproc = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not hproc:
            return None
        try:
            token = wintypes.HANDLE(0)
            # pointer() (not byref()) so test fakes can write [0] (core/dpi.py lesson).
            if not advapi32.OpenProcessToken(
                    hproc, TOKEN_QUERY, ctypes.pointer(token)):
                return None
            # HANDLE.value is the raw int handle (or None if null). int(HANDLE)
            # itself raises ValueError under Python 3.13 — read .value, don't
            # convert the c_void_p instance (core/dpi.py lesson, c_void_p caveat).
            return token.value if token.value else None
        finally:
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.CloseHandle(hproc)
    except (OSError, AttributeError, ValueError):
        return None


def _pid_of_hwnd(hwnd: int) -> int | None:
    """GetWindowThreadProcessId -> pid; None on failure."""
    try:
        user32 = ctypes.windll.user32
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        pid = wintypes.DWORD(0)
        # pointer() (not byref()) so test fakes can write [0] (core/dpi.py lesson).
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.pointer(pid)):
            return None
        return int(pid.value) if pid.value else None
    except (OSError, AttributeError, ValueError):
        return None


# ---- layer 2: own-IL cache -------------------------------------------------

_UNSET = object()
_OWN_IL: object = _UNSET


def _read_own_il_uncached() -> int | None:
    """Read the daemon process's own integrity RID; None on failure."""
    try:
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        token = wintypes.HANDLE(0)
        # GetCurrentProcess() returns a pseudo-handle (-1) that needs NO close;
        # but OpenProcessToken opens a real TOKEN handle below, which MUST be
        # closed. pointer() (not byref()) so test fakes can write [0] (core/dpi.py).
        if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.pointer(token)):
            return None
        try:
            return _read_token_il(token)
        finally:
            kernel32.CloseHandle(token)
    except (OSError, AttributeError, ValueError):
        return None


def own_integrity_level() -> int | None:
    """Return the daemon's own integrity RID (cached after first read).

    First read wins, including None (read failed) — own IL is process-fixed
    and re-reading after a failure won't help. The daemon's single COM STA
    worker means no concurrency, so no lock.
    """
    global _OWN_IL
    if _OWN_IL is not _UNSET:
        return _OWN_IL  # type: ignore[return-value]
    _OWN_IL = _read_own_il_uncached()
    return _OWN_IL  # type: ignore[return-value]


def _reset_own_il_cache_for_tests() -> None:
    """Test-only: clear the own-IL cache so monkeypatching sticks between tests."""
    global _OWN_IL
    _OWN_IL = _UNSET


# ---- layer 3: target reads + check logic -----------------------------------

def integrity_level_at(x: int, y: int) -> int | None:
    """Target process IL at screen coord (x,y); None if unknown."""
    try:
        user32 = ctypes.windll.user32
        user32.WindowFromPoint.argtypes = [wintypes.POINT]
        user32.WindowFromPoint.restype = wintypes.HWND
        hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
        if not hwnd:
            return None
        return _hwnd_to_il(hwnd)
    except (OSError, AttributeError, ValueError):
        return None


def integrity_level_foreground() -> int | None:
    """Target process IL of the foreground window; None if unknown."""
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        return _hwnd_to_il(hwnd)
    except (OSError, AttributeError, ValueError):
        return None


def _hwnd_to_il(hwnd: int) -> int | None:
    """hwnd -> pid -> OpenProcessToken -> IL RID; None if any step fails."""
    pid = _pid_of_hwnd(hwnd)
    if pid is None:
        return None
    token = _open_process_token_for_pid(pid)
    if token is None:
        return None
    try:
        return _read_token_il(token)
    finally:
        try:
            ctypes.windll.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            ctypes.windll.kernel32.CloseHandle.restype = wintypes.BOOL
            ctypes.windll.kernel32.CloseHandle(token)
        except (OSError, AttributeError):
            pass


def _check(own: int | None, target: int | None, detail: str) -> UipiResult:
    blocked = own is not None and target is not None and target > own
    return UipiResult(blocked=blocked, own_il=own, target_il=target, detail=detail)


def check_uipi_at(x: int, y: int) -> UipiResult:
    """Preflight a foreground click/type-at (x,y): would UIPI drop it?"""
    own = own_integrity_level()
    target = integrity_level_at(x, y)
    return _check(own, target, f"at=({x},{y})")


def check_uipi_foreground() -> UipiResult:
    """Preflight a focused-field type/key (foreground target)."""
    own = own_integrity_level()
    target = integrity_level_foreground()
    return _check(own, target, "foreground")
