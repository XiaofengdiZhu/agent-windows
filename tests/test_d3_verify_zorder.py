"""D3 e2e: selector-based verify reads the FindFirst element, not the visual
z-order topmost, when the target is occluded by another window.

Opt-in (mutates the real desktop). Skips unless ``AGENT_WINDOWS_E2E=1`` is set,
so the default ``pytest -q`` run stays side-effect-free. Run with::

    AGENT_WINDOWS_E2E=1 uv run pytest -m e2e tests/test_d3_verify_zorder.py -v -s

Contract (D3 acceptance gate):

  * Window A (Calculator) exposes an interactive element (a number button).
  * Window B (Notepad) is brought to the foreground and sized so its client
    area visually covers the button's center coordinate.
  * A selector click on the button fires (mouse via ``SendInput`` lands on B).
  * The verify ``Hit:`` line MUST name A's button (``FindFirst`` by RuntimeId,
    z-order-independent identity), NOT B's Edit (``ControlFromPoint``, visual
    z-order topmost — the bug D3 fixes).

The unit tests in Tasks 3-4 already prove the wiring (``_resolve_live_control``
→ ``FindFirst`` by RuntimeId; ``_append_hit_focus`` reads ``hit_element_of``
when ``control`` is given). This is the real-desktop acceptance gate that
proves the integrated behaviour under genuine occlusion.
"""
import ctypes
import os
import re
import subprocess
import time

import pytest

pytestmark = pytest.mark.e2e

CALC_AUMID = "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"

# Calculator's number-pad buttons are named "One".."Nine" on en-US builds,
# "1".."9" on other Latin locales, and "一".."九"/"零" on zh-CN/zh-TW. Accept
# all of them so the e2e finds a key across Windows installs (otherwise it
# would skip live on non-English hosts — observed on zh-CN where Calculator
# surfaces CJK digit names even in Scientific mode).
_NUMBER_NAMES = {
    "1", "2", "3", "4", "5", "6", "7", "8", "9", "0",
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "zero",
    "一", "二", "三", "四", "五", "六", "七", "八", "九", "零",
}

# Snapshot table Position cell: str(BoundingBox) = "(l,t,r,b)" (tree.py:149-150).
_BBOX_RE = re.compile(r"\((-?\d+),(-?\d+),(-?\d+),(-?\d+)\)")


def _enabled() -> bool:
    """Mirror test_uia_bypass_e2e.py / test_runtime_id_spike.py opt-in gate."""
    return bool(os.environ.get("AGENT_WINDOWS_E2E"))


@pytest.fixture(autouse=True)
def _com_sta_init():
    """COM-init STA on the test thread before any direct UIA call.

    The daemon runs all UIA/COM on a dedicated STA worker; this test process
    must match the apartment for ``activate_uwp_app`` (CoCreateInstance) and
    ``ControlFromPoint`` (the occlusion sanity check) to behave like production.
    Mirrors test_runtime_id_spike.py.
    """
    if not _enabled():
        yield
        return
    import comtypes

    comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
    try:
        yield
    finally:
        comtypes.CoUninitialize()


def _find_number_button(
        snapshot_output: str,
) -> tuple[str, str, tuple[int, int]] | None:
    """Scan the snapshot's Interactive Elements table for a Calculator number
    button. Returns ``(ref, name, center)`` or ``None``.

    Table row shape: ``| @eN | Button | click | <name> | Calculator | (l,t,r,b) | |``
    (see ``render_snapshot`` in tree.py:1969+). Center is bbox midpoint
    ((left+right)//2, (top+bottom)//2) — what ``resolve_element_center`` yields.
    """
    for line in snapshot_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith("| @e"):
            continue
        cols = [c.strip() for c in stripped.split("|")]
        # split("|") -> ["", ref, type, action, name, window, position, state, ""]
        if len(cols) < 7:
            continue
        ref, type_name, name, pos = cols[1], cols[2], cols[4], cols[6]
        if type_name != "Button" or name.lower() not in _NUMBER_NAMES:
            continue
        m = _BBOX_RE.search(pos)
        if not m:
            continue
        left, top, right, bottom = (int(v) for v in m.groups())
        return ref, name, ((left + right) // 2, (top + bottom) // 2)
    return None


def _window_text(hwnd: int) -> str:
    """Read a top-level window's title via Win32 (one GetWindowTextW)."""
    user32 = ctypes.windll.user32
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _enumerate_notepad_hwnds() -> set[int]:
    """Return the set of currently-visible top-level HWNDs whose title looks
    like a Notepad window (en-US "Untitled - Notepad", zh-CN "无标题 - Notepad",
    zh-TW "未命名 - Notepad"). Used to diff before/after a Popen so the test
    picks the Notepad IT launched, not stragglers from previous runs.

    Win11 note: ``notepad.exe`` is a launcher stub that forwards to a separate
    Notepad process, so the Popen PID does NOT match the actual Notepad process
    PID — HWND-by-title enumeration is the only reliable correlate.
    """
    return _enumerate_hwnds_by_title(("Notepad", "记事本"))


def _enumerate_calc_hwnds() -> set[int]:
    """Visible top-level Calculator HWNDs (en "Calculator", zh-CN "计算器",
    zh-TW "小算盤"). Used to diff before/after ``activate_uwp_app`` so the test
    closes only the Calculator IT activated — not stragglers. Critical: without
    this, every e2e run leaks a Calculator window (UWP suspends instead of
    exiting), which compounded into ~19 orphan windows during test development.
    """
    return _enumerate_hwnds_by_title(("Calculator", "计算器", "小算盤"))


def _enumerate_hwnds_by_title(matchers: tuple[str, ...]) -> set[int]:
    """Visible top-level HWNDs whose title contains any of ``matchers``."""
    user32 = ctypes.windll.user32
    found: set[int] = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title = _window_text(hwnd)
        if any(m in title for m in matchers):
            found.add(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return found


def _spawn_notepad() -> tuple[subprocess.Popen, int]:
    """Launch Notepad and return ``(popen, hwnd)``. The HWND is found by
    diffing Notepad-class windows before vs after the launch (Popen PID is
    unreliable on Win11). Returns ``(popen, 0)`` if no new HWND appeared.
    """
    before = _enumerate_notepad_hwnds()
    proc = subprocess.Popen(["notepad.exe"])
    # Wait up to 2.5s for the new Notepad window to appear (Win11 stub spawn
    # + UWP-style init can take >1s on a cold launch).
    deadline = time.time() + 2.5
    while time.time() < deadline:
        new = _enumerate_notepad_hwnds() - before
        if new:
            return proc, next(iter(new))
        time.sleep(0.1)
    return proc, 0


def _cover_and_raise(hwnd: int, cx: int, cy: int) -> bool:
    """Move+size *hwnd* so its client area covers ``(cx, cy)``, then make it
    a TOPMOST window so it sits above Calculator in z-order regardless of
    which app holds foreground. Returns True if ``SetWindowPos`` succeeded.

    Rationale: ``SetForegroundWindow`` is silently denied when the test process
    doesn't have foreground permission, and ``SetWindowPos(HWND_TOP)`` only
    promotes *hwnd* within its own Z band (won't lift it above the foreground
    window). ``HWND_TOPMOST`` bypasses both: the window is moved to the
    topmost band, above ALL non-topmost windows, so ``ControlFromPoint(cx, cy)``
    deterministically returns an element inside *hwnd*. Reverted in cleanup
    via ``_untop(hwnd)`` so the test never leaves a stray topmost window.
    """
    user32 = ctypes.windll.user32
    SWP_SHOWWINDOW = 0x0040
    w, h = 600, 400
    x = max(0, cx - w // 2)
    y = max(0, cy - h // 2)
    # HWND_TOPMOST as an HWND (-1) — pass via c_void_p to match the C type.
    return bool(user32.SetWindowPos(
        hwnd, ctypes.c_void_p(-1), x, y, w, h, SWP_SHOWWINDOW))


def _untop(hwnd: int) -> None:
    """Revert the HWND_TOPMOST side effect from ``_cover_and_raise`` so a
    leftover Notepad doesn't stay above every other window after the test.
    Best-effort; the close also handles this."""
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    user32.SetWindowPos(hwnd, ctypes.c_void_p(-2),  # HWND_NOTOPMOST
                        0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def _close_window(hwnd: int) -> None:
    """Best-effort graceful close (``PostMessage(WM_CLOSE)``). Idempotent."""
    if not hwnd:
        return
    WM_CLOSE = 0x0010
    ctypes.windll.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _kill_hwnd_process(hwnd: int) -> None:
    """Force-terminate the process owning *hwnd*. Win11 Notepad ignores
    ``WM_CLOSE`` (it suspends to the tray rather than exiting), so closing the
    occluder via ``_close_window`` would leak one Notepad window per run. Kill
    the owning process directly by PID. Idempotent + best-effort.

    Used only for the Notepad occluder we spawned — never for Calculator
    (UWP Calculator shares ``CalculatorApp.exe`` across instances; killing by
    PID there would nuke unrelated Calculator windows, and the graceful
    ``_close_window`` WM_CLOSE path works for UWP).
    """
    if not hwnd:
        return
    pid = ctypes.c_uint(0)
    if not ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        return
    if not pid.value:
        return
    try:
        subprocess.run(
            ["taskkill", "/pid", str(pid.value), "/f"],
            capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _terminate(handles: list[subprocess.Popen]) -> None:
    """Best-effort cleanup of every Notepad we launched (the launcher stub
    on Win11; the real Notepad process is closed via ``_close_window``)."""
    for proc in handles:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def test_selector_verify_names_occluded_target():
    """Selector click's ``Hit:`` line names the TARGET element (FindFirst by
    RuntimeId) even when another window visually occludes its coordinates.

    Staging: Calculator (A) is activated and a number button's bbox center is
    computed from the snapshot; Notepad (B) is launched, sized to cover that
    center, and raised to the foreground. The click then physically lands on B
    via ``SendInput``, but the verify ``Hit:`` must still name A's button —
    proving ``_resolve_live_control``/``hit_element_of`` routed through
    ``FindFirst`` (identity) rather than ``ControlFromPoint`` (visual topmost).

    Skips cleanly when staging is unavailable (Calculator won't activate, no
    number button found, Notepad won't launch or won't cover the coord). The
    skip is informative, not a silent placeholder — the contract is host-dependent.
    """
    if not _enabled():
        pytest.skip("Set AGENT_WINDOWS_E2E=1 to run the D3 z-order e2e")

    from agent_windows.core import uwp_activation as uwp
    from agent_windows.core.tree import foreground_window
    from agent_windows.ipc.client import call_daemon
    from agent_windows.uia.controls import ControlFromPoint

    launched: list[subprocess.Popen] = []
    occluder_hwnds: list[int] = []
    calc_before: set[int] = set()
    try:
        # 1. Activate Calculator (COM STA — fixture above). Snapshot the
        #    existing Calculator HWNDs first so cleanup closes only the one
        #    this run activates (UWP Calculator suspends rather than exiting,
        #    so without this diff every run would leak a Calculator window).
        calc_before = _enumerate_calc_hwnds()
        try:
            uwp.activate_uwp_app(CALC_AUMID)
        except Exception as e:
            pytest.skip(f"could not activate Calculator: {e!r}")
        time.sleep(1.0)

        # 2. Snapshot and locate a number button + its center.
        snap = call_daemon("snapshot", {})
        target = _find_number_button(snap.get("output", ""))
        if target is None:
            pytest.skip("no Calculator number button found in snapshot")
        ref, name, (cx, cy) = target

        # 3. Launch Notepad (B), move+size over (cx, cy), bring to front.
        try:
            proc, hwnd = _spawn_notepad()
            launched.append(proc)
        except Exception as e:
            pytest.skip(f"could not launch Notepad occluder: {e!r}")
        if not hwnd:
            pytest.skip("could not locate a new Notepad window HWND after launch")
        occluder_hwnds.append(hwnd)
        if not _cover_and_raise(hwnd, cx, cy):
            pytest.skip("could not position/raise Notepad occluder")
        time.sleep(0.8)

        # 4. Confirm occlusion is real (otherwise the test is vacuous):
        #    ControlFromPoint(cx, cy) must NOT name the Calculator button —
        #    proving B is the visual topmost at the click coord. With
        #    HWND_TOPMOST applied above, this is deterministic: B is above all
        #    non-topmost windows at (cx, cy), so ControlFromPoint hits B.
        fg_pre = foreground_window().get("title", "")
        try:
            visual_topmost = ControlFromPoint(cx, cy)
        except Exception as e:
            pytest.skip(
                f"ControlFromPoint({cx}, {cy}) raised {e!r}; cannot verify "
                f"occlusion is real")
        if visual_topmost is None:
            pytest.skip(
                f"ControlFromPoint({cx}, {cy}) returned None; cannot verify "
                f"occlusion is real (fg_pre={fg_pre!r})")
        try:
            topmost_name = (visual_topmost.Name or "").lower()
        except Exception:
            topmost_name = ""
        if topmost_name == name.lower():
            pytest.skip(
                f"occluder did not cover the button — ControlFromPoint still "
                f"names {topmost_name!r} at ({cx}, {cy}) (fg_pre={fg_pre!r})")

        # 5. Click the Calculator button via selector. SendInput lands on B's
        #    Edit at (cx, cy) (B is topmost), but the verify Hit must name A's
        #    button via FindFirst — this is the D3 contract under test.
        res = call_daemon("click", {"selector": ref})
        out = res.get("output", "")

        # 6. Parse Hit: + Focus: lines (format: `Hit: <Type> "<name>"`,
        #    `Focus: "<window title>"`).
        hit_line = ""
        focus_line = ""
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Hit:"):
                hit_line = s
            elif s.startswith("Focus:"):
                focus_line = s
        assert hit_line, (
            f"no Hit: line in click output (verify disabled?)\noutput:\n{out}")

        # 7. PASS condition: Hit names A's button (the digit/spelled-out
        #    number). The name is short (1 digit or <=5-char word), so a
        #    substring match on the lowercased line is unambiguous — Notepad's
        #    Edit has Name "" or "Text Editor"/"文本编辑器", never the digit.
        assert name.lower() in hit_line.lower(), (
            f"D3 REGRESSION: selector Hit did not name the occluded Calculator "
            f"button. Expected name {name!r} in Hit line {hit_line!r}; "
            f"visual_topmost_name={topmost_name!r}, Focus={focus_line!r}, "
            f"fg_pre={fg_pre!r}.\nClick output:\n{out}")
        # The occluder's Edit control type must NOT be what Hit names — that
        # would mean ControlFromPoint (the old bug) was used instead of
        # FindFirst.
        assert "Edit" not in hit_line, (
            f"D3 REGRESSION: Hit names the Notepad Edit (occluder) instead of "
            f"the Calculator button. Hit line: {hit_line!r}; "
            f"visual_topmost_name={topmost_name!r}, Focus={focus_line!r}, "
            f"fg_pre={fg_pre!r}.\nClick output:\n{out}")

        print(f"\nD3 PASS: occluded click Hit = {hit_line!r} "
              f"(button name={name!r}, visual_topmost={topmost_name!r}, "
              f"Focus={focus_line!r}, fg_pre={fg_pre!r})")
    finally:
        # Notepad occluder: revert HWND_TOPMOST first, then kill the owning
        # process. Win11 Notepad ignores WM_CLOSE (suspends to tray), so
        # _close_window alone would leak one Notepad per run. _kill_hwnd_process
        # terminates the specific process behind the HWND — safe here because
        # we spawned this Notepad; never used for Calculator.
        for h in occluder_hwnds:
            _untop(h)
            _kill_hwnd_process(h)
        _terminate(launched)
        # Calculator this run activated: close via HWND diff vs calc_before.
        # Graceful WM_CLOSE works for UWP Calculator; never kill (shared
        # CalculatorApp.exe across instances would nuke unrelated windows).
        for h in _enumerate_calc_hwnds() - calc_before:
            _close_window(h)
