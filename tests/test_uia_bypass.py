"""Unit tests for core/uia_bypass.py — no live desktop required."""

import pytest

import agent_windows.core.uia_bypass as bypass


def test_run_with_uia_bypass_disables_then_restores(monkeypatch):
    calls = []

    def fake_enable_window(hwnd, enable):
        calls.append((hwnd, enable))
        return 1  # previous state: was enabled

    monkeypatch.setattr(bypass, "_enable_window", fake_enable_window)

    result = bypass.run_with_uia_bypass(4242, lambda: calls.append(("action",)) or "done")

    assert result == "done"
    assert calls == [(4242, False), ("action",), (4242, True)]


def test_run_with_uia_bypass_restores_even_when_action_raises(monkeypatch):
    calls = []

    def fake_enable_window(hwnd, enable):
        calls.append(enable)
        return 1

    monkeypatch.setattr(bypass, "_enable_window", fake_enable_window)

    def boom():
        raise RuntimeError("pattern blew up")

    with pytest.raises(RuntimeError):
        bypass.run_with_uia_bypass(99, boom)

    assert calls == [False, True]  # finally still restored


def test_run_with_uia_bypass_restores_prior_disabled_state(monkeypatch):
    # Host was already disabled (prev=0): restore to disabled, not forced True.
    calls = []

    def fake_enable_window(hwnd, enable):
        calls.append(enable)
        return 0  # previous state: was disabled

    monkeypatch.setattr(bypass, "_enable_window", fake_enable_window)

    bypass.run_with_uia_bypass(7, lambda: None)

    assert calls == [False, False]


from agent_windows.daemon.router import handler, route_request
from agent_windows.ipc.protocol import JSONRPCRequest, BACKGROUND_UNAVAILABLE


def test_route_request_maps_background_unavailable():
    @handler("__test_bg_unavail__")
    def _raises(_params):
        raise bypass.BackgroundUnavailableError("no invoke pattern")

    req = JSONRPCRequest(id=1, method="__test_bg_unavail__", params={})
    resp = route_request(req)

    assert resp.error is not None
    assert resp.error["code"] == BACKGROUND_UNAVAILABLE
    assert "no invoke pattern" in resp.error["message"]


# NOTE: invoke_background does function-body lazy imports of
# ``_resolve_live_control`` and ``ControlFromPoint``. A function-body
# ``from M import name`` re-reads the source-module attribute at call time, so
# monkeypatching ``bypass.<name>`` would be shadowed by the local. We therefore
# patch the SOURCE modules below (``agent_windows.core.tree._resolve_live_control``
# and ``agent_windows.uia.controls.ControlFromPoint``). ``_enable_window`` is a
# true module-global referenced bare inside ``run_with_uia_bypass``, so it stays
# patched on ``bypass``.
from _ctypes import COMError


class _FakePattern:
    def __init__(self, name):
        self.name = name
        self.called = False

    def Invoke(self):
        self.called = True

    def Toggle(self):
        self.called = True

    def Select(self):
        self.called = True


class _FakeControl:
    """Minimal stand-in for uia.controls.Control."""

    def __init__(self, patterns, control_type_name="ButtonControl",
                 native_window_handle=0, ancestor_window=None):
        self._patterns = patterns          # {PatternId.int: _FakePattern|None}
        self.ControlTypeName = control_type_name
        self.NativeWindowHandle = native_window_handle
        self._ancestor_window = ancestor_window

    def GetPattern(self, pattern_id):
        return self._patterns.get(pattern_id)

    def GetAncestorControl(self, condition):
        # condition(control, depth) -> bool; the real walker looks for WindowControl.
        if self._ancestor_window is not None and condition(self._ancestor_window, -1):
            return self._ancestor_window
        return self._ancestor_window


class _FakeWindowControl(_FakeControl):
    def __init__(self, native_window_handle):
        super().__init__({}, control_type_name="WindowControl",
                         native_window_handle=native_window_handle)


def _comerror():
    return COMError(0x80004005, "fail", (None, None, 0, None, None))


def test_invoke_via_pattern_prefers_invoke(monkeypatch):
    from agent_windows.uia.enums import PatternId

    invoke = _FakePattern("invoke")
    toggle = _FakePattern("toggle")
    ctrl = _FakeControl({PatternId.InvokePattern: invoke,
                         PatternId.TogglePattern: toggle},
                        ancestor_window=_FakeWindowControl(555))

    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    res = bypass.invoke_background(10, 20, selector="@e1")

    assert res == {"action": "invoke", "host_hwnd": 555}
    assert invoke.called and not toggle.called


def test_invoke_via_pattern_falls_through_to_toggle(monkeypatch):
    from agent_windows.uia.enums import PatternId

    toggle = _FakePattern("toggle")
    ctrl = _FakeControl({PatternId.TogglePattern: toggle},
                        ancestor_window=_FakeWindowControl(555))
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    res = bypass.invoke_background(10, 20, selector="@e1")

    assert res["action"] == "toggle"
    assert toggle.called


def test_invoke_via_pattern_raises_when_no_pattern(monkeypatch):
    ctrl = _FakeControl({}, ancestor_window=_FakeWindowControl(555))
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    with pytest.raises(bypass.BackgroundUnavailableError, match="no Invoke"):
        bypass.invoke_background(10, 20, selector="@e1")


def test_invoke_via_pattern_wraps_comerror(monkeypatch):
    from agent_windows.uia.enums import PatternId

    class _BoomInvoke:
        def Invoke(self):
            raise _comerror()

    ctrl = _FakeControl({PatternId.InvokePattern: _BoomInvoke()},
                        ancestor_window=_FakeWindowControl(555))
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    with pytest.raises(bypass.BackgroundUnavailableError):
        bypass.invoke_background(10, 20, selector="@e1")


def test_host_hwnd_missing_raises(monkeypatch):
    ctrl = _FakeControl({})  # ancestor_window=None
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    with pytest.raises(bypass.BackgroundUnavailableError, match="window"):
        bypass.invoke_background(10, 20, selector="@e1")


def test_invoke_background_uses_controlfrompoint_when_no_selector(monkeypatch):
    from agent_windows.uia.enums import PatternId

    invoke = _FakePattern("invoke")
    ctrl = _FakeControl({PatternId.InvokePattern: invoke},
                        ancestor_window=_FakeWindowControl(321))
    monkeypatch.setattr("agent_windows.uia.controls.ControlFromPoint",
                        lambda x, y: ctrl)

    res = bypass.invoke_background(30, 40, selector=None)

    assert res == {"action": "invoke", "host_hwnd": 321}


def test_invoke_background_controlfrompoint_none_raises(monkeypatch):
    monkeypatch.setattr("agent_windows.uia.controls.ControlFromPoint",
                        lambda x, y: None)

    with pytest.raises(bypass.BackgroundUnavailableError):
        bypass.invoke_background(30, 40, selector=None)


def test_invoke_background_disables_host_around_pattern(monkeypatch):
    from agent_windows.uia.enums import PatternId

    enables = []
    monkeypatch.setattr(bypass, "_enable_window",
                        lambda hwnd, en: enables.append((hwnd, en)) or 1)
    invoke = _FakePattern("invoke")
    ctrl = _FakeControl({PatternId.InvokePattern: invoke},
                        ancestor_window=_FakeWindowControl(888))
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    bypass.invoke_background(1, 2, selector="@e1")

    assert enables == [(888, False), (888, True)]
