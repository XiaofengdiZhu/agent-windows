"""Tests for core/uwp_activation.py: IApplicationActivationManager wrapper.

The COM invocation is exercised via a DI seam (``_create``) that returns a fake
manager, so these tests stay hermetic (no real CoCreate, no desktop mutation).
A separate opt-in e2e test below activates a real app.
"""

import pytest

from _ctypes import COMError

from agent_windows.core import uwp_activation as uwp


class _FakeMgr:
    """Stand-in for the COM activation manager; records args + writes pid."""

    def __init__(self, pid=4321, raise_exc=None):
        self._pid = pid
        self._exc = raise_exc
        self.captured = None

    def ActivateApplication(self, aumid, args, options, pid_ptr):
        self.captured = (aumid, args, options)
        if self._exc:
            raise self._exc
        # CPython internal: byref(ULONG)._obj is the underlying c_ulong.
        # Test-only; production code never touches this attribute.
        pid_ptr._obj.value = self._pid


class TestActivateUwpApp:
    def test_iid_constant_matches_activation_manager(self):
        # Hardcoded expected IID (from shobjidl_core.h MIDL_INTERFACE) so a
        # one-sided edit to the module constant is caught as a regression.
        expected = "{2E941141-7F97-4756-BA1D-9DECDE894A3D}"
        assert str(uwp.IApplicationActivationManager._iid_) == expected
        assert uwp.IID_IApplicationActivationManager == expected

    def test_activate_returns_pid_from_manager(self):
        fake = _FakeMgr(pid=4321)
        result = uwp.activate_uwp_app(
            "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App", "", _create=lambda: fake)
        assert result == 4321
        assert fake.captured[0] == "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"

    def test_activate_threads_args(self):
        fake = _FakeMgr()
        uwp.activate_uwp_app("appid!x", "foo bar", _create=lambda: fake)
        assert fake.captured[1] == "foo bar"

    def test_activate_passes_ao_none(self):
        fake = _FakeMgr()
        uwp.activate_uwp_app("appid!x", "", _create=lambda: fake)
        assert fake.captured[2] == 0  # AO_NONE

    def test_comerror_becomes_valueerror_with_hresult(self):
        # hresult 0x80073D10; only the message surface matters here.
        exc = COMError(0x80073D10, "missing", (None, None, 0, None, None))
        with pytest.raises(ValueError, match="0x80073d10"):
            uwp.activate_uwp_app("bad!appid", "", _create=lambda: _FakeMgr(raise_exc=exc))

    def test_cocreate_failure_becomes_valueerror(self):
        """A COMError from the factory (CoCreate) is also caught — not just
        errors from ActivateApplication.  Locks the mgr-inside-try fix."""
        exc = COMError(0x80040154, "Class not registered", (None, None, 0, None, None))

        def _boom():
            raise exc

        with pytest.raises(ValueError, match="0x80040154"):
            uwp.activate_uwp_app("appid!x", "", _create=_boom)


# ── opt-in e2e (mutates desktop) ─────────────────────────────────────

def _e2e_enabled() -> bool:
    import os
    return bool(os.environ.get("AGENT_WINDOWS_E2E"))


@pytest.mark.e2e
def test_real_calculator_activation_returns_pid():
    """Opt-in: activates real Calculator. Mutates the desktop — skip unless
    AGENT_WINDOWS_E2E=1 is set (mirrors test_e2e_daemon.py)."""
    import ctypes
    import comtypes

    if not _e2e_enabled():
        pytest.skip("Set AGENT_WINDOWS_E2E=1 to run UWP activation e2e")
    # No-arg CoInitializeEx → COINIT_MULTITHREADED (MTA).  The daemon uses
    # STA, but the out-of-proc activation server marshals across apartments,
    # so MTA is fine for this standalone e2e.
    comtypes.CoInitializeEx()
    try:
        pid = uwp.activate_uwp_app("Microsoft.WindowsCalculator_8wekyb3d8bbwe!App")
    finally:
        comtypes.CoUninitialize()
    assert pid > 0
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    assert handle, f"activation returned pid {pid} but OpenProcess failed"
    ctypes.windll.kernel32.CloseHandle(handle)
