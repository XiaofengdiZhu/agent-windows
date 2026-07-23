"""Tests for core/uwp_activation.py: IApplicationActivationManager wrapper.

The COM invocation is exercised via a DI seam (``_create``) that returns a fake
manager, so these tests stay hermetic (no real CoCreate, no desktop mutation).
A separate opt-in e2e test below activates a real app.
"""

import pytest

from comtypes import GUID
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
        pid_ptr._obj.value = self._pid  # ctypes.byref(ULONG) → _obj is the ULONG


class TestActivateUwpApp:
    def test_iid_constant_matches_activation_manager(self):
        from agent_windows.core.uwp_activation import IID_IApplicationActivationManager
        assert uwp.IApplicationActivationManager._iid_ == GUID(IID_IApplicationActivationManager)

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
