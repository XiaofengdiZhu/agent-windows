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
        # The fake holds a single ancestor, so both branches return it; the
        # ``condition`` check is decorative, kept only to mirror the real walker.
        if self._ancestor_window is not None and condition(self._ancestor_window, -1):
            return self._ancestor_window
        return self._ancestor_window


class _FakeWindowControl(_FakeControl):
    def __init__(self, native_window_handle):
        super().__init__({}, control_type_name="WindowControl",
                         native_window_handle=native_window_handle)


def _comerror():
    return COMError(0x80004005, "fail", (None, None, 0, None, None))


@pytest.fixture
def no_real_enable_window(monkeypatch):
    """Stub the real Win32 ``EnableWindow`` for invoke_background tests.

    Bogus HWNDs (555/321/888/0) can collide with live recycled windows on the
    test host and disable them mid-run, so no invoke_background test may reach
    the real ``user32.EnableWindow``. This returns prior state = enabled, making
    ``run_with_uia_bypass``'s restore a harmless no-op. Tests that assert
    disable/restore ordering (``test_invoke_background_disables_host_around_pattern``)
    patch ``_enable_window`` themselves and opt out of this fixture.
    """
    monkeypatch.setattr(bypass, "_enable_window", lambda hwnd, en: 1)


def test_invoke_via_pattern_prefers_invoke(monkeypatch, no_real_enable_window):
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


def test_invoke_via_pattern_falls_through_to_toggle(monkeypatch, no_real_enable_window):
    from agent_windows.uia.enums import PatternId

    toggle = _FakePattern("toggle")
    ctrl = _FakeControl({PatternId.TogglePattern: toggle},
                        ancestor_window=_FakeWindowControl(555))
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    res = bypass.invoke_background(10, 20, selector="@e1")

    assert res["action"] == "toggle"
    assert toggle.called


def test_invoke_via_pattern_falls_through_to_select(monkeypatch, no_real_enable_window):
    from agent_windows.uia.enums import PatternId

    select = _FakePattern("select")
    ctrl = _FakeControl({PatternId.SelectionItemPattern: select},
                        ancestor_window=_FakeWindowControl(555))
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    res = bypass.invoke_background(10, 20, selector="@e1")

    assert res == {"action": "select", "host_hwnd": 555}
    assert select.called


def test_invoke_via_pattern_raises_when_no_pattern(monkeypatch, no_real_enable_window):
    ctrl = _FakeControl({}, ancestor_window=_FakeWindowControl(555))
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    with pytest.raises(bypass.BackgroundUnavailableError, match="no Invoke"):
        bypass.invoke_background(10, 20, selector="@e1")


def test_invoke_via_pattern_wraps_comerror(monkeypatch, no_real_enable_window):
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


def test_host_hwnd_no_ancestor_raises(monkeypatch, no_real_enable_window):
    ctrl = _FakeControl({})  # ancestor_window=None
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    with pytest.raises(bypass.BackgroundUnavailableError, match="window"):
        bypass.invoke_background(10, 20, selector="@e1")


def test_host_hwnd_zero_raises(monkeypatch, no_real_enable_window):
    # Ancestor is a WindowControl but reports NativeWindowHandle == 0.
    ctrl = _FakeControl({}, ancestor_window=_FakeWindowControl(0))
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: ctrl)

    with pytest.raises(bypass.BackgroundUnavailableError, match="HWND"):
        bypass.invoke_background(10, 20, selector="@e1")


def test_invoke_background_uses_controlfrompoint_when_no_selector(monkeypatch,
                                                                   no_real_enable_window):
    from agent_windows.uia.enums import PatternId

    invoke = _FakePattern("invoke")
    ctrl = _FakeControl({PatternId.InvokePattern: invoke},
                        ancestor_window=_FakeWindowControl(321))
    monkeypatch.setattr("agent_windows.uia.controls.ControlFromPoint",
                        lambda x, y: ctrl)

    res = bypass.invoke_background(30, 40, selector=None)

    assert res == {"action": "invoke", "host_hwnd": 321}


def test_invoke_background_controlfrompoint_none_raises(monkeypatch, no_real_enable_window):
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


# --- click handler --background branch (Task 4) -------------------------------
# The brief's handler called _coords unconditionally; for a selector that forces
# a snapshot-cache read (resolve_element_center) which (a) raises in-unit (no
# snapshot loaded) and (b) could spuriously fail a background click in prod via
# CoordinateRangeError on a stale echo-center. Pattern delivery ignores pixel
# coords, so echo-coord validation must never gate it. The corrected handler
# wraps _coords in try/except (best-effort echo) on the background path.
from agent_windows.daemon.handlers import input as input_handlers


def test_click_background_right_raises():
    # right + background has no Invoke semantics → BackgroundUnavailableError,
    # raised BEFORE _coords/invoke_background run — so no resolve patch or
    # EnableWindow isolation fixture is needed (nothing reaches EnableWindow).
    with pytest.raises(bypass.BackgroundUnavailableError):
        input_handlers._click({"x": 1, "y": 2, "right": True, "background": True})


def test_click_background_skips_sendinput(monkeypatch, no_real_enable_window):
    from agent_windows.uia.enums import PatternId

    sent = {"clicks": []}
    monkeypatch.setattr("agent_windows.core.input_controller.click",
                        lambda *a, **k: sent["clicks"].append((a, k)))
    # Drive _coords to raise deterministically. The real _coords("@e1") would
    # call resolve_element_center → get_tree_snapshot → traverse the LIVE UIA
    # tree (no snapshot is loaded in-unit), which on the test host triggers a
    # native COM fatal exception (0x80040155 SEH) — a host-dependent live-
    # desktop read. The corrected handler's contract is that _coords is
    # best-effort on the background path, so simulate its failure directly and
    # assert the degrade-to-(0,0)+deliver path instead of touching the desktop.
    def _coords_no_snapshot(_params):
        raise RuntimeError("no snapshot loaded for @e1")
    monkeypatch.setattr(input_handlers, "_coords", _coords_no_snapshot)
    # invoke_background does a function-body `from ...tree import
    # _resolve_live_control`, which shadows a bypass.-level patch — retarget
    # to the SOURCE module (same discipline as the invoke_background tests).
    monkeypatch.setattr("agent_windows.core.tree._resolve_live_control",
                        lambda sel: _FakeControl(
                            {PatternId.InvokePattern: _FakePattern("invoke")},
                            ancestor_window=_FakeWindowControl(123)))
    # No live-desktop reads in a unit test: kill the post-op Hit/Focus verify
    # lines (the Focus-stays-put assertion belongs to the Task 6 e2e, not here).
    monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: False)

    res = input_handlers._click({"selector": "@e1", "background": True})

    assert sent["clicks"] == []                       # SendInput never called
    assert "Delivery: background (invoke)" in res["output"]


def test_click_background_bad_selector_bubbles(monkeypatch):
    """A bad selector (ValueError/IndexError from resolve_element_center) is NOT
    swallowed by the background best-effort except — it bubbles (→ INVALID_PARAMS
    via route_request) instead of silently degrading to (0,0). Only
    CoordinateRangeError and RuntimeError degrade."""
    def _bad_selector(_params):
        raise ValueError("bad selector format")
    monkeypatch.setattr(input_handlers, "_coords", _bad_selector)

    def _fail_invoke(*a, **k):
        raise AssertionError("delivery ran on a bad selector")
    monkeypatch.setattr("agent_windows.core.uia_bypass.invoke_background",
                        _fail_invoke)

    with pytest.raises(ValueError, match="bad selector"):
        input_handlers._click({"selector": "@bad", "background": True})


def test_click_background_no_selector_bad_coord_bubbles(monkeypatch):
    """No selector → ControlFromPoint(x, y) targets, so coords are authoritative:
    a non-numeric x (CoordinateRangeError) must surface (→ -1002), NOT degrade
    to a silent misclick at (0,0). (v1's broad except left the bad value in place
    and crashed visibly at ControlFromPoint; this surfaces a clean error.)"""
    from agent_windows.daemon.router import CoordinateRangeError

    def _bad_coord(_params):
        raise CoordinateRangeError("coordinate 'x' must be a number, got str")
    monkeypatch.setattr(input_handlers, "_coords", _bad_coord)

    def _fail_invoke(*a, **k):
        raise AssertionError("delivery ran on a bad no-selector coord")
    monkeypatch.setattr("agent_windows.core.uia_bypass.invoke_background",
                        _fail_invoke)

    with pytest.raises(CoordinateRangeError, match="must be a number"):
        input_handlers._click({"x": "invalid", "y": 5, "background": True})


def test_click_background_selector_stale_echo_degrades(monkeypatch):
    """With a selector, coords are echo/verify-only (Invoke targets the resolved
    element), so a stale/off-screen echo center (CoordinateRangeError) degrades
    best-effort — delivery still fires, with the echo coerced to int so a bad
    value never reaches the Position: line."""
    from agent_windows.daemon.router import CoordinateRangeError

    def _stale_echo(_params):
        raise CoordinateRangeError("element center is off-screen")
    monkeypatch.setattr(input_handlers, "_coords", _stale_echo)

    delivered = {}
    def _cap_invoke(x, y, selector):
        delivered["x"], delivered["y"], delivered["sel"] = x, y, selector
        return {"action": "invoke", "host_hwnd": 1}
    monkeypatch.setattr("agent_windows.core.uia_bypass.invoke_background",
                        _cap_invoke)
    monkeypatch.setattr(input_handlers, "_verify_enabled", lambda: False)

    res = input_handlers._click({"selector": "@e1", "x": "stale", "y": None,
                                 "background": True})
    assert delivered["sel"] == "@e1"                   # selector drives targeting
    assert delivered["x"] == 0 and delivered["y"] == 0  # bad echo coerced to int
    assert "Delivery: background (invoke)" in res["output"]


def test_click_default_unchanged(monkeypatch):
    sent = {"clicks": []}
    monkeypatch.setattr("agent_windows.core.input_controller.click",
                        lambda *a, **k: sent["clicks"].append((a, k)))
    monkeypatch.setattr("agent_windows.core.tree.hit_element_at", lambda x, y: None)
    monkeypatch.setattr("agent_windows.core.tree.foreground_window",
                        lambda: {"hwnd": 0, "title": ""})

    res = input_handlers._click({"x": 5, "y": 6})

    assert sent["clicks"] == [((5, 6), {"right": False, "double": False})]
    assert "Delivery" not in res["output"]


# --- run_pattern_background helper (Task 1) ------------------------------------

def test_run_pattern_background_wraps_action(monkeypatch, no_real_enable_window):
    """run_pattern_background runs action inside run_with_uia_bypass with strict
    interleaving: EnableWindow(FALSE) -> action -> restore. One events list
    (not two independent asserts) so a hypothetical restore-then-action or
    action-before-disable ordering would fail. Mirrors the lower-level
    test_run_with_uia_bypass_disables_then_restores."""
    events = []
    monkeypatch.setattr(bypass, "_enable_window",
                        lambda hwnd, en: events.append(("enable", hwnd, en)) or 1)
    ctrl = _FakeControl({}, ancestor_window=_FakeWindowControl(777))

    def _action():
        events.append(("action",))

    bypass.run_pattern_background(ctrl, _action)

    assert events == [("enable", 777, False), ("action",),
                      ("enable", 777, True)]


def test_run_pattern_background_host_fail_raises_no_action(monkeypatch):
    """Host-HWND failure raises BackgroundUnavailableError and action NEVER runs
    — no silent foreground degrade (D2, consistent with click --background)."""
    # ancestor_window=None -> _host_hwnd_of -> GetAncestorControl returns None -> raise
    ctrl = _FakeControl({}, ancestor_window=None)
    fired = {"action": False}

    with pytest.raises(bypass.BackgroundUnavailableError):
        bypass.run_pattern_background(ctrl, lambda: fired.__setitem__("action", True))

    assert fired["action"] is False
