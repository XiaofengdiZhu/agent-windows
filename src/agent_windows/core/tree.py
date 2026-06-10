"""UIA accessibility tree traversal.

Walks the Windows UIA tree, classifies elements, and renders semantic text
output for AI agent consumption.

Simplified from Windows-MCP's tree service — no browser DOM/IA2 handling yet.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Control type classification ───────────────────────────────────

INTERACTIVE_TYPES = {
    "ButtonControl", "ListItemControl", "MenuItemControl", "EditControl",
    "CheckBoxControl", "RadioButtonControl", "ComboBoxControl",
    "HyperlinkControl", "SplitButtonControl", "TabItemControl",
    "TreeItemControl", "DataItemControl", "HeaderItemControl",
    "TextBoxControl", "SpinnerControl", "ScrollBarControl",
    "DocumentControl",
}

STRUCTURAL_TYPES = {
    "PaneControl", "GroupControl", "CustomControl",
    "ToolBarControl", "TabControl", "MenuBarControl",
}

INFORMATIVE_TYPES = {
    "TextControl", "StatusBarControl",
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

    @property
    def ref_id(self) -> str:
        """Stable reference ID for this element."""
        return f"@e{id(self) & 0xFFFF:03x}"


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
        process_name = _get_process_name(pid.value)

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


def _get_process_name(pid: int) -> str:
    """Get process executable name from PID."""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        import ctypes.wintypes
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.wintypes.DWORD(260)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        import os
        return os.path.basename(buf.value)
    except Exception:
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


# ── UIA Tree Traversal ────────────────────────────────────────────


def traverse_tree(
    max_depth: int = 30,
    include_invisible: bool = False,
    include_offscreen: bool = False,
    timeout_per_window: float = 5.0,
) -> dict[str, Any]:
    """Walk the UIA tree and return structured data.

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

    for win in all_windows:
        win_marker = " [active]" if win.is_active else ""
        tree_lines.append(f'  └─ Window "{win.title}" ({win.process_name}){win_marker}')

        try:
            _traverse_window(client, win, tree_lines, interactive_elements,
                           max_depth=max_depth, include_invisible=include_invisible,
                           include_offscreen=include_offscreen, indent=4,
                           timeout=timeout_per_window)
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
) -> None:
    """Traverse UIA tree for a single window."""
    start = time.time()

    try:
        root = client.IUIAutomation.ElementFromHandle(window.handle)
    except Exception as e:
        logger.debug("ElementFromHandle failed for %s: %s", window.title, e)
        return

    if root is None:
        return

    _traverse_node(
        client, root, tree_lines, interactive,
        window, max_depth, include_invisible, include_offscreen,
        indent, 0, start, timeout,
    )


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
) -> None:
    """Recursively traverse a UIA node."""
    if depth >= max_depth:
        return
    if time.time() - start_time > timeout:
        return

    try:
        # Get basic properties
        name = ""
        try:
            name = node.CurrentName or ""
        except Exception:
            pass

        control_type = ""
        try:
            ct = node.CurrentControlType
            # Convert control type int to name
            control_type = _control_type_name(ct)
        except Exception:
            pass

        is_enabled = True
        try:
            is_enabled = node.CurrentIsEnabled
        except Exception:
            pass

        is_offscreen = False
        try:
            is_offscreen = node.CurrentIsOffscreen
        except Exception:
            pass

        # Get bounding rectangle
        bbox = None
        try:
            rect = node.CurrentBoundingRectangle
            bbox = BoundingBox(
                left=int(rect.left),
                top=int(rect.top),
                right=int(rect.right),
                bottom=int(rect.bottom),
            )
        except Exception:
            pass

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
            is_focused = node.CurrentHasKeyboardFocus
        except Exception:
            pass

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

        # Recurse into children
        try:
            walker = client.ViewWalker
            child = walker.GetFirstChildElement(node)
            while child:
                _traverse_node(
                    client, child, tree_lines, interactive,
                    window, max_depth, include_invisible, include_offscreen,
                    indent, depth + 1, start_time, timeout,
                )
                child = walker.GetNextSiblingElement(child)
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
        # Build the map from the enums module
        try:
            from agent_windows.uia.core import ControlType
            for attr in dir(ControlType):
                if attr.startswith("UIA_") and attr.endswith("ControlTypeId"):
                    name = attr[4:-14] + "Control"  # e.g. "ButtonControl"
                    _CONTROL_TYPE_MAP[getattr(ControlType, attr)] = name
        except Exception:
            # Fallback: common types
            _CONTROL_TYPE_MAP = {
                50000: "ButtonControl", 50001: "CalendarControl",
                50002: "CheckBoxControl", 50003: "ComboBoxControl",
                50004: "EditControl", 50005: "HyperlinkControl",
                50006: "ImageControl", 50007: "ListItemControl",
                50008: "ListControl", 50009: "MenuBarControl",
                50010: "MenuControl", 50011: "MenuItemControl",
                50012: "ProgressBarControl", 50013: "RadioButtonControl",
                50014: "ScrollBarControl", 50015: "SliderControl",
                50016: "SpinnerControl", 50017: "StatusBarControl",
                50018: "TabControl", 50019: "TabItemControl",
                50020: "TextControl", 50021: "ToolBarControl",
                50022: "ToolTipControl", 50023: "TreeControl",
                50024: "TreeItemControl", 50025: "CustomControl",
                50026: "GroupControl", 50027: "ThumbControl",
                50028: "DataGridControl", 50029: "DataItemControl",
                50030: "DocumentControl", 50031: "SplitButtonControl",
                50032: "WindowControl", 50033: "PaneControl",
                50034: "HeaderControl", 50035: "HeaderItemControl",
                50036: "TableControl", 50037: "TitleBarControl",
                50038: "SeparatorControl", 50039: "SemanticZoomControl",
                50040: "AppBarControl",
            }

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
