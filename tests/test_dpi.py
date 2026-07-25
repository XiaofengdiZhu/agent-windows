"""Tests for core/dpi.py — DPI awareness setup with HRESULT verification.

Mocks ctypes.windll.shcore (in-process, no real DPI mutation). Host-independent.
"""
import ctypes

import pytest


class _Func:
    """Callable stand-in for a ctypes function.

    Real cdll function objects accept .argtypes/.restype attribute assignment;
    a plain bound method does not. _Func carries those attributes so the impl's
    `shcore.X.argtypes = [...]` lines don't AttributeError under mock.
    """
    def __init__(self, ret_fn):
        self._ret_fn = ret_fn           # (args_tuple) -> HRESULT
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._ret_fn(args)


class _FakeShcore:
    """Stand-in for ctypes.windll.shcore. Records args; returns scripted HR/value."""
    def __init__(self, set_hr=0, get_hr=0, get_value=2):
        self.set_hr = set_hr            # HRESULT returned by SetProcessDpiAwareness
        self.get_hr = get_hr            # HRESULT returned by GetProcessDpiAwareness
        self.get_value = get_value      # awareness value read back
        self.set_calls = []             # list of target args passed to Set
        self.get_calls = 0

        def _set(args):
            self.set_calls.append(args[0])
            return self.set_hr

        def _get(args):
            self.get_calls += 1
            args[1][0] = self.get_value  # ctypes.pointer(c_int) writeback via [0]
            return self.get_hr

        self.SetProcessDpiAwareness = _Func(_set)
        self.GetProcessDpiAwareness = _Func(_get)


@pytest.fixture
def fake_shcore(monkeypatch):
    """Inject a _FakeShcore as ctypes.windll.shcore for the module under test.

    core/dpi.py reads `ctypes.windll.shcore` inside each function (not cached at
    module import), so monkeypatching the attribute is safe and reversible.
    """
    fake = _FakeShcore()
    monkeypatch.setattr(ctypes.windll, "shcore", fake)
    return fake


def test_get_reads_current(fake_shcore):
    """get_process_dpi_awareness returns the value GetProcessDpiAwareness writes."""
    fake_shcore.get_value = 2
    from agent_windows.core.dpi import get_process_dpi_awareness
    assert get_process_dpi_awareness() == 2


def test_get_returns_neg_on_fail(fake_shcore):
    """Non-zero HRESULT from GetProcessDpiAwareness -> -1 (read failure)."""
    fake_shcore.get_hr = 0x80004005  # E_FAIL
    from agent_windows.core.dpi import get_process_dpi_awareness
    assert get_process_dpi_awareness() == -1


def test_ensure_success(fake_shcore):
    """S_OK set + readback matches -> ok=True."""
    fake_shcore.set_hr = 0           # S_OK
    fake_shcore.get_value = 2        # readback per-monitor
    from agent_windows.core.dpi import ensure_process_dpi_awareness, PER_MONITOR
    r = ensure_process_dpi_awareness(PER_MONITOR)
    assert r.ok is True
    assert r.actual == 2
    assert r.requested == 2
    assert "S_OK" in r.detail
    assert fake_shcore.set_calls == [2]


def test_ensure_already_at_target(fake_shcore):
    """E_ACCESSDENIED (already set) + readback matches -> ok=True.

    This is the core fix: non-zero HRESULT is NOT a failure when the process
    is already at the target awareness. The old inline call logged INFO on
    this path without verifying; we verify via readback.
    """
    fake_shcore.set_hr = 0x80070005  # E_ACCESSDENIED
    fake_shcore.get_value = 2        # readback per-monitor (already there)
    from agent_windows.core.dpi import ensure_process_dpi_awareness, PER_MONITOR
    r = ensure_process_dpi_awareness(PER_MONITOR)
    assert r.ok is True
    assert r.actual == 2
    assert "E_ACCESSDENIED" in r.detail


def test_ensure_actually_system(fake_shcore):
    """E_ACCESSDENIED + readback system (1) -> ok=False (real gap exposed)."""
    fake_shcore.set_hr = 0x80070005  # E_ACCESSDENIED
    fake_shcore.get_value = 1        # readback system-aware — NOT per-monitor
    from agent_windows.core.dpi import ensure_process_dpi_awareness, PER_MONITOR
    r = ensure_process_dpi_awareness(PER_MONITOR)
    assert r.ok is False
    assert r.actual == 1
    assert r.requested == 2
    assert "E_ACCESSDENIED" in r.detail


def test_ensure_other_hresult(fake_shcore):
    """Unrelated non-zero HRESULT -> ok=False, detail records the HRESULT."""
    fake_shcore.set_hr = 0x80070057  # E_INVALIDARG
    fake_shcore.get_value = 1        # readback doesn't match target
    from agent_windows.core.dpi import ensure_process_dpi_awareness, PER_MONITOR
    r = ensure_process_dpi_awareness(PER_MONITOR)
    assert r.ok is False
    assert "HRESULT=0x80070057" in r.detail


def test_ensure_ctypes_exception(monkeypatch):
    """SetProcessDpiAwareness attribute access raising (API missing on old
    Windows) -> ok=False, detail records the ctypes error (not a crash)."""
    class _ExplodingShcore:
        def GetProcessDpiAwareness(self, *a):
            return -1
        def SetProcessDpiAwareness(self, target):
            raise OSError("procedure not found")
    monkeypatch.setattr(ctypes.windll, "shcore", _ExplodingShcore())
    from agent_windows.core.dpi import ensure_process_dpi_awareness, PER_MONITOR
    r = ensure_process_dpi_awareness(PER_MONITOR)
    assert r.ok is False
    assert "ctypes error" in r.detail
