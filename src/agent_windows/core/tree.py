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


# ── Control type classification ───────────────────────────────────

INTERACTIVE_TYPES = {
    "ButtonControl", "ListItemControl", "MenuItemControl", "EditControl",
    "CheckBoxControl", "RadioButtonControl", "ComboBoxControl",
    "HyperlinkControl", "SplitButtonControl", "TabItemControl",
    "TreeItemControl", "DataItemControl", "HeaderItemControl",
    "TextBoxControl", "SpinnerControl", "ScrollBarControl",
    "DocumentControl",
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


# ── Window enumeration ────────────────────────────────────────────


def enumerate_windows() -> list[WindowInfo]:
    """List all visible top-level windows."""
    import ctypes
    import ctypes.wintypes

    foreground = ctypes.windll.user32.GetForegroundWindow()
    windows: list[WindowInfo] = []

    # Callback for EnumWindows
    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.wintypes.HWND,
        ctypes.wintypes.LPARAM,
    )

    def _enum_cb(hwnd, _lparam):
        # Skip invisible windows
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True

        # Get window title
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value

        # Get process name
        pid = ctypes.wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_name = get_process_name(pid.value)

        # Get window rect
        rect = ctypes.wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        bbox = BoundingBox(rect.left, rect.top, rect.right, rect.bottom)

        # Skip zero-size windows
        if bbox.area == 0:
            return True

        # Get window class name (locale-invariant, e.g. "Progman" for desktop).
        # Used to skip low-value subtrees; cheaper to fetch here once than to read
        # via UIA ClassNameProperty per-element during traversal.
        cls_buf = ctypes.create_unicode_buffer(257)
        cls_len = ctypes.windll.user32.GetClassNameW(hwnd, cls_buf, 257)
        class_name = cls_buf.value if cls_len else ""

        windows.append(WindowInfo(
            title=title,
            process_name=process_name,
            handle=hwnd,
            bbox=bbox,
            is_active=(hwnd == foreground),
            class_name=class_name,
        ))
        return True

    ctypes.windll.user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    return windows



# ── UIA Tree Traversal ────────────────────────────────────────────


def traverse_tree(
    max_depth: int = 15,
    include_invisible: bool = False,
    include_offscreen: bool = False,
    timeout_per_window: float = 5.0,
    max_elements: int = 200,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Walk the UIA tree and return structured data.

    Bounds (H-04): ``max_depth`` limits descent, ``max_elements`` caps the
    interactive-elements list (traversal stops once it is reached), and
    ``timeout`` is a global wall-clock budget across all windows. Set any
    bound to 0 to disable it.

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

    # Enumerate windows
    all_windows = enumerate_windows()
    active_window = next((w for w in all_windows if w.is_active), None)

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
        ValueError: If selector format is invalid.
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
) -> None:
    """Recursively traverse a UIA node."""
    if depth >= max_depth:
        return
    if time.time() - start_time > timeout:
        return
    if time.time() > global_deadline:
        return
    if max_elements > 0 and len(interactive) >= max_elements:
        return

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
            return
        if is_offscreen and not include_offscreen:
            return

        # Determine if interactive
        is_interactive = control_type in INTERACTIVE_TYPES and is_enabled

        # Get focus state
        is_focused = False
        try:
            is_focused = bool(node.GetCachedPropertyValue(PropertyId.HasKeyboardFocusProperty))
        except Exception:
            logger.debug("Failed to get cached node property", exc_info=True)

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

        # Build tree line
        prefix = " " * indent + "├─ " if depth > 0 else " " * indent
        if depth == 0:
            pass  # Window line already added
        else:
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
            tree_lines.append(f"{prefix}{type_label}{name_label}{bbox_str} {meta_str}".rstrip())

            # Add to interactive elements list
            if is_interactive:
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
                ))

        # Recurse into children. PERF3-05: fetch all children pre-cached in one
        # COM round-trip (``BuildUpdatedCache`` + ``GetCachedChildren``) instead
        # of one ``GetNextSiblingElementBuildCache`` call per child.
        try:
            for child in _get_cached_children(node, cache_request):
                _traverse_node(
                    client, child, tree_lines, interactive,
                    window, max_depth, include_invisible, include_offscreen,
                    indent, depth + 1, start_time, timeout,
                    max_elements, global_deadline, cache_request,
                    in_modal_subtree=this_in_modal,
                )
                if max_elements > 0 and len(interactive) >= max_elements:
                    break
        except Exception as e:
            # Backstop: _get_cached_children and _traverse_node swallow their own
            # errors, so this only fires on something escaping both (e.g. a
            # comtypes marshalling fault mid-iteration). Keeps a single bad node
            # from aborting the whole subtree's siblings.
            logger.debug("Error iterating children at depth %d: %s", depth, e)

    except Exception as e:
        logger.debug("Error traversing node at depth %d: %s", depth, e)


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

    # Windows table
    windows = data.get("windows", [])
    if windows:
        parts.append("## Windows")
        parts.append("| # | Title | App | Status | Position |")
        parts.append("|---|-------|-----|--------|----------|")
        for i, w in enumerate(windows, 1):
            status = "active" if w.is_active else ""
            pos = str(w.bbox) if w.bbox else "N/A"
            parts.append(f"| {i} | {w.title[:40]} | {w.process_name} | {status} | {pos} |")
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
        parts.append("| Ref | Type | Name | Window | Position | State |")
        parts.append("|-----|------|------|---------|----------|-------|")
        for i, el in enumerate(elements, 1):
            orig_idx = el.metadata.get("_orig_idx") if el.metadata else None
            ref = f"@e{orig_idx}" if orig_idx else f"@e{i}"
            type_name = el.control_type.replace("Control", "")
            name = el.name[:40] if el.name else ""
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
            state_str = ", ".join(states)
            parts.append(f"| {ref} | {type_name} | {name} | {window_name} | {pos} | {state_str} |")
        parts.append("")

    return "\n".join(parts)
