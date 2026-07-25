"""Tests for core/uipi.py — UIPI integrity-level preflight.

Mocks layer-1 ctypes reads (never touches a real token). Host-independent.
"""
import pytest


# ---- check_* logic (Group A: monkeypatch the IL read functions) ------------

@pytest.fixture(autouse=True)
def _reset_own_cache():
    """Clear the own-IL module cache between tests so monkeypatching sticks."""
    from agent_windows.core import uipi as uipi_mod
    uipi_mod._reset_own_il_cache_for_tests()
    yield
    uipi_mod._reset_own_il_cache_for_tests()


def test_check_blocked_when_target_higher(monkeypatch):
    """target High (0x3000) > own Medium (0x2000) -> blocked=True."""
    from agent_windows.core import uipi as uipi_mod
    monkeypatch.setattr(uipi_mod, "own_integrity_level", lambda: 0x2000)
    monkeypatch.setattr(uipi_mod, "integrity_level_at", lambda x, y: 0x3000)
    r = uipi_mod.check_uipi_at(10, 20)
    assert r.blocked is True
    assert r.own_il == 0x2000
    assert r.target_il == 0x3000


def test_check_ok_when_equal(monkeypatch):
    """target Medium == own Medium -> blocked=False."""
    from agent_windows.core import uipi as uipi_mod
    monkeypatch.setattr(uipi_mod, "own_integrity_level", lambda: 0x2000)
    monkeypatch.setattr(uipi_mod, "integrity_level_at", lambda x, y: 0x2000)
    r = uipi_mod.check_uipi_at(5, 5)
    assert r.blocked is False
    assert r.target_il == 0x2000


def test_check_ok_when_lower(monkeypatch):
    """target Low < own Medium -> blocked=False."""
    from agent_windows.core import uipi as uipi_mod
    monkeypatch.setattr(uipi_mod, "own_integrity_level", lambda: 0x2000)
    monkeypatch.setattr(uipi_mod, "integrity_level_foreground", lambda: 0x1000)
    r = uipi_mod.check_uipi_foreground()
    assert r.blocked is False
    assert r.target_il == 0x1000


def test_check_degrades_when_own_unknown(monkeypatch):
    """own IL None (read failed) -> blocked=False, even if target is High."""
    from agent_windows.core import uipi as uipi_mod
    monkeypatch.setattr(uipi_mod, "own_integrity_level", lambda: None)
    monkeypatch.setattr(uipi_mod, "integrity_level_at", lambda x, y: 0x3000)
    r = uipi_mod.check_uipi_at(1, 1)
    assert r.blocked is False
    assert r.own_il is None


def test_check_degrades_when_target_unknown(monkeypatch):
    """target IL None (read failed) -> blocked=False."""
    from agent_windows.core import uipi as uipi_mod
    monkeypatch.setattr(uipi_mod, "own_integrity_level", lambda: 0x2000)
    monkeypatch.setattr(uipi_mod, "integrity_level_foreground", lambda: None)
    r = uipi_mod.check_uipi_foreground()
    assert r.blocked is False
    assert r.target_il is None


# ---- ctypes chain + own cache (Group B) ------------------------------------

class _Func:
    """Callable stand-in for a ctypes function (carries .argtypes/.restype).

    A plain bound method cannot take .argtypes setattr (core/dpi.py lesson).
    """
    def __init__(self, ret_fn):
        self._ret_fn = ret_fn
        self.argtypes = None
        self.restype = None

    def __call__(self, *args):
        return self._ret_fn(args)


class _FakeWin:
    """Fake ctypes.windll with user32/advapi32/kernel32 scripted for IL read."""
    def __init__(self, hwnd=1, pid=1234, token_succeed=True, rid=0x2000):
        self.hwnd = hwnd
        self.pid = pid
        self.token_succeed = token_succeed
        self.rid = rid
        self.closed = []

        user32 = type("U", (), {})()
        def _wfp(args):
            return self.hwnd  # WindowFromPoint(POINT) -> hwnd
        def _gftpi(args):
            args[1][0] = self.pid  # GetWindowThreadProcessId(hwnd, &pid)
            return 1  # thread id
        def _gfw(args):
            return self.hwnd  # GetForegroundWindow -> hwnd
        user32.WindowFromPoint = _Func(_wfp)
        user32.GetWindowThreadProcessId = _Func(_gftpi)
        user32.GetForegroundWindow = _Func(_gfw)

        advapi32 = type("A", (), {})()
        def _opt(args):  # OpenProcessToken(handle, access, &token)
            args[2][0] = 9999 if self.token_succeed else 0
            return 1 if self.token_succeed else 0
        def _gti(args):  # GetTokenInformation(token, class, buf, len, &got)
            # Only handle the TokenIntegrityLevel(25) second call (buf non-None).
            if args[1] != 25:
                args[4][0] = 8  # buflen for first probe
                return 0
            args[4][0] = 8
            # Write a fake TOKEN_MANDATORY_LABEL: first 8 bytes = Sid pointer,
            # next 4 = Attributes. Sid pointer is opaque; we route through a
            # patched GetSidSubAuthority instead, so the buffer content does
            # not matter — we only need the call to succeed.
            return 1
        def _gsa(args):  # GetSidSubAuthority(sid, idx) -> &rid
            # Return a pointer-like whose [0] is the RID. Use a real c_uint
            # array so [0] indexing works.
            import ctypes
            arr = (ctypes.c_uint * 1)(self.rid)
            return arr
        advapi32.OpenProcessToken = _Func(_opt)
        advapi32.GetTokenInformation = _Func(_gti)
        advapi32.GetSidSubAuthority = _Func(_gsa)

        kernel32 = type("K", (), {})()
        kernel32.GetCurrentProcess = _Func(lambda args: -1)  # pseudo handle
        def _openproc(args):  # OpenProcess(access, inherit, pid) -> handle
            return 4242  # nonzero
        def _close(args):  # CloseHandle(handle) -> bool
            self.closed.append(args[0])
            return 1
        kernel32.OpenProcess = _Func(_openproc)
        kernel32.CloseHandle = _Func(_close)

        self.user32 = user32
        self.advapi32 = advapi32
        self.kernel32 = kernel32


@pytest.fixture
def fake_windll(monkeypatch):
    """Inject _FakeWin as ctypes.windll; returns it for per-test tweaks."""
    import ctypes
    fake = _FakeWin()
    # ctypes.windll exposes .user32/.advapi32/.kernel32 as dynamic attrs;
    # monkeypatch the windll instance to hand them out.
    class _Proxy:
        def __getattr__(self, name):
            return {"user32": fake.user32,
                    "advapi32": fake.advapi32,
                    "kernel32": fake.kernel32}[name]
    monkeypatch.setattr(ctypes, "windll", _Proxy())
    return fake


def test_own_reads_medium(fake_windll):
    """own_integrity_level reads the daemon token IL via the ctypes chain."""
    from agent_windows.core import uipi as uipi_mod
    fake_windll.rid = 0x2000
    assert uipi_mod.own_integrity_level() == 0x2000


def test_at_reads_target(fake_windll):
    """integrity_level_at walks WindowFromPoint->pid->token->IL.

    own-IL coverage is in test_own_reads_medium; this test uses a different
    rid so the two reads do not collide on the single fake_windll.rid value.
    """
    from agent_windows.core import uipi as uipi_mod
    fake_windll.rid = 0x3000
    assert uipi_mod.integrity_level_at(7, 8) == 0x3000


def test_own_il_cached_after_first_read(fake_windll):
    """own_integrity_level caches: a second call does not re-invoke the chain."""
    from agent_windows.core import uipi as uipi_mod
    fake_windll.rid = 0x2000
    first = uipi_mod.own_integrity_level()
    # Mutate the fake's rid; a cached second call must still return the first value.
    fake_windll.rid = 0x3000
    second = uipi_mod.own_integrity_level()
    assert first == 0x2000
    assert second == 0x2000  # cached, not re-read


def test_at_degrades_on_null_hwnd(fake_windll):
    """WindowFromPoint returns 0 -> integrity_level_at returns None."""
    from agent_windows.core import uipi as uipi_mod
    fake_windll.hwnd = 0
    assert uipi_mod.integrity_level_at(0, 0) is None


def test_at_degrades_on_token_denied(fake_windll):
    """OpenProcessToken returns 0 -> integrity_level_at returns None."""
    from agent_windows.core import uipi as uipi_mod
    fake_windll.token_succeed = False
    assert uipi_mod.integrity_level_at(3, 4) is None


def test_own_degrades_on_api_missing(monkeypatch):
    """advapi32 missing (old Windows / API absent) -> OSError -> None."""
    import ctypes
    class _NoAdvapi:
        def __getattr__(self, name):
            raise AttributeError(name)
    class _Proxy:
        def __getattr__(self, name):
            if name == "advapi32":
                raise AttributeError(name)
            return _NoAdvapi()
    monkeypatch.setattr(ctypes, "windll", _Proxy())
    from agent_windows.core import uipi as uipi_mod
    assert uipi_mod.own_integrity_level() is None


# ---- daemon _setup_own_il wiring (Group C) --------------------------------

def test_setup_own_il_logs_info_on_read(caplog, monkeypatch):
    """_setup_own_il logs INFO with the own IL name when the read succeeds."""
    import logging
    from agent_windows.core import uipi as uipi_mod
    from agent_windows.daemon import server as server_mod

    uipi_mod._reset_own_il_cache_for_tests()
    monkeypatch.setattr(uipi_mod, "_read_own_il_uncached", lambda: 0x2000)
    monkeypatch.setattr(uipi_mod, "_OWN_IL", uipi_mod._UNSET, raising=False)

    with caplog.at_level(logging.DEBUG, logger="agent_windows.daemon"):
        server_mod._setup_own_il(logging.getLogger("agent_windows.daemon"))
    assert any("own integrity" in r.message and "medium" in r.message
               for r in caplog.records)
    assert all(r.levelno < logging.WARNING for r in caplog.records)


def test_setup_own_il_logs_warning_on_none(caplog, monkeypatch):
    """_setup_own_il logs WARNING when the own-IL read fails (None)."""
    import logging
    from agent_windows.core import uipi as uipi_mod
    from agent_windows.daemon import server as server_mod

    uipi_mod._reset_own_il_cache_for_tests()
    monkeypatch.setattr(uipi_mod, "_read_own_il_uncached", lambda: None)
    monkeypatch.setattr(uipi_mod, "_OWN_IL", uipi_mod._UNSET, raising=False)

    with caplog.at_level(logging.DEBUG, logger="agent_windows.daemon"):
        server_mod._setup_own_il(logging.getLogger("agent_windows.daemon"))
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warned
    assert any("own integrity" in r.message for r in warned)


# ---- handler wiring (Group D) ----------------------------------------------

def _patch_uipi_blocked(monkeypatch, at_blocked=True, fg_blocked=True):
    """Monkeypatch check_uipi_at/foreground to scripted UipiResult values."""
    from agent_windows.core import uipi as uipi_mod

    class _R:
        def __init__(self, blocked):
            self.blocked = blocked
            self.own_il = 0x2000
            self.target_il = 0x3000 if blocked else 0x2000
            self.detail = "test"
    monkeypatch.setattr(uipi_mod, "check_uipi_at",
                        lambda x, y: _R(at_blocked))
    monkeypatch.setattr(uipi_mod, "check_uipi_foreground", lambda: _R(fg_blocked))


def test_click_foreground_raises_on_uipi_block(monkeypatch):
    """click foreground raises PermissionError whose hint mentions background/elevated."""
    import pytest
    _patch_uipi_blocked(monkeypatch, at_blocked=True)
    from agent_windows.daemon.handlers import input as input_mod
    with pytest.raises(PermissionError) as ei:
        input_mod._click({"x": 10, "y": 20})  # no background, no selector
    msg = str(ei.value)
    assert "UIPI" in msg
    assert "background" in msg
    assert "elevated" in msg


def test_type_focused_raises_on_uipi_block(monkeypatch):
    """type plain (focused-field) branch raises on UIPI block."""
    import pytest
    _patch_uipi_blocked(monkeypatch, fg_blocked=True)
    from agent_windows.daemon.handlers import input as input_mod
    with pytest.raises(PermissionError):
        input_mod._type({"text": "hi"})  # no at_x/at_y/at_selector -> focused


def test_click_background_skips_check(monkeypatch):
    """click --background does NOT call check_uipi_at (Invoke bypasses UIPI)."""
    from agent_windows.core import uipi as uipi_mod
    called = {"at": 0}
    monkeypatch.setattr(uipi_mod, "check_uipi_at",
                        lambda x, y: called.__setitem__("at", called["at"] + 1) or type(
                            "R", (), {"blocked": True, "own_il": 0x2000,
                                      "target_il": 0x3000, "detail": ""})())
    # invoke_background will fail (no UIA in unit test) — we only assert the
    # preflight was NOT consulted on the background branch. Wrap in try.
    from agent_windows.daemon.handlers import input as input_mod
    try:
        input_mod._click({"x": 5, "y": 5, "background": True})
    except Exception:
        pass  # invoke_background/ControlFromPoint will raise under unit test
    assert called["at"] == 0


def test_click_degrade_does_not_raise(monkeypatch):
    """blocked=False -> click proceeds to SendInput (no PermissionError)."""
    _patch_uipi_blocked(monkeypatch, at_blocked=False)
    sent = {"n": 0}

    def _fake_click(x, y, right=False, double=False):
        sent["n"] += 1
    import agent_windows.core.input_controller as ic
    monkeypatch.setattr(ic, "click", _fake_click)
    from agent_windows.daemon.handlers import input as input_mod
    # Should not raise.
    result = input_mod._click({"x": 1, "y": 2})
    assert sent["n"] == 1
    assert "Click" in result["output"]
