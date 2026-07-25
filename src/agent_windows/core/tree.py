"""UIA accessibility tree traversal.

Walks the Windows UIA tree, classifies elements, and renders semantic text
output for AI agent consumption.

Simplified from Windows-MCP's tree service — no browser DOM/IA2 handling yet.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from _ctypes import COMError

from agent_windows.utils.win32 import get_process_name
from agent_windows.utils.debug import is_debug
from agent_windows.uia.enums import PropertyId, TreeScope
from agent_windows.uia.exceptions import from_com_error, UIARetryableError

logger = logging.getLogger(__name__)

# Window-class name of the Windows desktop background window (explorer.exe).
# Its subtree is the desktop icon list — low value for automation and pure noise,
# so we show it in the window table but skip traversing it. The class name is
# locale-invariant (unlike the window title, which localizes to e.g. "桌面").
_DESKTOP_WINDOW_CLASS = "Progman"

# Max retry attempts for building a window's UIA root when the target process is
# busy (RPC_E_SERVERCALL_RETRYLATER etc.). Mirrors Windows-MCP's THREAD_MAX_RETRIES.
# Only retryable COM errors are retried — dead-element / disconnected errors are
# terminal (the window is gone) and won't benefit from another attempt.
_WINDOW_MAX_RETRIES = 3


def _should_skip_subtree(window: WindowInfo) -> bool:
    """Whether to skip deep-traversing a window's subtree (the window itself is
    still listed). Currently skips the desktop (Progman) icon list — low value
    for automation and pure noise. Kept as a predicate so it is unit-testable
    without driving the full COM traversal (PERF3-06)."""
    return window.class_name == _DESKTOP_WINDOW_CLASS


def _promote_modal_elements(elements: list[ElementInfo]) -> None:
    """Stable-sort ``elements`` in place so modal-dialog elements come first.

    The agent should see a blocking dialog before the window behind it. Done on
    the list itself (not just at render time) so the @eN refs render_snapshot
    emits match resolve_element's index-based lookup (PERF3-06). Stable sort
    preserves within-group traversal order. Extracted as a named function so the
    promotion logic is unit-testable directly rather than only via traverse_tree.
    """
    elements.sort(key=lambda el: 0 if el.is_modal else 1)

# ── Tree cache ────────────────────────────────────────────────────

_tree_cache: dict[str, Any] = {}
_tree_cache_ts: float = 0.0
_tree_cache_lock = threading.Lock()
_TREE_CACHE_TTL = 2.0  # seconds


def get_tree_snapshot(ttl: float = _TREE_CACHE_TTL, **kwargs) -> dict[str, Any]:
    """Get a UIA tree snapshot, with caching.

    The lock spans the cache check, traversal, and update so a concurrent miss
    can never trigger a redundant traversal or leave the cache half-updated
    (M-09 TOCTOU). ``traverse_tree`` is a UIA walk that runs on the daemon's
    single COM STA worker thread, so the lock is otherwise uncontended —
    holding it across the (bounded, ≤ ``timeout`` seconds) traversal costs
    nothing today, and if the daemon ever gains a second consumer thread this
    serializes them rather than double-traversing.
    """
    global _tree_cache, _tree_cache_ts
    with _tree_cache_lock:
        if _tree_cache and time.time() - _tree_cache_ts < ttl:
            return _tree_cache
        data = traverse_tree(**kwargs)
        _tree_cache = data
        _tree_cache_ts = time.time()
    return data


def prime_tree_cache(data: dict[str, Any]) -> None:
    """Replace the 2s tree cache with ``data``, timestamped now.

    Used by snapshot-diff so a freshly captured CURRENT tree becomes the cache
    that ``resolve_element('@eN')`` / ``click @eN`` read next — keeping the
    report's refs valid (decision D: baseline bypasses the cache, current
    primes it). Mirrors the write side of ``get_tree_snapshot`` under the same
    lock.
    """
    global _tree_cache, _tree_cache_ts
    with _tree_cache_lock:
        _tree_cache = data
        _tree_cache_ts = time.time()


# ── Control type classification ───────────────────────────────────

INTERACTIVE_TYPES = {
    "ButtonControl", "ListItemControl", "MenuItemControl", "EditControl",
    "CheckBoxControl", "RadioButtonControl", "ComboBoxControl",
    "HyperlinkControl", "SplitButtonControl", "TabItemControl",
    "TreeItemControl", "DataItemControl", "HeaderItemControl",
    "TextBoxControl", "SliderControl", "SpinnerControl", "ScrollBarControl",
    "DocumentControl",
}
# Structural container types — pure layout/grouping nodes (no action of their
# own). Used by _traverse_node's empty-container pruning (TODO #6): a structural
# node with NO useful descendants AND no name is dropped from the rendered tree
# (it carries zero information for the agent). A named one is kept (it may label
# a region even when empty). Mirrors the reference's
# STRUCTURAL_CONTROL_TYPE_NAMES (tree/config.py:60). (TODO #6)
_STRUCTURAL_TYPES = {
    "PaneControl", "GroupControl", "CustomControl",
    "ToolBarControl", "TabControl", "MenuBarControl",
}

# ── Data models ───────────────────────────────────────────────────


@dataclass
class BoundingBox:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)

    def __str__(self) -> str:
        return f"({self.left},{self.top},{self.right},{self.bottom})"


@dataclass
class ElementInfo:
    """Information about a UI element."""
    name: str
    control_type: str
    bbox: BoundingBox | None
    is_interactive: bool = False
    is_focused: bool = False
    is_enabled: bool = True
    is_offscreen: bool = False
    # True for elements inside a modal dialog subtree. render_snapshot promotes
    # these to the front of the interactive-elements list so the agent sees the
    # blocking dialog first, but does NOT drop other windows' elements.
    is_modal: bool = False
    # Title of the top-level WindowInfo this element belongs to. Filled during
    # traversal from the owning window's ``title`` (no extra COM cost — the
    # window is already an _traverse_node parameter). Enables WaitFor conditions
    # like element_exists + window_name to scope a match to a specific window.
    # ``str`` (not Optional) with a "" default so existing constructors and the
    # make_element test fixture stay backward-compatible without changes.
    window_name: str = ""
    # HWND of the top-level WindowInfo this element belongs to (0 if unset).
    # Filled during traversal from ``window.handle`` at zero extra COM cost
    # (the window is already an _traverse_node parameter). Gives snapshot-diff a
    # STABLE per-element window anchor: unlike window_name (which mirrors the
    # title and shifts on load/tab/state), the handle is stable for a window's
    # lifetime. ``int`` with a 0 default so existing constructors/fixtures stay
    # backward-compatible.
    window_handle: int = 0
    # UIA RuntimeId of the element (tuple of int). Stable for the element's
    # lifetime (survives move/redraw/occlusion); invalid only once the element
    # is destroyed. Cached during traversal (zero extra COM cost) so selector
    # commands can re-resolve the live control via FindFirst by RuntimeId
    # instead of ControlFromPoint (which reads the visual z-order topmost, not
    # the intended element — the bug D3 fixes). Empty () when the provider
    # didn't expose one → _resolve_live_control falls back to the coordinate
    # path (D3 D2). ``tuple`` (immutable, hashable) with an empty default so
    # existing constructors/fixtures stay backward-compatible.
    runtime_id: tuple[int, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowInfo:
    """Information about a top-level window."""
    title: str
    process_name: str
    handle: int
    bbox: BoundingBox | None
    is_active: bool = False
    # Win32 window class name (e.g. "Progman" for the desktop). Used to skip
    # traversing low-value subtrees without relying on the locale-dependent title.
    class_name: str = ""
    # Window placement state: "Maximized", "Minimized", or "Normal". Computed in
    # enumerate_windows via IsIconic/IsZoomed. (Invisible windows are filtered
    # before appending, so a fourth "Hidden" state never reaches here.) Useful to
    # the agent because a minimized window's bbox is an icon rect, not a usable
    # click target — surfacing this avoids clicking into empty space (TODO #2).
    status: str = "Normal"
    # True when the owning process is a known web browser (chrome/msedge/firefox).
    # Hint for the agent: browser windows need keyboard shortcuts / @eN refs from
    # the tree rather than coordinate guesses on the rendered page area. Derived
    # from process_name (already fetched) at zero extra cost (TODO #2).
    is_browser: bool = False


# ── Window enumeration ────────────────────────────────────────────

# Process image names (lowercased, with .exe) of known web browsers. Used to flag
# WindowInfo.is_browser — a hint that the window is a browser, so the agent prefers
# keyboard shortcuts / tree refs over coordinate guesses on page content. Matches
# the reference's Browser enum (chrome/msedge/firefox).
_BROWSER_PROCESS_NAMES = frozenset({"chrome.exe", "msedge.exe", "firefox.exe"})


def _window_status_from_placement(is_iconic: bool, is_zoomed: bool) -> str:
    """Map Win32 placement flags to a status string. Extracted from
    enumerate_windows so the logic is unit-testable without driving the real
    EnumWindows callback (which needs live ctypes structs). ``is_iconic`` wins
    over ``is_zoomed`` to match the reference's if/elif order — in practice the
    flags are mutually exclusive (TODO #2)."""
    if is_iconic:
        return "Minimized"
    if is_zoomed:
        return "Maximized"
    return "Normal"


def _is_browser_process(process_name: str) -> bool:
    """True if ``process_name`` is a known browser image (case-insensitive).
    ``process_name`` comes from QueryFullProcessImageNameW's basename, so it
    includes the ``.exe`` suffix (TODO #2)."""
    return process_name.lower() in _BROWSER_PROCESS_NAMES


def _pick_uwp_app_pid(
    child_pids: list[int], frame_host_pid: int,
) -> int | None:
    """First child pid that differs from a UWP frame host's pid.

    A ``ApplicationFrameWindow`` is owned by ``ApplicationFrameHost.exe``;
    the hosted app renders into a child window owned by the app's real
    process. The first child whose pid is not the frame host's belongs to
    the app. Pure (no ctypes) so the selection rule is unit-testable —
    mirrors ``_window_status_from_placement``'s testability. (TODO P2#6 6a)

    Args:
        child_pids: Owning pids of the frame's child windows (any order).
        frame_host_pid: The frame window's own owning pid.

    Returns:
        The first child pid != ``frame_host_pid``, else ``None`` when every
        child shares the host pid or the list is empty (app not yet rendered
        into the frame — caller degrades).
    """
    for pid in child_pids:
        # `pid and …` — belt-suspenders against a hypothetical zero pid sneaking
        # in (GetWindowThreadProcessId failure on a child writes back 0). Skipping
        # it here keeps the "never a bogus pid 0" invariant without relying on the
        # caller to filter. The non-equality check still does the real work.
        if pid and pid != frame_host_pid:
            return pid
    return None


def _resolve_uwp_app_pid(
    frame_hwnd: int, *, _user32=None,
) -> int | None:
    """Resolve the hosted app's pid for a UWP ``ApplicationFrameWindow``.

    Walks the frame's children via ``EnumChildWindows`` and returns the
    first child pid that differs from the frame host's pid (delegates the
    selection to ``_pick_uwp_app_pid``). Explicit ``.argtypes``/``.restype``
    on every ctypes call — ``ctypes.windll`` does not check HRESULT/BOOL
    without it (the ``core/uipi.py`` / ``core/dpi.py`` lesson). (TODO P2#6 6a)

    ``_user32`` is a DI seam defaulting to ``ctypes.windll.user32`` so the
    EnumChildWindows walk is testable without live ctypes (mirrors
    ``uwp_activation.activate_uwp_app``'s ``_create`` seam).

    Args:
        frame_hwnd: Top-level ``ApplicationFrameWindow`` handle.
        _user32: user32 binding (tests inject a fake).

    Returns:
        Hosted app pid, or ``None`` on any ctypes failure or when no
        non-host child exists (caller degrades to ``ApplicationFrameHost``).
    """
    import ctypes
    import ctypes.wintypes

    user32 = _user32 if _user32 is not None else ctypes.windll.user32
    try:
        WNDENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.wintypes.HWND,
            ctypes.wintypes.LPARAM,
        )
        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
        user32.EnumChildWindows.argtypes = [
            ctypes.wintypes.HWND, WNDENUMPROC, ctypes.wintypes.LPARAM]
        user32.EnumChildWindows.restype = ctypes.c_bool

        frame_host_pid = ctypes.wintypes.DWORD()
        # GetWindowThreadProcessId returns the thread id (0 on failure); mirror
        # ``core/uipi.py``'s ``_pid_of_hwnd`` and bail before reading .value.
        if not user32.GetWindowThreadProcessId(
            frame_hwnd, ctypes.byref(frame_host_pid)
        ):
            return None

        child_pids: list[int] = []

        def _child_cb(child_hwnd, _lparam):
            cpid = ctypes.wintypes.DWORD()
            if not user32.GetWindowThreadProcessId(
                child_hwnd, ctypes.byref(cpid)
            ):
                return True  # pid read failed; skip, keep enumerating
            child_pids.append(cpid.value)
            return True  # enumerate all children

        user32.EnumChildWindows(frame_hwnd, WNDENUMPROC(_child_cb), 0)
        return _pick_uwp_app_pid(child_pids, frame_host_pid.value)
    except (OSError, AttributeError, ValueError):
        return None


def _normalize_process_name(
    class_name: str, hwnd: int, original_name: str,
) -> str:
    """Normalize process_name for a UWP ``ApplicationFrameWindow``.

    A frame window's owning process is ``ApplicationFrameHost.exe``; the
    hosted app's real image is reached via ``_resolve_uwp_app_pid``. Only
    ``ApplicationFrameWindow`` triggers resolution — every other class
    passes ``original_name`` through unchanged. On unresolved pid (app not
    yet rendered into the frame) OR on a resolved pid whose
    ``get_process_name`` read returns ``""`` (process exited
    mid-enumeration, OpenProcess denied, etc.), the frame host name is
    kept — per the spec's degrade contract, ``process_name`` is always
    ``ApplicationFrameHost.exe`` for a frame window, never ``""``.
    (TODO P2#6 6a)

    Args:
        class_name: Window class (already captured in ``_enum_cb``).
        hwnd: Top-level window handle.
        original_name: process_name from ``GetWindowThreadProcessId`` +
            ``get_process_name`` (the frame host image for UWP frames).

    Returns:
        The hosted app's image name when resolvable, else
        ``original_name``.
    """
    if class_name != "ApplicationFrameWindow":
        return original_name
    app_pid = _resolve_uwp_app_pid(hwnd)
    if app_pid is None:
        return original_name
    # get_process_name is total (never raises) but returns "" on failure (process
    # exited mid-enumeration, OpenProcess denied, etc.). Per the spec's degrade
    # contract, an empty result must NOT surface — keep the frame host name so
    # every downstream consumer still sees ApplicationFrameHost.exe, never "".
    name = get_process_name(app_pid)
    return name or original_name


def enumerate_windows() -> list[WindowInfo]:
    """List all visible top-level windows."""
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    foreground = user32.GetForegroundWindow()
    windows: list[WindowInfo] = []

    # Callback for EnumWindows
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )

    def _enum_cb(hwnd, _lparam):
        # Skip invisible windows
        if not user32.IsWindowVisible(hwnd):
            return True

        # Get window title
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        # Get process name
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_name = get_process_name(pid.value)

        # Get window rect
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        bbox = BoundingBox(rect.left, rect.top, rect.right, rect.bottom)

        # Skip zero-size windows
        if bbox.area == 0:
            return True

        # Get window class name (locale-invariant, e.g. "Progman" for desktop).
        # Used to skip low-value subtrees; cheaper to fetch here once than to read
        # via UIA ClassNameProperty per-element during traversal.
        cls_buf = ctypes.create_unicode_buffer(257)
        cls_len = user32.GetClassNameW(hwnd, cls_buf, 257)
        class_name = cls_buf.value if cls_len else ""

        # UWP frame windows report ApplicationFrameHost.exe as the owning
        # process; resolve the hosted app's real image so downstream
        # consumers (snapshot/list/switch) see the app, not the frame host.
        # Degrades to original_name when the app is not yet rendered. (6a)
        process_name = _normalize_process_name(class_name, hwnd, process_name)

        # Window placement. IsIconic == minimized (still "visible" to
        # IsWindowVisible, but shown as an icon — its bbox is the icon rect, not a
        # usable client area). IsZoomed == maximized. Otherwise normal. Done with
        # the same user32 handle already in scope; two cheap bool calls per window.
        # Logic lives in _window_status_from_placement so it's unit-testable sans
        # ctypes (TODO #2).
        status = _window_status_from_placement(
            bool(user32.IsIconic(hwnd)), bool(user32.IsZoomed(hwnd)))

        windows.append(WindowInfo(
            title=title,
            process_name=process_name,
            handle=hwnd,
            bbox=bbox,
            is_active=(hwnd == foreground),
            class_name=class_name,
            status=status,
            is_browser=_is_browser_process(process_name),
        ))
        return True

    user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    return windows


def resolve_window(
    value: str | None = None,
    active: bool = False,
) -> tuple[WindowInfo, str | None]:
    """Resolve a top-level window for ``snapshot --window`` / ``--active``.

    Returns ``(WindowInfo, warning_text_or_None)``.

    - ``active=True``: pick the foreground window via the ``is_active`` flag set
      during :func:`enumerate_windows` (``hwnd == GetForegroundWindow()``).
      Raises ``ValueError`` if no enumerated window is active (desktop / not
      collected). No extra Win32 call.
    - ``value`` parses as an integer (``int(value, 0)`` — accepts ``"12345"`` and
      ``"0x1a2b"``) → matched against ``WindowInfo.handle``.
    - otherwise: title substring (case-insensitive); zero hits → process-name
      fallback (``.exe``-normalized substring on both sides); zero hits →
      ``ValueError("Window not found")`` (mirrors ``switch_window``).

    On multiple matches: returns the first and a ``warning`` naming the count,
    the chosen title, and a "pass the HWND for determinism" hint.

    Args:
        value: Window title substring, process-name substring, or HWND (decimal
            or ``0x``-hex). ``None`` only when ``active=True`` (the handler
            enforces mutual exclusion).
        active: If True, return the foreground window and ignore ``value``.

    Returns:
        ``(WindowInfo, warning)`` where ``warning`` is ``None`` for a unique
        match, else a human-readable string describing the ambiguity.

    Raises:
        ValueError: zero match, or ``active`` with no foreground window. These
            surface as INVALID_PARAMS (-32602) via ``route_request``.
            Also when ``active`` is False and ``value`` is empty/None.
    """
    if not active and not value:
        raise ValueError("value is required when active is False")
    windows = enumerate_windows()

    if active:
        win = next((w for w in windows if w.is_active), None)
        if win is None:
            raise ValueError("No active foreground window")
        return win, None

    # 1) Integer → HWND (accepts decimal "12345" and 0x-prefixed hex "0x1a2b").
    try:
        hwnd = int(value, 0)
    except (ValueError, TypeError):
        hwnd = None
    if hwnd is not None:
        for w in windows:
            if w.handle == hwnd:
                return w, None
        raise ValueError(f"Window not found: HWND {hwnd}")

    # 2) Title substring (case-insensitive).
    needle = value.lower()
    title_hits = [w for w in windows if needle in (w.title or "").lower()]
    if title_hits:
        chosen = title_hits[0]
        warning = None
        if len(title_hits) > 1:
            warning = (
                f'⚠ Matched {len(title_hits)} windows by title "{value}"; '
                f'using first: "{chosen.title}". Pass the HWND for determinism.'
            )
        return chosen, warning

    # 3) Process-name fallback (.exe-normalized substring on both sides so the
    #    match is robust to whether process_name carries the suffix).
    proc_needle = needle.removesuffix(".exe")
    proc_hits = [
        w for w in windows
        if proc_needle in (w.process_name or "").lower().removesuffix(".exe")
    ]
    if proc_hits:
        chosen = proc_hits[0]
        warning = None
        if len(proc_hits) > 1:
            warning = (
                f'⚠ Matched {len(proc_hits)} windows by process "{value}"; '
                f'using first: "{chosen.title}". Pass the HWND for determinism.'
            )
        return chosen, warning

    raise ValueError(f"Window not found: {value!r}")


# ── UIA Tree Traversal ────────────────────────────────────────────


def traverse_tree(
    max_depth: int = 15,
    include_invisible: bool = False,
    include_offscreen: bool = False,
    timeout_per_window: float = 5.0,
    max_elements: int = 200,
    timeout: float = 10.0,
    only_window: WindowInfo | None = None,
) -> dict[str, Any]:
    """Walk the UIA tree and return structured data.

    Bounds (H-04): ``max_depth`` limits descent, ``max_elements`` caps the
    interactive-elements list (traversal stops once it is reached), and
    ``timeout`` is a global wall-clock budget across all windows. Set any
    bound to 0 to disable it.

    ``only_window`` (snapshot --window / --active): when set, skip the full
    desktop ``enumerate_windows()`` call and traverse ONLY the given window's
    subtree. ``data["windows"]`` will be ``[only_window]`` and
    ``data["active_window"]`` will be ``only_window`` itself (single-window
    focus; whether it's the real foreground is expressed by --active at the
    handler layer). ``None`` preserves the existing whole-desktop behavior
    byte-for-byte (backward compatible).

    Returns a dict with:
        - cursor: (x, y) mouse position
        - resolution: (width, height) of the primary monitor
        - virtual_screen: (left, top, width, height) spanning all monitors
        - dpi_scale: system DPI as a multiplier of 96 (1.0 / 1.25 / 1.5 / …)
        - displays: count
        - active_window: WindowInfo
        - windows: list of WindowInfo
        - semantic_tree: string tree representation
        - interactive_elements: list of ElementInfo
    """
    from agent_windows.uia.core import _AutomationClient

    # Get system info
    import ctypes
    cursor_pos = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(cursor_pos))
    cursor = (cursor_pos.x, cursor_pos.y)

    SM_CXSCREEN = 0
    SM_CYSCREEN = 1
    primary_w = ctypes.windll.user32.GetSystemMetrics(SM_CXSCREEN)
    primary_h = ctypes.windll.user32.GetSystemMetrics(SM_CYSCREEN)
    monitor_count = ctypes.windll.user32.GetSystemMetrics(80)

    # Virtual screen + DPI scale (informational metadata; TODO #3).
    from agent_windows.core.screenshot import get_virtual_screen_rect, get_dpi_scale
    virtual_screen = get_virtual_screen_rect()
    dpi_scale = get_dpi_scale()

    # Enumerate windows. Timed under debug (TODO #3): window enumeration is a
    # series of Win32 calls (IsWindowVisible/GetWindowText/GetWindowRect/…) whose
    # cost scales with open window count, and is a common contributor to snapshot
    # latency. The perf_counter calls only run when is_debug(), so the production
    # hot path pays nothing.
    _profile = is_debug()
    _t_enumerate = time.perf_counter() if _profile else 0.0
    if only_window is None:
        all_windows = enumerate_windows()
        active_window = next((w for w in all_windows if w.is_active), None)
    else:
        # Single-window scope (snapshot --window / --active): skip the full
        # desktop enumerate and focus the traversal on the target window. The
        # target itself is the "active" window of the snapshot — whether it's
        # the real foreground is expressed by --active at the handler layer,
        # not by a separate GetForegroundWindow call here.
        all_windows = [only_window]
        active_window = only_window
    # enumerate_ms measures ONLY the Win32 enumeration + the trivial active-window
    # scan, so its label stays truthful. The COM-init / CacheRequest build below
    # is UIA-traversal setup and correctly falls under traverse_ms instead.
    _t_traverse = time.perf_counter() if _profile else 0.0

    # Interactive elements collected during traversal
    interactive_elements: list[ElementInfo] = []
    # Semantic tree nodes
    tree_lines: list[str] = []

    # Desktop header
    tree_lines.append("Desktop")

    client = _AutomationClient.instance()

    # Global wall-clock budget across all windows (0 disables). Computed once so
    # every node/window is checked against the same absolute deadline.
    global_deadline = (time.time() + timeout) if timeout > 0 else float("inf")

    # PERF3-04: the CacheRequest's property set is constant, so build it once
    # for the whole traversal instead of per-window (CreateCacheRequest + 6
    # AddProperty calls per window was pure redundant work).
    cache_request = _build_traversal_cache(client)

    for win in all_windows:
        if time.time() > global_deadline:
            logger.info(
                "Tree traversal stopped: global timeout %.1fs reached", timeout
            )
            break

        win_marker = " [active]" if win.is_active else ""
        tree_lines.append(f'  └─ Window "{win.title}" ({win.process_name}){win_marker}')

        # Skip the desktop window's subtree (desktop icon list) — low value and
        # pure noise. The window itself is still listed above so the agent knows
        # it exists; only the deep traversal is skipped (PERF3-06).
        if _should_skip_subtree(win):
            tree_lines.append("      [desktop icons — skipped]")
            continue

        try:
            _traverse_window(client, win, tree_lines, interactive_elements,
                           max_depth=max_depth, include_invisible=include_invisible,
                           include_offscreen=include_offscreen, indent=4,
                           timeout=timeout_per_window, max_elements=max_elements,
                           global_deadline=global_deadline, cache_request=cache_request)
        except Exception as e:
            logger.warning("Failed to traverse window '%s': %s", win.title, e)
            tree_lines.append(f"      [traversal error: {e}]")

    # Promote modal-dialog elements to the front (stable) so the agent sees the
    # blocking dialog first. Done on the list itself (not just at render time) so
    # @eN refs in render_snapshot match resolve_element's indices (PERF3-06).
    _promote_modal_elements(interactive_elements)

    # Snapshot profiling (TODO #3): emit per-stage timings only when debug is on.
    # Matches the reference's logger.info approach (WINDOWS_MCP_PROFILE_SNAPSHOT)
    # — timings go to the log stream, never into the response body, so no client
    # sees a schema change. enumerate_ms = Win32 window enumeration only;
    # traverse_ms = COM client init + CacheRequest build + the UIA tree walk (the
    # dominant cost, bounded by tree.max_depth/timeout/max_elements). render
    # happens in the caller, not here.
    if _profile:
        _t_now = time.perf_counter()
        enumerate_ms = (_t_traverse - _t_enumerate) * 1000.0
        traverse_ms = (_t_now - _t_traverse) * 1000.0
        logger.info(
            "snapshot profile: enumerate_ms=%.1f traverse_ms=%.1f windows=%d "
            "elements=%d max_depth=%d",
            enumerate_ms, traverse_ms, len(all_windows),
            len(interactive_elements), max_depth,
        )

    return {
        "cursor": cursor,
        "resolution": (primary_w, primary_h),
        "virtual_screen": virtual_screen,
        "dpi_scale": dpi_scale,
        "displays": monitor_count,
        "active_window": active_window,
        "windows": all_windows,
        "semantic_tree": "\n".join(tree_lines),
        "interactive_elements": interactive_elements,
    }


def resolve_element(selector: str) -> ElementInfo:
    """Resolve a UIA element selector to an ElementInfo.

    Args:
        selector: Element reference like "@e3" (1-based index into
            the interactive elements list from the rendered snapshot).

    Returns:
        ElementInfo for the matched element.

    Raises:
        ValueError: If selector format is invalid, or no tree snapshot is
            loaded (``@eN`` indexes a prior snapshot, so auto-traversing would
            mask the caller bug — run ``snapshot``/``observe`` first).
        IndexError: If element index is out of range.
    """
    if not selector.startswith("@e"):
        raise ValueError(f"Invalid selector: {selector!r} (expected @eN)")
    try:
        index = int(selector[2:]) - 1  # @e{N} is 1-based
    except ValueError:
        raise ValueError(f"Invalid selector: {selector!r} (expected @eN)")
    if index < 0:
        raise ValueError(f"Invalid selector: {selector!r} (index must be ≥ 1)")

    # Fail fast before any live UIA walk: an @eN ref indexes a PRIOR snapshot's
    # interactive-element list, so resolving one with no snapshot loaded is
    # meaningless — ``get_tree_snapshot`` would otherwise auto-traverse the live
    # UIA tree (a native COM fatal 0x80040155 on hosts without COM initialized
    # for the walk, and a wasted traversal even where it works) and silently
    # index into an unrelated fresh tree, masking the caller bug. Require an
    # explicit ``snapshot``/``observe`` first. (TODO #3)
    with _tree_cache_lock:
        snapshot_loaded = bool(_tree_cache)
    if not snapshot_loaded:
        raise ValueError(
            "no tree snapshot loaded — run `snapshot` or `observe` before using "
            "an @eN element selector")

    from agent_windows.utils.config import load_config
    cfg = load_config().get("tree", {})
    data = get_tree_snapshot(
        max_depth=cfg.get("max_depth", 15),
        include_invisible=cfg.get("include_invisible", False),
        include_offscreen=cfg.get("include_offscreen", False),
        max_elements=cfg.get("max_elements", 200),
        timeout=cfg.get("timeout", 10),
    )
    elements = data.get("interactive_elements", [])
    if index >= len(elements):
        raise IndexError(
            f"Element {selector} not found ({len(elements)} interactive elements)"
        )
    return elements[index]


def resolve_element_center(selector: str) -> tuple[int, int]:
    """Resolve a UIA element selector to the center of its bounding box. (TODO #13)

    Used by the input handlers (click/type/scroll/move/…) to accept an ``@eN``
    element ref in place of explicit ``x, y`` coordinates. The center is taken
    from the element's cached bbox (no extra COM cost) and re-validated against
    the virtual screen so a stale/moved element surfaces as
    COORDINATE_OUT_OF_RANGE rather than clicking off-target.

    Args:
        selector: Element reference like ``"@e3"``.

    Returns:
        ``(x, y)`` pixel coordinates of the bbox center.

    Raises:
        ValueError: If selector format is invalid or the element has no usable
            bbox (→ INVALID_PARAMS at the router).
        IndexError: If the element index is out of range (→ INVALID_PARAMS).
        CoordinateRangeError: If the (possibly stale) center is off-screen
            (→ COORDINATE_OUT_OF_RANGE).
    """
    el = resolve_element(selector)
    if not el.bbox or el.bbox.area <= 0:
        raise ValueError(f"Element {selector} has no usable bounding box")
    cx, cy = el.bbox.center
    # Re-validate: the snapshot is cached (up to 2s), so the element may have
    # moved or been closed since. validate_coordinates raises
    # CoordinateRangeError (→ COORDINATE_OUT_OF_RANGE) on a stale off-screen
    # center, which is more informative than a silent misclick.
    from agent_windows.core.input_controller import validate_coordinates
    return validate_coordinates(cx, cy)


def _resolve_live_via_point(selector: str):
    """Coordinate fallback for ``_resolve_live_control``.

    Resolves the cached bbox center and reverse-looks it up via
    ``ControlFromPoint`` (visual z-order topmost). Used only when the cached
    ``runtime_id`` is empty — the element is fine, the snapshot just didn't get
    an id (D3 D2 empty-fallback). Preserves the original COMError-wrapping +
    None→ValueError contract so callers (expand/collapse/invoke_background,
    TODO #6) keep seeing typed errors.

    Args:
        selector: Element reference like ``"@e3"``.

    Returns:
        A live ``Control`` at the element's bbox center.

    Raises:
        ValueError: If the element moved/vanished (ControlFromPoint returned
            None) — surfaces as INVALID_PARAMS at the router with an
            actionable "Re-snapshot and retry" hint — or propagated from
            ``resolve_element_center`` for a bad selector format.
        UIADeadElementError: If the target's process died / disconnected during
            the reverse-lookup — surfaces as ELEMENT_NOT_FOUND at the router
            (TODO #6 decision 2). ``ControlFromPoint`` calls the raw
            ``IUIAutomation.ElementFromPoint`` with no COM wrapping (unlike the
            vendored GetPattern/Expand, which swallow internally), so a dead
            target would otherwise leak a bare ``COMError`` as
            DAEMON_INTERNAL_ERROR.
        IndexError: Propagated from ``resolve_element_center`` for an
            out-of-range index.
        CoordinateRangeError: If the bbox center is off-screen.
    """
    from agent_windows.uia.controls import ControlFromPoint

    x, y = resolve_element_center(selector)
    try:
        control = ControlFromPoint(x, y)
    except COMError as e:
        # The only unwrapped live-COM call on this path. Map to a typed
        # UIAException so the router can classify it (dead → ELEMENT_NOT_FOUND,
        # retryable → the caller may retry) instead of leaking DAEMON_INTERNAL.
        raise from_com_error(e) from e
    if control is None:
        raise ValueError(
            f"Element {selector} not reachable at ({x}, {y}) — it may have "
            f"moved, been obscured, or closed. Re-snapshot and retry."
        )
    return control


def _resolve_live_control(selector: str):
    """Resolve an ``@eN`` selector to a LIVE UIA ``Control`` for Pattern calls.

    D3: prefer ``FindFirst`` by ``RuntimeId`` (z-order-independent identity
    lookup) over ``ControlFromPoint`` (visual z-order topmost). The snapshot
    caches each element's ``RuntimeId`` (Task 2); we scope the search to the
    owning window (``ElementInfo.window_handle`` → ``ControlFromHandle``) and
    ``FindFirst`` by ``RuntimeIdProperty``. Falls back to the coordinate path
    (``_resolve_live_via_point``) only when ``runtime_id`` is empty (the cache
    didn't get an id) — never on a miss (a miss means the element was
    destroyed; falling back would click the visual topmost = the bug D3 fixes).

    Args:
        selector: Element reference like ``"@e3"``.

    Returns:
        A live ``Control`` (from ``agent_windows.uia.controls``).

    Raises:
        ValueError: bad selector format / index (propagated from
            ``resolve_element``); FindFirst miss (element destroyed →
            "Re-snapshot and retry"); owning window closed
            (``ControlFromHandle`` None → "window ... closed since snapshot").
        UIADeadElementError: target process died during the lookup — either
            ``ControlFromHandle`` (owning window tearing down) or
            ``FindFirst``/``create_runtime_id_condition`` (provider disconnecting
            mid-call) raised COMError, mapped via ``from_com_error``. Also from
            the coordinate fallback path (TODO #6 decision 2).
        CoordinateRangeError: bbox center off-screen (coordinate fallback).
        IndexError: out-of-range @eN index (propagated from resolve_element).
    """
    from agent_windows.uia.controls import ControlFromHandle
    from agent_windows.uia.core import _AutomationClient, create_runtime_id_condition
    from agent_windows.uia.enums import TreeScope

    el = resolve_element(selector)
    rid = el.runtime_id
    if not rid:
        # No RuntimeId (provider didn't expose one) → identity lookup impossible.
        # The element is fine; fall back to the coordinate path. NOT a raise.
        return _resolve_live_via_point(selector)

    if el.window_handle:
        # ControlFromHandle hits the live UIA provider of the owning process;
        # if that process is tearing down mid-call it raises COMError (e.g.
        # RPC_E_DISCONNECTED) which must surface as a typed UIAException, not
        # DAEMON_INTERNAL_ERROR. Same wrap as FindFirst below.
        try:
            root = ControlFromHandle(el.window_handle)
        except COMError as e:
            raise from_com_error(e) from e
        if root is None:
            # Owning window closed since the snapshot — a stale @eN ref. Don't
            # fall back to coordinates (the whole window is gone); surface as a
            # re-snapshot hint so the caller refreshes.
            raise ValueError(
                f"Element {selector}: owning window (HWND {el.window_handle}) "
                f"closed since snapshot. Re-snapshot and retry."
            )
    else:
        # No owning window recorded (legacy/0 HWND) → search the whole desktop.
        root = _AutomationClient.instance().IUIAutomation.GetRootElement()

    # Both calls go through the live UIA provider: create_runtime_id_condition
    # builds a typed SAFEARRAY VARIANT via IUIAutomation.CreatePropertyCondition,
    # and FindFirst walks the tree. Wrap them together so either's COMError
    # (provider disconnecting mid-condition-build or mid-walk) surfaces typed.
    # typed SAFEARRAY(I4); see uia.core.create_runtime_id_condition (spike R2).
    try:
        cond = create_runtime_id_condition(rid)
        control = root.FindFirst(TreeScope.TreeScope_Subtree, cond)
    except COMError as e:
        raise from_com_error(e) from e
    if control is None:
        # Window alive but element gone (rebuilt/destroyed). Don't fall back to
        # coordinates: that would hit the visual topmost at the stale bbox —
        # the exact wrong-element bug D3 exists to fix. Surface a re-snapshot.
        raise ValueError(
            f"Element {selector} not found by RuntimeId — it may have moved, "
            f"been rebuilt, or closed. Re-snapshot and retry."
        )
    return control


# ── Post-op verify helpers (P0.1 / P0.3) ────────────────────────────
#
# Unlike _resolve_live_control (above), these are *decoration* on an already-
# completed input op: they must NEVER raise. Any COM/Win32 failure degrades to
# None / empty so a successful click/drag can't turn into an error from verify.


def _truncate_with_count(s: str, limit: int) -> str:
    """Truncate *s* to *limit* chars, suffixing ``...(+N)`` with the dropped
    count (P3.7 style) so a cut prefix isn't read as the whole label."""
    if len(s) <= limit:
        return s
    return s[:limit] + f"...(+{len(s) - limit})"


def _control_label(control) -> dict[str, Any] | None:
    """Best-effort compact ``{control_type, name}`` label for a LIVE UIA
    ``control`` (name truncated P3.7-style, ``ControlTypeName`` stripped of
    the ``Control`` suffix). Returns None on any read failure — shared by
    ``hit_element_at`` (coordinate path, ``ControlFromPoint``) and
    ``hit_element_of`` (selector path, FindFirst-resolved control) so the
    ``Hit:`` / ``Drop:`` line format is identical regardless of how the
    control was obtained. Never raises.
    """
    try:
        ctype = control.ControlTypeName.replace("Control", "")
        name = _truncate_with_count(control.Name or "", 40)
    except Exception:
        logger.debug("verify: control label read failed", exc_info=True)
        return None
    return {"control_type": ctype, "name": name}


def hit_element_at(x: int, y: int) -> dict[str, Any] | None:
    """Best-effort: the live UIA element under (x, y) as a compact
    ``{control_type, name}`` (name truncated P3.7-style). Returns None if the
    point is unreachable or the read fails — verify must never raise.

    Powers the ``Hit:`` line on click/hover/move/dblclick and the ``Drop:``
    line on drag (P0.1 / P0.3).
    """
    from agent_windows.uia.controls import ControlFromPoint
    try:
        control = ControlFromPoint(x, y)
    except Exception:
        logger.debug("verify: ControlFromPoint failed at (%s, %s)", x, y, exc_info=True)
        return None
    if control is None:
        return None
    return _control_label(control)


def hit_element_of(control) -> dict[str, Any] | None:
    """Best-effort label of a LIVE ``control`` as ``{control_type, name}``
    (name truncated P3.7-style). Mirror of ``hit_element_at`` that takes the
    control directly (e.g. a ``FindFirst`` result from
    ``_resolve_live_control``) instead of resolving it by coordinate — so
    post-op verify on the selector path reads the intended element, not the
    visual z-order topmost under the click point (D3 Task 4). Returns None
    when ``control`` is None or any read fails (verify must never raise).

    Powers the ``Hit:`` line on click/dblclick/hover/move and the ``Drop:``
    line on drag when the handler resolved the live control via a selector.
    """
    if control is None:
        return None
    return _control_label(control)


def range_percent_at(x: int, y: int) -> int | None:
    """Best-effort slider/scroll position at (x, y) as an int percent 0-100.

    Tries RangeValuePattern (value/min/max) then Scroll pattern (vertical then
    horizontal). Returns None if no pattern is exposed, the point is
    unreachable, or any read fails. Mirrors the slider + scroll blocks of
    _collect_rich_metadata (below) but on a live Control via GetPropertyValue
    instead of GetCachedPropertyValue. Powers the ``Slider:`` line on drag
    (P0.3). Native Win32/WPF sliders expose RangeValue on the thumb control;
    scrollbars / custom canvas sliders may not — best-effort, not guaranteed.
    """
    from agent_windows.uia.controls import ControlFromPoint
    try:
        control = ControlFromPoint(x, y)
    except Exception:
        return None
    if control is None:
        return None
    try:
        rv = control.GetPropertyValue(PropertyId.RangeValueValueProperty)
        rmin = control.GetPropertyValue(PropertyId.RangeValueMinimumProperty)
        rmax = control.GetPropertyValue(PropertyId.RangeValueMaximumProperty)
        if all(not _is_uia_sentinel(v) for v in (rv, rmin, rmax)) and rmax != rmin:
            return round((float(rv) - float(rmin)) / (float(rmax) - float(rmin)) * 100)
    except Exception:
        pass
    try:
        for pid in (PropertyId.ScrollVerticalScrollPercentProperty,
                    PropertyId.ScrollHorizontalScrollPercentProperty):
            pct = control.GetPropertyValue(pid)
            if pct is not None and not _is_uia_sentinel(pct):
                n = round(float(pct))
                if n >= 0:  # ScrollPattern.NoScrollValue == -1
                    return n
    except Exception:
        pass
    return None


def foreground_window() -> dict[str, Any]:
    """Best-effort current foreground window ``{hwnd, title}`` (title truncated
    P3.7-style). Win32 only (no COM), thread-safe. title is "" on any failure.
    Powers the ``Focus:`` line on click/hover/move/dblclick (P0.1).
    """
    from agent_windows.uia.core import GetForegroundWindow, GetWindowText
    try:
        hwnd = GetForegroundWindow()
        title = _truncate_with_count(GetWindowText(hwnd) or "", 40)
    except Exception:
        logger.debug("verify: foreground read failed", exc_info=True)
        return {"hwnd": 0, "title": ""}
    return {"hwnd": hwnd, "title": title}


def _read_field_value(control) -> dict[str, Any]:
    """Best-effort read of a live Control's text value + password flag (P0.2).

    Mirrors _collect_rich_metadata's value block: ValueValueProperty first,
    LegacyIAccessibleValueProperty fallback; IsPasswordProperty read independently
    of value (a password field may expose the flag but not the value). Returns
    ``{"value": str|None, "is_password": bool}`` — value is None when the control
    exposes no value pattern (unreadable) or the read fails. Never raises.
    """
    try:
        is_pw = control.GetPropertyValue(PropertyId.IsPasswordProperty)
        is_pw = bool(is_pw) and not _is_uia_sentinel(is_pw)
    except Exception:
        is_pw = False
    val = None
    try:
        raw = control.GetPropertyValue(PropertyId.ValueValueProperty)
        if raw is None or _is_uia_sentinel(raw):
            raw = control.GetPropertyValue(PropertyId.LegacyIAccessibleValueProperty)
            if raw is None or _is_uia_sentinel(raw):
                raw = None
        val = None if raw is None else str(raw)
    except Exception:
        logger.debug("verify: field value read failed", exc_info=True)
        val = None
    return {"value": val, "is_password": is_pw}


def focused_field_value() -> dict[str, Any] | None:
    """Best-effort focused element's text value + password flag (P0.2).

    Powers type's pre/post diff on the no-coords path (the field is already
    focused). Returns None when there's no focused element or the lookup fails.
    """
    from agent_windows.uia.controls import GetFocusedControl
    try:
        control = GetFocusedControl()
    except Exception:
        logger.debug("verify: GetFocusedControl failed", exc_info=True)
        return None
    if control is None:
        return None
    return _read_field_value(control)


def field_value_at(x: int, y: int) -> dict[str, Any] | None:
    """Best-effort text value + password flag of the element at (x, y) (P0.2).

    Powers type's pre/post diff on the --at / --at-selector path, where focus
    may not be on the target at pre-read time. ControlFromPoint resolves the
    target element directly by coordinate, so pre (before click/type) and post
    (after) are guaranteed the same field. Returns None on failure.
    """
    from agent_windows.uia.controls import ControlFromPoint
    try:
        control = ControlFromPoint(x, y)
    except Exception:
        logger.debug("verify: ControlFromPoint failed at (%s, %s)", x, y, exc_info=True)
        return None
    if control is None:
        return None
    return _read_field_value(control)


_VALUE_DIFF_INLINE_LIMIT = 60    # per side, single-line inline render
_VALUE_DIFF_LINE_LIMIT = 120     # per rendered line, multi-line unified
_VALUE_DIFF_TOTAL_LIMIT = 50000  # skip diff if either side huge (DoS guard)


def format_value_diff(pre: dict | None, post: dict | None) -> list[str]:
    """Render a git-diff-style output block for pre→post field value (P0.2).

    pre/post are focused_field_value/field_value_at dicts ({"value","is_password"})
    or None/empty. Returns the lines to append after the type echo (possibly []
    when there's nothing useful to show). Never raises. Cases:
      - password → single hidden line (no value leak);
      - both values None (no value pattern) → [] (nothing to report);
      - equal → "no change" (input rejected / maxlength / IME-swallowed);
      - single-line (no \\r/\\n) → "old → new" inline;
      - multi-line → difflib.unified_diff hunk.
    """
    pre = pre or {}
    post = post or {}
    is_pw = bool(post.get("is_password")) or bool(pre.get("is_password"))
    pv = pre.get("value")
    qv = post.get("value")
    if is_pw:
        return ["Field value: (password field, value hidden)"]
    if pv is None and qv is None:
        return []
    a = pv or ""
    b = qv or ""
    if len(a) > _VALUE_DIFF_TOTAL_LIMIT or len(b) > _VALUE_DIFF_TOTAL_LIMIT:
        return ["Field value: (value too large to diff)"]
    if a == b:
        return ["Field value: no change"]
    # Multi-line if EITHER side carries a line separator. Note \\r alone counts:
    # Win11 Notepad's RichEdit exposes Value with CR (not CRLF/LF) separators, so
    # a \\n-only check would mis-classify a multi-line document as single-line
    # and leak the raw \\r into the inline render (terminal renders CR as a
    # carriage return, scrambling the line). splitlines() below handles all of
    # \\r/\\n/\\r\\n uniformly.
    if not any(c in a or c in b for c in "\r\n"):
        return [f"Field value: {_truncate_with_count(a, _VALUE_DIFF_INLINE_LIMIT)} → "
                f"{_truncate_with_count(b, _VALUE_DIFF_INLINE_LIMIT)}"]
    import difflib
    diff = list(difflib.unified_diff(
        a.splitlines(), b.splitlines(),
        fromfile="before", tofile="after", lineterm=""))
    if not diff:
        return ["Field value: no change"]
    return ["Field value diff:"] + [
        _truncate_with_count(ln, _VALUE_DIFF_LINE_LIMIT) for ln in diff]


# ExpandCollapseExpandCollapseStateProperty int → human-readable state. Shared
# with _collect_rich_metadata; the value map is the single source of truth so
# expand_element/collapse_element report states consistent with the tree.
_EC_STATE_STR = {
    0: "collapsed",    # ExpandCollapseState.Collapsed
    1: "expanded",     # ExpandCollapseState.Expanded
    2: "partially expanded",  # ExpandCollapseState.PartiallyExpanded
    3: "leaf",         # ExpandCollapseState.LeafNode
}


def _read_expand_collapse_state(pattern) -> str | None:
    """Read the current ExpandCollapseState from a live pattern, or None."""
    try:
        return _EC_STATE_STR.get(int(pattern.ExpandCollapseState))
    except Exception:
        logger.debug("Failed to read ExpandCollapseState", exc_info=True)
        return None


def expand_element(selector: str) -> dict[str, Any]:
    """Expand an expandable UI element (ComboBox/TreeItem/Menu/Group). (TODO #12)

    Acquires a live ``Control`` via ``_resolve_live_control`` and drives its
    ``ExpandCollapsePattern``. Idempotent: if the element is already expanded,
    returns ``{"skipped": True}`` rather than erroring — the desired end state
    already holds.

    Args:
        selector: ``@eN`` element ref.

    Returns:
        ``{"action": "expanded"/"skipped", "state": <new state str>,
        "skipped": bool}``.

    Raises:
        ValueError: Element not reachable (→ INVALID_PARAMS) or does not
            support expand/collapse (→ INVALID_PARAMS).
        UIADeadElementError: Target vanished during the live-element lookup
            (→ ELEMENT_NOT_FOUND at the router). See _resolve_live_control.
    """
    from agent_windows.uia.enums import PatternId

    control = _resolve_live_control(selector)
    pattern = control.GetPattern(PatternId.ExpandCollapsePattern)
    if pattern is None:
        raise ValueError(
            f"Element {selector} does not support expand/collapse"
        )
    state = _read_expand_collapse_state(pattern)
    # Skip when the goal already holds (fully expanded) or can never hold
    # (leaf — no children to expand). Calling Expand() on a leaf is a wasted
    # ~0.5s sleep (OPERATION_WAIT_TIME) inside the pattern, so pre-check.
    # PartiallyExpanded falls through to Expand(): the agent asking to expand
    # a partially-expanded node (e.g. a 3-state tree) usually wants the
    # remaining children, so let the provider decide rather than hard-skip.
    if state == "expanded" or state == "leaf":
        return {"action": "skipped", "state": state, "skipped": True}
    from agent_windows.core.uia_bypass import run_pattern_background
    run_pattern_background(control, pattern.Expand)
    new_state = _read_expand_collapse_state(pattern) or "expanded"
    return {"action": "expanded", "state": new_state, "skipped": False}


def collapse_element(selector: str) -> dict[str, Any]:
    """Collapse an expandable UI element. (TODO #12)

    Mirror of ``expand_element``. Idempotent: an already-collapsed element
    returns ``{"skipped": True}``.

    Args:
        selector: ``@eN`` element ref.

    Returns:
        ``{"action": "collapsed"/"skipped", "state": <new state str>,
        "skipped": bool}``.

    Raises:
        ValueError: Element not reachable or does not support expand/collapse.
        UIADeadElementError: Target vanished during the live-element lookup
            (→ ELEMENT_NOT_FOUND at the router). See _resolve_live_control.
    """
    from agent_windows.uia.enums import PatternId

    control = _resolve_live_control(selector)
    pattern = control.GetPattern(PatternId.ExpandCollapsePattern)
    if pattern is None:
        raise ValueError(
            f"Element {selector} does not support expand/collapse"
        )
    state = _read_expand_collapse_state(pattern)
    if state == "collapsed" or state == "leaf":
        return {"action": "skipped", "state": state, "skipped": True}
    from agent_windows.core.uia_bypass import run_pattern_background
    run_pattern_background(control, pattern.Collapse)
    new_state = _read_expand_collapse_state(pattern) or "collapsed"
    return {"action": "collapsed", "state": new_state, "skipped": False}


def _traverse_window(
    client,
    window: WindowInfo,
    tree_lines: list[str],
    interactive: list[ElementInfo],
    max_depth: int,
    include_invisible: bool,
    include_offscreen: bool,
    indent: int,
    timeout: float,
    max_elements: int,
    global_deadline: float,
    cache_request,
) -> None:
    """Traverse UIA tree for a single window."""
    start = time.time()

    # M-25: fetch the per-node properties through a UIA CacheRequest so each
    # element arrives WITH its properties already cached (one cross-process
    # round-trip at retrieval) instead of six separate live CurrentX round-trips
    # per node. The request is built once per traversal in traverse_tree and
    # passed in (PERF3-04) — its property set is constant across windows.

    # Build the window root with retry. ElementFromHandleBuildCache is the most
    # common failure point when the target process is momentarily busy
    # (RPC_E_SERVERCALL_RETRYLATER / RPC_E_CALL_REJECTED). Only retryable COM
    # errors are retried with exponential backoff; dead-element / disconnected
    # errors mean the window is gone and are terminal (PERF3-06).
    root = None
    for attempt in range(_WINDOW_MAX_RETRIES + 1):
        try:
            root = client.IUIAutomation.ElementFromHandleBuildCache(window.handle, cache_request)
            break
        except COMError as e:
            mapped = from_com_error(e)
            if isinstance(mapped, UIARetryableError) and attempt < _WINDOW_MAX_RETRIES:
                wait = 0.5 * (2 ** attempt)
                # Don't let retry backoff eat the global wall-clock budget meant
                # for traversal: if the next sleep would blow the deadline, give
                # up on this window rather than starving the remaining ones.
                if time.time() + wait > global_deadline:
                    logger.debug(
                        "ElementFromHandleBuildCache busy for '%s' but retry "
                        "backoff %.1fs would exceed global deadline; giving up: %s",
                        window.title, wait, mapped,
                    )
                    return
                logger.debug(
                    "ElementFromHandleBuildCache busy for '%s' (attempt %d/%d), "
                    "retrying in %.1fs: %s",
                    window.title, attempt + 1, _WINDOW_MAX_RETRIES, wait, mapped,
                )
                time.sleep(wait)
                continue
            # Non-retryable, or retries exhausted: give up on this window.
            logger.debug(
                "ElementFromHandleBuildCache failed for '%s' (giving up): %s",
                window.title, mapped,
            )
            return
        except Exception as e:
            # Non-COM error (e.g. comtypes marshalling) — not retryable.
            logger.debug("ElementFromHandleBuildCache failed for '%s': %s", window.title, e)
            return

    if root is None:
        return

    _traverse_node(
        client, root, tree_lines, interactive,
        window, max_depth, include_invisible, include_offscreen,
        indent, 0, start, timeout, max_elements, global_deadline, cache_request,
    )


def _build_traversal_cache(client):
    """Build a UIA CacheRequest for the properties read per node. (M-25)

    Elements fetched through this request (``ElementFromHandleBuildCache`` and
    ``_get_cached_children``) arrive with these properties already cached, so
    reading them via ``GetCachedPropertyValue`` is a local lookup rather than a
    live cross-process COM round-trip.
    ``AutomationElementMode`` is left at its default (Full) so cached elements
    stay navigable for the recursive walk.

    The scope includes ``Element | Children`` so a cached element exposes its
    children via ``GetCachedChildren`` after a ``BuildUpdatedCache`` — this is
    what ``_get_cached_children`` relies on to fetch all siblings in one COM
    round-trip instead of one per child (PERF3-05).

    ``TreeFilter`` is pinned to ``RawViewCondition`` to match the
    ``RawViewWalker`` the old per-child walk used. A CacheRequest's default
    TreeFilter is NULL, which lets ``BuildUpdatedCache`` descend into subtrees
    the Raw view prunes (notably ``DocumentControl`` web content in browsers),
    inflating the tree ~4x and changing traversal semantics. Setting it to the
    walker's own condition keeps the bulk path's node set identical to the old
    walker path's (PERF3-05).
    """
    cache = client.IUIAutomation.CreateCacheRequest()
    cache.AddProperty(PropertyId.NameProperty)
    cache.AddProperty(PropertyId.ControlTypeProperty)
    cache.AddProperty(PropertyId.IsEnabledProperty)
    cache.AddProperty(PropertyId.IsOffscreenProperty)
    cache.AddProperty(PropertyId.BoundingRectangleProperty)
    cache.AddProperty(PropertyId.HasKeyboardFocusProperty)
    # WindowIsModal lets _traverse_node detect modal dialogs and mark their
    # subtree so render_snapshot can promote those elements (PERF3-06).
    cache.AddProperty(PropertyId.WindowIsModalProperty)
    # RuntimeId (D3): cached so each element's stable UIA identity is a local
    # lookup during traversal, not a live cross-process COM round-trip per
    # node. Selector commands re-resolve the live control via FindFirst by
    # RuntimeId instead of ControlFromPoint (Task 3).
    cache.AddProperty(PropertyId.RuntimeIdProperty)
    # TODO #15: richer element attributes surfaced in ElementInfo.metadata —
    # toggle/expand/value/range/selection/shortcut/help/password. Cached so each
    # is a local lookup, not a live COM round-trip per property.
    cache.AddProperty(PropertyId.ToggleToggleStateProperty)
    cache.AddProperty(PropertyId.ExpandCollapseExpandCollapseStateProperty)
    cache.AddProperty(PropertyId.ValueValueProperty)
    cache.AddProperty(PropertyId.LegacyIAccessibleValueProperty)
    cache.AddProperty(PropertyId.RangeValueValueProperty)
    cache.AddProperty(PropertyId.RangeValueMinimumProperty)
    cache.AddProperty(PropertyId.RangeValueMaximumProperty)
    cache.AddProperty(PropertyId.AcceleratorKeyProperty)
    cache.AddProperty(PropertyId.HelpTextProperty)
    cache.AddProperty(PropertyId.IsPasswordProperty)
    cache.AddProperty(PropertyId.SelectionItemIsSelectedProperty)
    # Scroll (TODO #12): surface scrollable containers so the agent can see
    # which regions scroll and their current position. Each is a cached local
    # lookup (no COM round-trip) — collected into metadata only when the
    # container actually reports scrollability, keeping the tree output quiet
    # for static panes.
    cache.AddProperty(PropertyId.ScrollVerticallyScrollableProperty)
    cache.AddProperty(PropertyId.ScrollHorizontallyScrollableProperty)
    cache.AddProperty(PropertyId.ScrollVerticalScrollPercentProperty)
    cache.AddProperty(PropertyId.ScrollHorizontalScrollPercentProperty)
    cache.TreeScope = TreeScope.TreeScope_Element | TreeScope.TreeScope_Children
    cache.TreeFilter = client.IUIAutomation.RawViewCondition
    return cache


def _get_cached_children(node, cache_request):
    """Return a node's children with their properties already cached. (PERF3-05)

    Instead of ``walker.GetFirstChildElementBuildCache`` /
    ``GetNextSiblingElementBuildCache`` — one cross-process COM round-trip per
    child — clone the request, narrow its scope to ``Children`` only, and call
    ``BuildUpdatedCache`` once: UIA then returns *all* children pre-cached via
    ``GetCachedChildren`` in a single round-trip. On large windows (browsers,
    IDEs) this collapses N round-trips into 1.

    Adapted from Windows-MCP's ``CachedControlHelper.get_cached_children``
    (``tree/cache_utils.py``), but operates on the raw comtypes element that
    ``tree.py`` uses throughout rather than the high-level ``Control`` wrapper.

    On failure returns ``[]`` — i.e. prunes this subtree — matching the old
    walker loop's behavior of silently stopping the iteration. No walker
    fallback: it shares the same COM plumbing as the bulk path, so it would
    fail under the same conditions (dead element, target process unreachable).
    """
    try:
        children_req = cache_request.Clone()
        children_req.TreeScope = TreeScope.TreeScope_Children
        updated = node.BuildUpdatedCache(children_req)
        element_array = updated.GetCachedChildren()
        if not element_array:
            return []
        length = element_array.Length
        return [element_array.GetElement(i) for i in range(length)]
    except Exception:
        logger.debug("get_cached_children failed; pruning subtree", exc_info=True)
        return []


def _traverse_node(
    client,
    node,
    tree_lines: list[str],
    interactive: list[ElementInfo],
    window: WindowInfo,
    max_depth: int,
    include_invisible: bool,
    include_offscreen: bool,
    indent: int,
    depth: int,
    start_time: float,
    timeout: float,
    max_elements: int,
    global_deadline: float,
    cache_request,
    in_modal_subtree: bool = False,
) -> bool:
    """Recursively traverse a UIA node.

    Returns ``produced_useful`` — whether this subtree rendered any line the
    agent can act on (an interactive element, a named non-structural leaf, or a
    descendant that did). Structural containers use it for empty-subtree
    pruning (TODO #6): a structural node whose whole subtree produced nothing
    useful AND which has no name is dropped from ``tree_lines`` (pure layout
    noise), while a named one is kept. Interactive elements are ALWAYS collected
    into ``interactive`` regardless of the prune decision, so ``@eN`` refs stay
    stable and resolve_element is unaffected.
    """
    if depth >= max_depth:
        return False
    if time.time() - start_time > timeout:
        return False
    if time.time() > global_deadline:
        return False
    if max_elements > 0 and len(interactive) >= max_elements:
        return False

    try:
        # Read the per-node properties from the element's cache (M-25) — each is
        # a local lookup, not a live cross-process COM round-trip. Per-property
        # try/except mirrors the old CurrentX code so a stale/missing cached
        # value degrades to the same default rather than aborting the node.
        name = ""
        try:
            name = node.GetCachedPropertyValue(PropertyId.NameProperty) or ""
        except Exception:
            logger.debug("Failed to get cached node property", exc_info=True)

        control_type = ""
        try:
            ct = node.GetCachedPropertyValue(PropertyId.ControlTypeProperty)
            control_type = _control_type_name(ct) if ct is not None else ""
        except Exception:
            logger.debug("Failed to get cached node property", exc_info=True)

        is_enabled = True
        try:
            val = node.GetCachedPropertyValue(PropertyId.IsEnabledProperty)
            is_enabled = bool(val) if val is not None else True
        except Exception:
            logger.debug("Failed to get cached node property", exc_info=True)

        is_offscreen = False
        try:
            val = node.GetCachedPropertyValue(PropertyId.IsOffscreenProperty)
            is_offscreen = bool(val) if val is not None else False
        except Exception:
            logger.debug("Failed to get cached node property", exc_info=True)

        # Get bounding rectangle. GetCachedPropertyValue unwraps the cached
        # BoundingRectangle VARIANT into a (left, top, WIDTH, HEIGHT) tuple of
        # floats — NOT the edge-based RECT the typed CurrentBoundingRectangle
        # accessor returns (whose .right/.bottom are screen-coordinate edges).
        # Convert width/height back to edges so BoundingBox matches the old
        # representation: right = left + width, bottom = top + height.
        bbox = None
        try:
            rect = node.GetCachedPropertyValue(PropertyId.BoundingRectangleProperty)
            left, top = int(rect[0]), int(rect[1])
            bbox = BoundingBox(
                left=left,
                top=top,
                right=left + int(rect[2]),
                bottom=top + int(rect[3]),
            )
        except Exception:
            logger.debug("Failed to get cached node property", exc_info=True)

        # Filter
        if bbox and bbox.area == 0 and not include_invisible:
            return False
        if is_offscreen and not include_offscreen:
            return False

        # Determine if interactive
        is_interactive = control_type in INTERACTIVE_TYPES and is_enabled

        # Get focus state
        is_focused = False
        try:
            is_focused = bool(node.GetCachedPropertyValue(PropertyId.HasKeyboardFocusProperty))
        except Exception:
            logger.debug("Failed to get cached node property", exc_info=True)

        # Collect richer attributes (TODO #15 / #12). Done unconditionally —
        # before the interactive decision — because a node may carry actionable
        # metadata (scrollable container, expandable group) that PROMOTES it to
        # interactive even though its control type (Pane/Group/Document) isn't
        # in INTERACTIVE_TYPES. Those are exactly the containers the agent
        # needs to see and address (@eN). Mirrors reference's separate
        # scrollable_nodes list, folded into our single interactive list.
        metadata = _collect_rich_metadata(node, control_type)

        # Promote scrollable / expandable containers to interactive so they get
        # an @eN ref and surface in the Interactive Elements table. (TODO #12)
        if is_enabled and ("scrollable" in metadata or
                           "expand_collapse_state" in metadata):
            is_interactive = True

        # Detect a modal dialog: a WindowControl whose WindowIsModal is True.
        # Once entered, every interactive element in this subtree is marked
        # is_modal so render_snapshot can promote them to the front. The flag
        # propagates down via in_modal_subtree (PERF3-06).
        is_modal_window = False
        if control_type == "WindowControl":
            try:
                val = node.GetCachedPropertyValue(PropertyId.WindowIsModalProperty)
                # Guard None explicitly: UIA may return a ReservedNotSupportedValue
                # sentinel (a non-None COM object) for providers that don't
                # implement the property, and bool(sentinel) would be True —
                # falsely marking a non-modal window's subtree. Mirror the other
                # property reads' None-aware handling.
                is_modal_window = bool(val) if val is not None else False
            except Exception:
                logger.debug("Failed to get cached node property", exc_info=True)
        this_in_modal = in_modal_subtree or is_modal_window

        # RuntimeId for selector re-resolution (D3). Prefer the cached value
        # (zero COM cost — RuntimeIdProperty is in _build_traversal_cache); fall
        # back to live GetRuntimeId if the provider didn't cache it. Spike
        # finding (Task 1 Minor): GetCachedPropertyValue swallows COMError →
        # None, BUT UIA's "not supported" is a TRUTHY ReservedNotSupported
        # sentinel (not None), so a bare `if cached_rid` falsy-check would pass
        # the sentinel through. Coerce to tuple[int]; None/sentinel/non-int
        # raise TypeError/ValueError and trigger the live fallback. Live read
        # is also wrapped: a stale/disconnected element raises here, not in the
        # caller. Empty () on total failure → _resolve_live_control's coordinate
        # fallback (D3 D2). Computed unconditionally (cheap) but consumed only
        # on the interactive branch below; non-interactive nodes don't need an
        # @eN ref, so the field stays () for them.
        runtime_id: tuple[int, ...] = ()
        try:
            cached_rid = node.GetCachedPropertyValue(
                PropertyId.RuntimeIdProperty)
            runtime_id = tuple(int(v) for v in cached_rid)
        except (TypeError, ValueError):
            runtime_id = ()
        if not runtime_id:
            try:
                runtime_id = tuple(
                    int(v) for v in (node.GetRuntimeId() or []))
            except Exception:
                logger.debug(
                    "RuntimeId live read failed for node", exc_info=True)
                runtime_id = ()

        # Add to interactive elements list. Done for depth>0 nodes only (the
        # window root at depth 0 is never an element) and UNCONDITIONALLY w.r.t.
        # tree-line pruning — so the interactive list and @eN refs are
        # independent of whether the node's TREE LINE is emitted: a pruned-away
        # empty container still contributes its interactive children, and an
        # element promoted to interactive by metadata (scrollable/expandable) is
        # kept even if its structural parent is later dropped. (TODO #6)
        if depth > 0 and is_interactive:
            interactive.append(ElementInfo(
                name=name,
                control_type=control_type,
                bbox=bbox,
                is_interactive=True,
                is_focused=is_focused,
                is_enabled=is_enabled,
                is_offscreen=is_offscreen,
                is_modal=this_in_modal,
                window_name=window.title,
                window_handle=window.handle,
                runtime_id=runtime_id,
                metadata=metadata,
            ))

        # Recurse into children FIRST, collecting their lines into a local list
        # (not straight into tree_lines). We need to know whether the subtree
        # produced anything useful before deciding whether to emit THIS node's
        # own line — that's the basis for empty-container pruning (TODO #6).
        # PERF3-05: fetch all children pre-cached in one COM round-trip
        # (``BuildUpdatedCache`` + ``GetCachedChildren``) instead of one
        # ``GetNextSiblingElementBuildCache`` call per child.
        child_lines: list[str] = []
        any_child_useful = False
        try:
            for child in _get_cached_children(node, cache_request):
                child_useful = _traverse_node(
                    client, child, child_lines, interactive,
                    window, max_depth, include_invisible, include_offscreen,
                    indent, depth + 1, start_time, timeout,
                    max_elements, global_deadline, cache_request,
                    in_modal_subtree=this_in_modal,
                )
                if child_useful:
                    any_child_useful = True
                if max_elements > 0 and len(interactive) >= max_elements:
                    break
        except Exception as e:
            # Backstop: _get_cached_children and _traverse_node swallow their own
            # errors, so this only fires on something escaping both (e.g. a
            # comtypes marshalling fault mid-iteration). Keeps a single bad node
            # from aborting the whole subtree's siblings.
            logger.debug("Error iterating children at depth %d: %s", depth, e)

        # Build this node's own line (depth 0 = window root, no own line).
        prefix = " " * indent + "├─ " if depth > 0 else " " * indent
        own_line = None
        is_self_useful = False
        if depth > 0:
            type_label = control_type.replace("Control", "") if control_type else "?"
            name_label = f' "{name}"' if name else ""
            meta_parts = []
            if is_focused:
                meta_parts.append("focused")
            if not is_enabled:
                meta_parts.append("disabled")
            if is_modal_window:
                meta_parts.append("modal")
            if is_interactive and is_enabled:
                meta_parts.append("interactive")
            meta_str = " ".join(f"[{m}]" for m in meta_parts)
            bbox_str = f" {bbox}" if bbox else ""
            own_line = f"{prefix}{type_label}{name_label}{bbox_str} {meta_str}".rstrip()
            # A node's own line is "useful" to the agent if it's actionable
            # (interactive) or carries a name (a labeled leaf/label has info
            # value; a structural container's usefulness also hinges on name —
            # see the prune rule below).
            is_self_useful = bool(is_interactive) or bool(name)

        # Empty-container pruning (TODO #6). A structural container (Pane/Group/
        # Custom/ToolBar/Tab/MenuBar) whose whole subtree produced nothing useful
        # AND which has no name is dropped — it's pure layout noise. A named one
        # is kept (it may label a region even when empty). Interactive elements
        # were already collected above, so dropping the line never loses an
        # @eN ref. The ``not is_interactive`` guard is key: a structural node
        # PROMOTED to interactive by metadata (scrollable/expandable) is always
        # shown even when childless — otherwise the tree would hide an actionable
        # target that the Interactive Elements table still references via @eN.
        # Non-structural nodes are always emitted (they have a reason: interactive
        # or named).
        emit_own_line = True
        if (depth > 0 and control_type in _STRUCTURAL_TYPES
                and not any_child_useful and not name and not is_interactive):
            emit_own_line = False

        if emit_own_line and own_line is not None:
            tree_lines.append(own_line)
        tree_lines.extend(child_lines)

        produced_useful = any_child_useful or is_self_useful or (depth == 0)
        return produced_useful

    except Exception as e:
        logger.debug("Error traversing node at depth %d: %s", depth, e)
        return False


# ── Rich metadata collection (TODO #15) ────────────────────────────


def _is_uia_sentinel(val: Any) -> bool:
    """True if *val* is UIA's ReservedNotSupportedValue / ReservedMixedAttributeValue.

    UIA returns these non-None COM sentinels (``IUnknown`` pointers) for
    properties a provider doesn't implement. ``bool(sentinel)`` is ``True`` and
    ``str(sentinel)`` is non-empty, so without filtering they'd corrupt bool /
    value reads (e.g. falsely flag a control ``is_password`` or ``is_selected``).
    The real values we collect are always bool/int/float/str, so reject any
    other type. (TODO #15; mirrors the WindowIsModal guard at the top of
    _traverse_node.)
    """
    return not isinstance(val, (bool, int, float, str))


def _read_cached(node, prop_id) -> Any:
    """Read one cached property, returning None on any failure or sentinel.

    A per-property try/except mirrors the existing node reads, and the sentinel
    filter (_is_uia_sentinel) stops a NotSupported/MixedAttribute COM object
    from being misread as a truthy bool or a non-empty string. GetCachedPropertyValue
    is a local lookup (no COM round-trip), so reading several is cheap. (TODO #15)
    """
    try:
        val = node.GetCachedPropertyValue(prop_id)
    except Exception:
        logger.debug("Failed to get cached node property", exc_info=True)
        return None
    if val is None or _is_uia_sentinel(val):
        return None
    return val


def _fmt_scroll_pct(val: Any) -> str:
    """Format a ScrollPattern percent value for metadata. (TODO #12)

    UIA reports scroll position as a float in [0, 100] (or -1 /
    ``ScrollPattern.NoScrollValue`` when not set). Round to an int for the
    tree output — sub-percent precision isn't actionable for the agent.
    Returns "0" for None/invalid so the marker still reads sensibly.
    """
    try:
        n = round(float(val))
    except (TypeError, ValueError):
        return "0"
    if n < 0:  # NoScrollValue sentinel
        return "0"
    return str(n)


# Control types whose Toggle state is meaningful (on/off/indeterminate).
_TOGGLE_TYPES = {"ButtonControl", "CheckBoxControl", "MenuItemControl",
                 "ListItemControl", "TreeItemControl"}
# Controls carrying a text value (input fields).
_VALUE_TYPES = {"EditControl", "TextBoxControl", "DocumentControl"}
# Controls exposing a numeric range (sliders, spinners).
_RANGE_TYPES = {"SliderControl", "SpinnerControl"}

# Control type → the primary action verb the agent should use on it. Extends the
# reference's _ACTION_MAP (tree/views.py) — the reference only covers
# edit/checkbox/combobox/slider/radio/document; this adds TextBoxControl (fill),
# ListItemControl/TreeItemControl/TabItemControl (select),
# SpinnerControl/ScrollBarControl (slide) and keys everything to this project's
# XxxControl names (the lowercased
# "check box" form in the reference doesn't match what UIA returns here). Verbs
# line up with the MCP tools the agent actually has: click→click, fill→type,
# toggle→click a checkbox, select→click a radio/list item,
# slide→drag the thumb on a range control (Slider/Spinner/ScrollBar all expose
# RangeValuePattern), scroll→scroll, expand→expand/collapse. Anything not listed
# (Button, MenuItem, Hyperlink, …) defaults to click. (TODO #5)
_ACTION_MAP: dict[str, str] = {
    "EditControl": "fill",
    "TextBoxControl": "fill",
    "CheckBoxControl": "toggle",
    "ComboBoxControl": "select",
    "RadioButtonControl": "select",
    "ListItemControl": "select",
    "TreeItemControl": "select",
    "TabItemControl": "select",
    "SliderControl": "slide",
    "SpinnerControl": "slide",
    "ScrollBarControl": "slide",
    "DocumentControl": "scroll",
}


def _action_for(control_type: str, metadata: dict[str, Any] | None = None) -> str:
    """Return the action verb(s) an element supports, for the Action column.

    Starts from the control-type map, then overlays property-driven actions read
    from ``metadata`` (computed in _collect_rich_metadata) — these are what let a
    *promoted* scrollable/expandable container (Pane/Group, not in the
    control-type map) still get a meaningful verb. Multiple verbs join with ``/``
    (e.g. a ComboBox exposing expand state is ``select/expand``; a container that
    both scrolls and expands is ``scroll/expand``). ``click`` is the fallback only
    when no type verb AND no scroll/expand metadata apply — so a promoted
    scrollable pane reports ``scroll`` (its real action), not the misleading
    ``click/scroll``. Pure function — no COM, safe to unit-test directly. (TODO #5)
    """
    verbs: list[str] = []
    base = _ACTION_MAP.get(control_type)
    if base:
        verbs.append(base)
    # Property-driven actions apply regardless of control type — a scrollable
    # Pane or an expandable Group gets scroll/expand even though "PaneControl"
    # isn't in _ACTION_MAP.
    if metadata:
        if "scrollable" in metadata and "scroll" not in verbs:
            verbs.append("scroll")
        if "expand_collapse_state" in metadata and "expand" not in verbs:
            verbs.append("expand")
    if not verbs:
        verbs.append("click")
    return "/".join(verbs)


def _collect_rich_metadata(node, control_type: str) -> dict[str, Any]:
    """Collect richer element attributes into the metadata dict. (TODO #15)

    Reads only cached properties (one local lookup each — the CacheRequest in
    _build_traversal_cache pre-fetched them), so this adds no COM round-trips.
    Fills keys conditionally based on control_type to keep the metadata concise:
      - toggle_state  for Button/CheckBox/MenuItem/ListItem/TreeItem
      - value         for Edit/TextBox/Document (text content) and Slider/Spinner
      - is_password   for Edit/TextBox
      - expand_collapse_state for ComboBox/TreeItem/MenuItem/SplitButton
      - min/max/value for Slider/Spinner (range)
      - is_selected   for ListItem/TreeItem (selection state)
      - shortcut      (AcceleratorKey) and help_text for any element that has one

    Returns an empty dict when no richer attributes apply (keeps ElementInfo
    small and the render output quiet for plain buttons/links).
    """
    from agent_windows.uia.enums import ToggleState, ExpandCollapseState

    md: dict[str, Any] = {}

    # Shortcut / help text apply to any element that exposes them.
    accel = _read_cached(node, PropertyId.AcceleratorKeyProperty)
    if isinstance(accel, str) and accel.strip():
        md["shortcut"] = accel.strip()
    help_text = _read_cached(node, PropertyId.HelpTextProperty)
    if isinstance(help_text, str) and help_text.strip():
        md["help_text"] = help_text.strip()

    # Toggle state (on/off/indeterminate) for checkable controls.
    if control_type in _TOGGLE_TYPES:
        ts = _read_cached(node, PropertyId.ToggleToggleStateProperty)
        if ts is not None:
            try:
                md["toggle_state"] = {
                    ToggleState.Off: "off",
                    ToggleState.On: "on",
                    ToggleState.Indeterminate: "indeterminate",
                }.get(int(ts))
            except (TypeError, ValueError):
                pass

    # Expand/collapse state — collected for ANY element that exposes a valid
    # ExpandCollapseExpandCollapseStateProperty value (property-gated, not
    # control-type-gated). Browsers/chromium expose expand/collapse on
    # Group/Tree nodes beyond the classic ComboBox/TreeItem/SplitButton set;
    # gating by property value catches them while leaving plain controls
    # quiet (LeafNode/no property → no key). Mirrors reference's
    # tree/service.py GroupControl handling. (TODO #12)
    ecs = _read_cached(node, PropertyId.ExpandCollapseExpandCollapseStateProperty)
    if ecs is not None:
        try:
            state_str = _EC_STATE_STR.get(int(ecs))
            # Only record when the element is actually expandable — LeafNode
            # means "no children to expand", so it carries no useful state.
            if state_str and state_str != "leaf":
                md["expand_collapse_state"] = state_str
        except (TypeError, ValueError):
            pass

    # Scrollable container (TODO #12): report which axes scroll and the
    # current position, so the agent knows where to send wheel input. Only
    # emitted when at least one axis reports scrollable — static panes stay
    # quiet. Axis string ("v"/"h"/"vh") is denser than a bare bool.
    v_scrollable = _read_cached(node, PropertyId.ScrollVerticallyScrollableProperty)
    h_scrollable = _read_cached(node, PropertyId.ScrollHorizontallyScrollableProperty)
    axes = []
    if v_scrollable:
        axes.append("v")
    if h_scrollable:
        axes.append("h")
    if axes:
        md["scrollable"] = "".join(axes)
        parts = []
        if v_scrollable:
            pct = _read_cached(node, PropertyId.ScrollVerticalScrollPercentProperty)
            parts.append(f"v:{_fmt_scroll_pct(pct)}%")
        if h_scrollable:
            pct = _read_cached(node, PropertyId.ScrollHorizontalScrollPercentProperty)
            parts.append(f"h:{_fmt_scroll_pct(pct)}%")
        md["scroll_pct"] = " ".join(parts)

    # Text value + password flag for input fields.
    if control_type in _VALUE_TYPES:
        # Prefer ValueValue (ValuePattern), fall back to LegacyIAccessibleValue.
        value = _read_cached(node, PropertyId.ValueValueProperty)
        if value is None:
            value = _read_cached(node, PropertyId.LegacyIAccessibleValueProperty)
        if isinstance(value, str):
            md["value"] = value.strip() if value.strip() else "(empty)"
        if _read_cached(node, PropertyId.IsPasswordProperty):
            md["is_password"] = True

    # Numeric range (slider/spinner): value, min, max.
    if control_type in _RANGE_TYPES:
        rv = _read_cached(node, PropertyId.RangeValueValueProperty)
        rmin = _read_cached(node, PropertyId.RangeValueMinimumProperty)
        rmax = _read_cached(node, PropertyId.RangeValueMaximumProperty)
        if rv is not None:
            try:
                md["value"] = round(float(rv), 2)
            except (TypeError, ValueError):
                pass
        if rmin is not None:
            try:
                md["min"] = round(float(rmin), 2)
            except (TypeError, ValueError):
                pass
        if rmax is not None:
            try:
                md["max"] = round(float(rmax), 2)
            except (TypeError, ValueError):
                pass

    # Selection state for list/tree/radio/tab items (is this item selected?).
    if control_type in {"ListItemControl", "TreeItemControl",
                        "RadioButtonControl", "TabItemControl"}:
        sel = _read_cached(node, PropertyId.SelectionItemIsSelectedProperty)
        if sel is not None:
            md["is_selected"] = bool(sel)

    return md


# ── Control type mapping ─────────────────────────────────────────

# UIA Control Type IDs → human-readable names
_CONTROL_TYPE_MAP: dict[int, str] = {}


def _control_type_name(type_id: int) -> str:
    """Convert a UIA control type ID to a human-readable name."""
    global _CONTROL_TYPE_MAP

    if not _CONTROL_TYPE_MAP:
        # Build the map from the uia module's ControlType class
        try:
            from agent_windows.uia.core import ControlType
            for attr in dir(ControlType):
                if attr.endswith("Control"):
                    val = getattr(ControlType, attr)
                    if isinstance(val, int):
                        _CONTROL_TYPE_MAP[val] = attr
        except Exception:
            pass

        # Fill in any gaps with hardcoded fallback
        _CONTROL_TYPE_MAP.setdefault(50000, "ButtonControl")
        _CONTROL_TYPE_MAP.setdefault(50032, "WindowControl")
        _CONTROL_TYPE_MAP.setdefault(50033, "PaneControl")

    return _CONTROL_TYPE_MAP.get(type_id, f"Unknown({type_id})")


# ── Text rendering ────────────────────────────────────────────────


def render_windows_table(windows: list[WindowInfo], include_hwnd: bool = False) -> str:
    """Render the windows markdown table (header + separator + rows).

    Shared by :func:`render_snapshot` (observe's ``## Windows`` block, HWND off)
    and :func:`agent_windows.core.app_manager.list_windows` (``## App List``,
    HWND on — app list's job is to enumerate windows to act on, so the precise
    handle is worth the extra column; observe's table is informational and stays
    HWND-free). Returns only the table; the caller owns the ``##`` title and any
    trailing blank line. Always emits header + separator (even when empty) so
    each caller keeps its own empty-list policy.
    """
    if include_hwnd:
        rows = ["| # | Title | App | Status | HWND | Position |",
                "|---|-------|-----|--------|------|----------|"]
    else:
        rows = ["| # | Title | App | Status | Position |",
                "|---|-------|-----|--------|----------|"]
    for i, w in enumerate(windows, 1):
        # Status reflects placement (Maximized/Minimized/Normal). is_active is
        # orthogonal (the foreground window can be any of the three), so append
        # "(active)" rather than overwriting — keeps both signals.
        status = w.status
        if w.is_active:
            status = f"{status} (active)"
        # Surface is_browser as a marker on the App cell so the hint reaches the
        # agent (prefer keyboard/tree refs over coord guesses on web content).
        app = w.process_name
        if w.is_browser:
            app = f"{app} (browser)"
        pos = str(w.bbox) if w.bbox else "N/A"
        if include_hwnd:
            rows.append(f"| {i} | {w.title[:40]} | {app} | {status} | 0x{w.handle:X} | {pos} |")
        else:
            rows.append(f"| {i} | {w.title[:40]} | {app} | {status} | {pos} |")
    return "\n".join(rows)


def render_snapshot(data: dict[str, Any]) -> str:
    """Render the snapshot data as structured text for AI consumption."""
    parts = []

    # Desktop State
    cursor = data["cursor"]
    res = data["resolution"]
    displays = data["displays"]
    active = data.get("active_window")

    parts.append("## Desktop State")
    # Cursor: nulled by display filtering when the pointer is off the selected
    # display(s) (TODO #9-B); show N/A so the agent doesn't use stale coords.
    if cursor is None:
        parts.append("Cursor: N/A (off selected display)")
    else:
        parts.append(f"Cursor: ({cursor[0]}, {cursor[1]})")
    parts.append(f"Resolution: {res[0]}x{res[1]}")
    parts.append(f"Displays: {displays}")
    # Virtual screen + DPI scale + coordinate space (TODO #3). Appended after
    # the existing lines so nothing depending on the original header order
    # shifts. virtual_screen may be absent on older snapshots (defensive .get).
    vscreen = data.get("virtual_screen")
    if vscreen is not None:
        parts.append(f"Virtual Screen: ({vscreen[0]}, {vscreen[1]}, {vscreen[2]}, {vscreen[3]})")
    dpi_scale = data.get("dpi_scale")
    if dpi_scale is not None:
        dpi = round(dpi_scale * 96)
        parts.append(f"DPI Scale: {dpi_scale:g} ({dpi} dpi)")
    # The daemon is PerMonitorDpiAware, so all coordinates here are physical
    # pixels in the virtual-desktop space — state that explicitly so the agent
    # doesn't try to apply DPI correction to click coords.
    parts.append("Coordinate Space: Virtual desktop coordinates (physical pixels)")
    if active:
        parts.append(f'Active Window: "{active.title}" ({active.process_name})')
    parts.append("")

    # Windows table (shared renderer; observe shows it HWND-free — `app list`
    # turns the HWND column on for precise targeting. See render_windows_table.)
    windows = data.get("windows", [])
    if windows:
        parts.append("## Windows")
        parts.append(render_windows_table(windows))
        parts.append("")

    # Semantic tree. NOTE: the tree text is NOT display-filtered (TODO #9-B) —
    # it is a pre-rendered free-text string with no per-line bbox, so clipping
    # it to a display region is not reliable. When a display filter is active,
    # surface that here so the agent knows the tree still shows the full desktop
    # while windows/elements/cursor above are scoped to the selected display(s).
    display_filter = data.get("display_filter")
    tree = data.get("semantic_tree", "")
    if display_filter and tree:
        parts.append(
            f"[Accessibility Tree not filtered — shows full desktop. "
            f"Windows/elements/cursor filtered to display(s) "
            f"{','.join(str(i) for i in display_filter)}]"
        )
        parts.append("")
    if tree:
        parts.append("## Accessibility Tree")
        parts.append(tree)
        parts.append("")

    # Interactive elements table. The list itself is already ordered with modal
    # elements first (see traverse_tree), so @eN refs here match the indices
    # resolve_element uses against interactive_elements (PERF3-06).
    #
    # Display filtering (TODO #9-B) stashes each survivor's ORIGINAL 1-based
    # index in metadata["_orig_idx"]; emit that so refs stay valid against the
    # UNFILTERED snapshot resolve_element resolves against (non-contiguous
    # @e1, @e3, @e7 …). Without it, renumbering from 1 would silently point
    # every @eN at the wrong element.
    elements = data.get("interactive_elements", [])
    if elements:
        parts.append("## Interactive Elements")
        parts.append("| Ref | Type | Action | Name | Window | Position | State |")
        parts.append("|-----|------|--------|------|---------|----------|-------|")
        for i, el in enumerate(elements, 1):
            orig_idx = el.metadata.get("_orig_idx") if el.metadata else None
            ref = f"@e{orig_idx}" if orig_idx else f"@e{i}"
            type_name = el.control_type.replace("Control", "")
            # Action verb(s) this element supports (TODO #5): tells the agent
            # whether @eN is click/fill/toggle/select/slide/scroll/expand — derived
            # from control_type + metadata. Kept separate from State so "what to
            # do" isn't diluted by "current state" (toggle_state, value, …).
            action = _action_for(el.control_type, el.metadata)
            if el.name:
                # Surface the dropped tail length so the agent doesn't read a
                # silently-cut prefix as the whole label. (P3.7)
                name = _truncate_with_count(el.name, 40)
            else:
                name = ""
            # Truncate the owning window title like the Windows table does, so a
            # long title doesn't blow out the row width. Empty when the element
            # has no owning window (legacy/constructed ElementInfo).
            window_name = el.window_name[:30] if el.window_name else ""
            pos = str(el.bbox) if el.bbox else "N/A"
            states = []
            if el.is_modal:
                states.append("modal")
            if el.is_focused:
                states.append("focused")
            if not el.is_enabled:
                states.append("disabled")
            # TODO #15: surface richer attributes from metadata. _orig_idx is a
            # display-filtering internal (not user-facing), so skip it. Each key
            # renders as ``key:value`` for compactness; long values (value/help_text)
            # are truncated. Truncate BEFORE escaping so a pipe near the boundary
            # can't be split from its backslash; collapse newlines and escape pipes
            # so user-controlled text (an Edit value, help_text) can't break the
            # markdown table row.
            if el.metadata:
                for key, val in el.metadata.items():
                    if key == "_orig_idx":
                        continue
                    raw = str(val).replace("\r", " ").replace("\n", " ")
                    if len(raw) > 30:
                        # Append the dropped char count so the agent knows a
                        # tail remains (worth a get-text follow-up). (P3.7)
                        raw = raw[:27] + f"...(+{len(raw) - 27})"
                    states.append(f"{key}:{raw.replace('|', '\\|')}")
            state_str = ", ".join(states)
            parts.append(f"| {ref} | {type_name} | {action} | {name} | {window_name} | {pos} | {state_str} |")
        parts.append("")

    return "\n".join(parts)


# ── snapshot-diff (structural diff of two snapshots) ────────────────

# Element pairing: identity excludes `name` (a label/text change should be
# reported as `changed`, not a spurious add+remove). Within one identity
# bucket, bbox-bearing elements pair by nearest centre; bbox-less elements
# pair by exact name. MATCH_RADIUS caps spatial pairing (a big move ⇒
# removed+added). MOVE_THRESHOLD flags a paired element as "moved".
_SNAPSHOT_DIFF_MATCH_RADIUS = 60.0
_SNAPSHOT_DIFF_MOVE_THRESHOLD = 10.0


def _element_identity(el: "ElementInfo") -> tuple[str, int]:
    """Pairing key: (control_type, window_handle).

    ``name`` is excluded: a label/text change surfaces as ``changed``, not
    add+remove. ``window_name`` is excluded: it mirrors the owning window's
    title, which shifts on load/tab/state and would flood Added/Removed. Instead
    the STABLE ``window_handle`` (HWND, lifetime-stable) scopes each bucket to
    one window, so same-type elements in DIFFERENT windows no longer share a
    bucket and cannot mis-pair by bbox proximity — a precision win over
    control_type-only when diffing the full desktop. ``window_name`` is still
    shown in render; a title change is reported once at window level as
    ``windows_changed``. A ``window_handle`` of 0 (unset/legacy) degrades to a
    shared bucket across all 0-handle elements; rare, since traversal always
    fills the owning handle.
    """
    return (el.control_type, el.window_handle)


def _element_center(el: "ElementInfo") -> tuple[float, float] | None:
    if el.bbox is None:
        return None
    cx, cy = el.bbox.center
    return (float(cx), float(cy))


def _pair_elements(
    base: list["ElementInfo"], cur: list["ElementInfo"],
) -> tuple[list[tuple["ElementInfo", "ElementInfo"]], list["ElementInfo"], list["ElementInfo"]]:
    """Greedy nearest-neighbour pairing within identity buckets.

    Returns ``(pairs, removed[base-only], added[cur-only])``.
    """
    from collections import defaultdict

    def bucket(elems):
        b: dict[tuple, list] = defaultdict(list)
        for e in elems:
            b[_element_identity(e)].append(e)
        return b

    base_b = bucket(base)
    cur_b = bucket(cur)
    pairs: list[tuple[ElementInfo, ElementInfo]] = []
    removed: list[ElementInfo] = []
    added: list[ElementInfo] = []

    # Iterate buckets in first-seen order so rendered Added/Removed order is
    # stable across processes (a `set` of string-tuple keys is hash-randomized
    # via PYTHONHASHSEED, which would shuffle the section order otherwise).
    seen: set = set()
    keys: list = []
    for d in (base_b, cur_b):
        for k in d:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    for key in keys:
        b_list = list(base_b.get(key, []))
        c_list = list(cur_b.get(key, []))
        b_nobox = [e for e in b_list if e.bbox is None]
        b_box = [e for e in b_list if e.bbox is not None]
        c_nobox = [e for e in c_list if e.bbox is None]
        c_box = [e for e in c_list if e.bbox is not None]

        # bbox-less: exact-name match within the bucket.
        for b_el in list(b_nobox):
            for i, c_el in enumerate(c_nobox):
                if (c_el.name or "") == (b_el.name or ""):
                    pairs.append((b_el, c_el))
                    b_nobox.remove(b_el)
                    c_nobox.pop(i)
                    break
        removed.extend(b_nobox)
        added.extend(c_nobox)

        # bbox-bearing: nearest-neighbour within MATCH_RADIUS.
        dists = []
        for bi, b_el in enumerate(b_box):
            bc = _element_center(b_el)
            for ci, c_el in enumerate(c_box):
                cc = _element_center(c_el)
                dx, dy = bc[0] - cc[0], bc[1] - cc[1]
                dists.append(((dx * dx + dy * dy) ** 0.5, bi, ci))
        dists.sort()
        used_b: set[int] = set()
        used_c: set[int] = set()
        for dist, bi, ci in dists:
            # MATCH_RADIUS caps spatial pairing: a base element only pairs with
            # a current element within this centre distance. dists is sorted
            # ascending, so everything past the first over-radius entry is also
            # too far — those base/cur elements fall through to removed/added.
            if dist > _SNAPSHOT_DIFF_MATCH_RADIUS:
                continue
            if bi in used_b or ci in used_c:
                continue
            used_b.add(bi)
            used_c.add(ci)
            pairs.append((b_box[bi], c_box[ci]))
        removed.extend(b for i, b in enumerate(b_box) if i not in used_b)
        added.extend(c for i, c in enumerate(c_box) if i not in used_c)

    return pairs, removed, added


def _diff_element(a: "ElementInfo", b: "ElementInfo") -> list[tuple[str, str, str]]:
    """Field deltas between a paired (baseline=a, current=b) element. Empty if identical."""
    deltas: list[tuple[str, str, str]] = []
    if (a.name or "") != (b.name or ""):
        deltas.append(("name", a.name or "", b.name or ""))
    if a.is_enabled != b.is_enabled:
        deltas.append(("enabled", str(a.is_enabled), str(b.is_enabled)))
    if a.is_focused != b.is_focused:
        deltas.append(("focused", str(a.is_focused), str(b.is_focused)))
    if a.is_offscreen != b.is_offscreen:
        deltas.append(("offscreen", str(a.is_offscreen), str(b.is_offscreen)))
    if a.is_modal != b.is_modal:
        deltas.append(("modal", str(a.is_modal), str(b.is_modal)))
    av = a.metadata.get("value") if a.metadata else None
    bv = b.metadata.get("value") if b.metadata else None
    if av != bv:
        deltas.append(("value", str(av), str(bv)))
    ac, bc = _element_center(a), _element_center(b)
    if ac and bc:
        dx, dy = ac[0] - bc[0], ac[1] - bc[1]
        if (dx * dx + dy * dy) ** 0.5 > _SNAPSHOT_DIFF_MOVE_THRESHOLD:
            deltas.append(("moved", f"{int(ac[0])},{int(ac[1])}",
                           f"{int(bc[0])},{int(bc[1])}"))
    return deltas


def diff_snapshots(baseline: dict, current: dict) -> dict:
    """Structural diff of two ``traverse_tree`` result dicts.

    Returns ``{added, removed, changed, windows_added, windows_removed, stats}``.
    ``changed`` entries are ``{"element": <current ElementInfo>, "deltas": [...]}``.

    Windows are identity-keyed by ``(process_name, handle)`` — ``handle`` is
    stable for a window's lifetime (only a close+reopen changes it), unlike
    ``title`` which shifts on load/tab/state. A title change thus pairs the
    window: it is NOT add/remove noise, but reported ONCE as ``windows_changed``
    (window level), not per-element — a title change shifts every element's
    ``window_name``, so a per-element delta would flood the element Changed
    list. Element identity excludes ``window_name`` for the same reason (see
    ``_element_identity``).
    """
    base_els = baseline.get("interactive_elements", [])
    cur_els = current.get("interactive_elements", [])
    pairs, removed, added = _pair_elements(base_els, cur_els)
    changed = []
    for b_el, c_el in pairs:
        deltas = _diff_element(b_el, c_el)
        if deltas:
            changed.append({"element": c_el, "deltas": deltas})

    base_w = {(w.process_name, w.handle): w for w in baseline.get("windows", [])}
    cur_w = {(w.process_name, w.handle): w for w in current.get("windows", [])}
    win_added = [cur_w[k] for k in cur_w if k not in base_w]
    win_removed = [base_w[k] for k in base_w if k not in cur_w]
    # A paired window whose title changed (same process+handle). Reported ONCE
    # at window level — a title change shifts every element's window_name, so a
    # per-element changed:window would flood the element Changed list.
    win_changed = []
    for key, c_win in cur_w.items():
        b_win = base_w.get(key)
        if b_win is not None and (b_win.title or "") != (c_win.title or ""):
            win_changed.append({"window": c_win,
                                "title": (b_win.title or "", c_win.title or "")})

    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "windows_added": win_added,
        "windows_removed": win_removed,
        "windows_changed": win_changed,
        "stats": {"baseline_n": len(base_els), "current_n": len(cur_els)},
    }


def _oneline(s: Any, limit: int = 60) -> str:
    """Flatten newlines/tabs (one element = one diff line) and cap length.

    A multiline value (e.g. a Notepad document's full text) would otherwise
    split a Changed row across lines and scramble the rendered section.
    """
    s = str(s).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return s if len(s) <= limit else s[:limit - 3] + "..."


def _fmt_diff_row(el: "ElementInfo", ref: str | None) -> str:
    ref = ref or ""
    t = el.control_type.replace("Control", "")
    name = _oneline(el.name, 40)
    win = _oneline(el.window_name, 30)
    return f"{ref:<5} {t:<10} {name:<24} [{win}]".rstrip()


def render_snapshot_diff(diff: dict, current: dict) -> str:
    """Render a ``diff_snapshots`` result as markdown.

    ``@eN`` refs are numbered against the CURRENT tree. When an element carries
    ``metadata["_orig_idx"]`` (display-filtered snapshot), that ORIGINAL index is
    used so the ref stays valid against the unfiltered snapshot that
    ``resolve_element`` resolves against — mirroring ``render_snapshot``.
    Added/changed rows carry the ref (clickable now); removed rows carry none
    (the element is gone). Empty sections are omitted. If
    ``diff["stats"]["scope"]`` is present (handler-injected), it is shown in the
    header parenthetically.
    """
    cur_els = current.get("interactive_elements", [])
    # Mirror render_snapshot's numbering path so refs survive a display filter:
    # when --display clips the tree, current elements carry metadata["_orig_idx"]
    # pointing at their ORIGINAL 1-based slot in the unfiltered snapshot that
    # resolve_element resolves against. Falling back to a naive enumerate would
    # renumber from 1 and silently point every @eN at the wrong element.
    def _diff_ref(el: "ElementInfo", i: int) -> str:
        orig = el.metadata.get("_orig_idx") if el.metadata else None
        return f"@e{orig}" if orig else f"@e{i}"

    ref_by_id = {id(el): _diff_ref(el, i) for i, el in enumerate(cur_els, 1)}
    stats = diff["stats"]
    # Scope (e.g. the selected window title) is injected by the handler before
    # render — core has no view of it. The handler's _scope_label carries any
    # quoting (window titles are quoted; "active window"/"full desktop" are
    # not), so render interpolates verbatim. Optional: absent → no parenthetical.
    scope = stats.get("scope")
    scope_part = f' (scope: {scope})' if scope else ""
    base_str = f'baseline: {stats["baseline_n"]} elements{scope_part}'
    parts = ["## Snapshot Diff",
             f'{base_str} → current: {stats["current_n"]}']
    summary = []
    if diff["added"]:
        summary.append(f'+{len(diff["added"])} added')
    if diff["removed"]:
        summary.append(f'-{len(diff["removed"])} removed')
    if diff["changed"]:
        summary.append(f'~{len(diff["changed"])} changed')
    if diff["windows_added"]:
        summary.append(f'+{len(diff["windows_added"])} window')
    if diff.get("windows_changed"):
        summary.append(f'~{len(diff["windows_changed"])} window title')
    parts.append("  ".join(summary) if summary else "no changes")
    parts.append("")

    if diff["added"]:
        parts.append(f'### Added ({len(diff["added"])})')
        for el in diff["added"]:
            parts.append(_fmt_diff_row(el, ref_by_id.get(id(el))))
        parts.append("")
    if diff["removed"]:
        parts.append(f'### Removed ({len(diff["removed"])})')
        for el in diff["removed"]:
            parts.append(_fmt_diff_row(el, None))
        parts.append("")
    if diff["changed"]:
        parts.append(f'### Changed ({len(diff["changed"])})')
        for c in diff["changed"]:
            el = c["element"]
            row = _fmt_diff_row(el, ref_by_id.get(id(el)))
            delta_str = "  ".join(
                f"{f}: {_oneline(o)}→{_oneline(n)}" for f, o, n in c["deltas"])
            parts.append(f"{row}  {delta_str}" if row else delta_str)
        parts.append("")
    if diff["windows_added"]:
        parts.append(f'### Windows +{len(diff["windows_added"])}')
        for w in diff["windows_added"]:
            parts.append(f'"{w.title}" ({w.process_name})')
        parts.append("")
    if diff["windows_removed"]:
        parts.append(f'### Windows -{len(diff["windows_removed"])}')
        for w in diff["windows_removed"]:
            parts.append(f'"{w.title}" ({w.process_name})')
        parts.append("")
    if diff.get("windows_changed"):
        parts.append(f'### Windows ~{len(diff["windows_changed"])} title')
        for c in diff["windows_changed"]:
            w = c["window"]
            old_t, new_t = c["title"]
            parts.append(f'"{old_t}" → "{new_t}" ({w.process_name})')
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"
