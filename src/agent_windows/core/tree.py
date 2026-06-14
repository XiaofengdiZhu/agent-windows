"""UIA accessibility tree traversal.

Walks the Windows UIA tree, classifies elements, and renders semantic text
output for AI agent consumption.

Simplified from Windows-MCP's tree service — no browser DOM/IA2 handling yet.
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from agent_windows.utils.win32 import get_process_name
from agent_windows.uia.enums import PropertyId

logger = logging.getLogger(__name__)

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
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowInfo:
    """Information about a top-level window."""
    title: str
    process_name: str
    handle: int
    bbox: BoundingBox | None
    is_active: bool = False


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

        windows.append(WindowInfo(
            title=title,
            process_name=process_name,
            handle=hwnd,
            bbox=bbox,
            is_active=(hwnd == foreground),
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
        - resolution: (width, height) virtual screen
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

    for win in all_windows:
        if time.time() > global_deadline:
            logger.info(
                "Tree traversal stopped: global timeout %.1fs reached", timeout
            )
            break

        win_marker = " [active]" if win.is_active else ""
        tree_lines.append(f'  └─ Window "{win.title}" ({win.process_name}){win_marker}')

        try:
            _traverse_window(client, win, tree_lines, interactive_elements,
                           max_depth=max_depth, include_invisible=include_invisible,
                           include_offscreen=include_offscreen, indent=4,
                           timeout=timeout_per_window, max_elements=max_elements,
                           global_deadline=global_deadline)
        except Exception as e:
            logger.warning("Failed to traverse window '%s': %s", win.title, e)
            tree_lines.append(f"      [traversal error: {e}]")

    return {
        "cursor": cursor,
        "resolution": (primary_w, primary_h),
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
) -> None:
    """Traverse UIA tree for a single window."""
    start = time.time()

    # M-25: fetch the per-node properties through a UIA CacheRequest so each
    # element arrives WITH its properties already cached (one cross-process
    # round-trip at retrieval) instead of six separate live CurrentX round-trips
    # per node. Built per window — CreateCacheRequest + AddProperty is negligible
    # next to the traversal itself.
    cache_request = _build_traversal_cache(client)

    try:
        root = client.IUIAutomation.ElementFromHandleBuildCache(window.handle, cache_request)
    except Exception as e:
        logger.debug("ElementFromHandleBuildCache failed for %s: %s", window.title, e)
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
    the tree walker's ``*BuildCache`` overloads) arrive with these properties
    already cached, so reading them via ``GetCachedPropertyValue`` is a local
    lookup rather than a live cross-process COM round-trip.
    ``AutomationElementMode`` is left at its default (Full) so cached elements
    stay navigable for the recursive walk.
    """
    cache = client.IUIAutomation.CreateCacheRequest()
    cache.AddProperty(PropertyId.NameProperty)
    cache.AddProperty(PropertyId.ControlTypeProperty)
    cache.AddProperty(PropertyId.IsEnabledProperty)
    cache.AddProperty(PropertyId.IsOffscreenProperty)
    cache.AddProperty(PropertyId.BoundingRectangleProperty)
    cache.AddProperty(PropertyId.HasKeyboardFocusProperty)
    return cache


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
                ))

        # Recurse into children — each child is fetched through the cache request
        # so it arrives with its properties already cached (M-25).
        try:
            walker = client.ViewWalker
            child = walker.GetFirstChildElementBuildCache(node, cache_request)
            while child:
                _traverse_node(
                    client, child, tree_lines, interactive,
                    window, max_depth, include_invisible, include_offscreen,
                    indent, depth + 1, start_time, timeout,
                    max_elements, global_deadline, cache_request,
                )
                child = walker.GetNextSiblingElementBuildCache(child, cache_request)
        except Exception as e:
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
    parts.append(f"Cursor: ({cursor[0]}, {cursor[1]})")
    parts.append(f"Resolution: {res[0]}x{res[1]}")
    parts.append(f"Displays: {displays}")
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

    # Semantic tree
    tree = data.get("semantic_tree", "")
    if tree:
        parts.append("## Accessibility Tree")
        parts.append(tree)
        parts.append("")

    # Interactive elements table
    elements = data.get("interactive_elements", [])
    if elements:
        parts.append("## Interactive Elements")
        parts.append("| Ref | Type | Name | Position | State |")
        parts.append("|-----|------|------|----------|-------|")
        for i, el in enumerate(elements, 1):
            ref = f"@e{i}"
            type_name = el.control_type.replace("Control", "")
            name = el.name[:40] if el.name else ""
            pos = str(el.bbox) if el.bbox else "N/A"
            states = []
            if el.is_focused:
                states.append("focused")
            if not el.is_enabled:
                states.append("disabled")
            state_str = ", ".join(states)
            parts.append(f"| {ref} | {type_name} | {name} | {pos} | {state_str} |")
        parts.append("")

    return "\n".join(parts)
