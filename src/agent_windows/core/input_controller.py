"""Mouse/keyboard input controller using Win32 API.

Uses mouse_event for clicks, SendInput for Unicode text, keybd_event for shortcuts.
All coordinates are in physical pixels (DPI-aware).
"""

import ctypes
import ctypes.wintypes
import time
import logging

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────

WHEEL_DELTA = 120

# MouseEventFlag
MOUSE_MOVE = 0x0001
MOUSE_LEFTDOWN = 0x0002
MOUSE_LEFTUP = 0x0004
MOUSE_RIGHTDOWN = 0x0008
MOUSE_RIGHTUP = 0x0010
MOUSE_MIDDLEDOWN = 0x0020
MOUSE_MIDDLEUP = 0x0040
MOUSE_WHEEL = 0x0800
MOUSE_ABSOLUTE = 0x8000

# KeyboardEventFlag
KEY_DOWN = 0x0000
KEY_EXTENDED = 0x0001
KEY_UP = 0x0002
KEY_UNICODE = 0x0004

# InputType
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

# Virtual key codes
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # ALT
VK_LWIN = 0x5B
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_SPACE = 0x20
VK_DELETE = 0x2E
VK_HOME = 0x24
VK_END = 0x23
VK_PRIOR = 0x21  # Page Up
VK_NEXT = 0x22   # Page Down
VK_LEFT = 0x25
VK_UP = 0x26
VK_RIGHT = 0x27
VK_DOWN = 0x28
VK_INSERT = 0x2D
VK_SNAPSHOT = 0x2C  # Print Screen
VK_APPS = 0x5D     # Context menu
VK_F1 = 0x70
VK_F12 = 0x7B

# Key name aliases
KEY_ALIASES: dict[str, str] = {
    "ctrl": "Ctrl", "control": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
    "win": "Win", "windows": "Win", "super": "Win",
    "enter": "Enter", "return": "Enter",
    "tab": "Tab",
    "escape": "Esc", "esc": "Esc",
    "backspace": "Back", "back": "Back",
    "delete": "Delete", "del": "Delete",
    "insert": "Insert",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp", "page_up": "PageUp", "pgup": "PageUp",
    "pagedown": "PageDown", "page_down": "PageDown", "pgdn": "PageDown",
    "space": "Space",
    "printscreen": "PrintScreen", "prtsc": "PrintScreen",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "apps": "Apps", "menu": "Apps", "context": "Apps",
    # Function keys (F1–F12)
    **{f"f{i}": f"F{i}" for i in range(1, 13)},
}

# Special key name → VK code
SPECIAL_KEYS: dict[str, int] = {
    "Ctrl": VK_CONTROL, "Shift": VK_SHIFT, "Alt": VK_MENU, "Win": VK_LWIN,
    "Enter": VK_RETURN, "Tab": VK_TAB, "Esc": VK_ESCAPE,
    "Back": VK_BACK, "Delete": VK_DELETE, "Insert": VK_INSERT,
    "Home": VK_HOME, "End": VK_END,
    "PageUp": VK_PRIOR, "PageDown": VK_NEXT,
    "Space": VK_SPACE, "PrintScreen": VK_SNAPSHOT, "Apps": VK_APPS,
    "Up": VK_UP, "Down": VK_DOWN, "Left": VK_LEFT, "Right": VK_RIGHT,
    # OEM keys (physical keyboard keys, not ASCII codes)
    "=": 0xBB, "+": 0xBB,   # VK_OEM_PLUS
    "-": 0xBD, "_": 0xBD,   # VK_OEM_MINUS
    ",": 0xBC, "<": 0xBC,   # VK_OEM_COMMA
    ".": 0xBE, ">": 0xBE,   # VK_OEM_PERIOD
    "/": 0xBF, "?": 0xBF,   # VK_OEM_2
    ";": 0xBA, ":": 0xBA,   # VK_OEM_1
    "'": 0xDE, '"': 0xDE,   # VK_OEM_7
    "[": 0xDB, "{": 0xDB,   # VK_OEM_4
    "]": 0xDD, "}": 0xDD,   # VK_OEM_6
    "\\": 0xDC, "|": 0xDC,  # VK_OEM_5
    "`": 0xC0, "~": 0xC0,   # VK_OEM_3
    "F1": VK_F1, "F2": 0x71, "F3": 0x72, "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76, "F8": 0x77,
    "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
}

# Hold keys (modifier keys that stay pressed until released)
HOLD_KEYS = {"Ctrl", "Shift", "Alt", "Win"}


# ── ctypes structures ─────────────────────────────────────────────


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.wintypes.PULONG),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.wintypes.PULONG),
    )


class _INPUTUnion(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", ctypes.wintypes.DWORD), ("union", _INPUTUnion))


# ── Low-level API wrappers ────────────────────────────────────────

user32 = ctypes.windll.user32


def _get_virtual_screen() -> tuple[int, int, int, int]:
    """Get virtual screen rect (left, top, width, height) covering all monitors."""
    left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
    width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    height = user32.GetSystemMetrics(79) # SM_CYVIRTUALSCREEN
    return left, top, width, height


def _normalize(x: int, y: int) -> tuple[int, int]:
    """Convert pixel coordinates to normalized absolute coordinates (0-65535).

    Uses virtual screen dimensions for multi-monitor support.
    Formula: (x - vleft) * 65535 / (vwidth - 1)
    """
    vl, vt, vw, vh = _get_virtual_screen()
    nx = (x - vl) * 65535 // max(vw - 1, 1)
    ny = (y - vt) * 65535 // max(vh - 1, 1)
    return nx, ny


def _send_mouse_event(flags: int, x: int = 0, y: int = 0, data: int = 0) -> None:
    """Send a mouse event via mouse_event API."""
    user32.mouse_event(flags, x, y, data, 0)


def _send_input(*inputs: INPUT) -> int:
    """Send INPUT structures atomically via a single SendInput call."""
    n = len(inputs)
    if n == 0:
        return 0
    cb_size = ctypes.c_int(ctypes.sizeof(INPUT))
    arr = (INPUT * n)(*inputs)
    return user32.SendInput(n, arr, cb_size)


def _mouse_input(dx: int, dy: int, data: int, flags: int) -> INPUT:
    return INPUT(INPUT_MOUSE, _INPUTUnion(mi=MOUSEINPUT(dx, dy, data, flags, 0, None)))


def _key_input(vk: int, scan: int, flags: int) -> INPUT:
    return INPUT(INPUT_KEYBOARD, _INPUTUnion(ki=KEYBDINPUT(vk, scan, flags, 0, None)))


# ── Mouse operations ──────────────────────────────────────────────


def get_cursor_pos() -> tuple[int, int]:
    """Get current cursor position."""
    point = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def set_cursor_pos(x: int, y: int) -> bool:
    """Set cursor position."""
    return bool(user32.SetCursorPos(x, y))


def move_to(x: int, y: int, speed: float = 10.0) -> None:
    """Move cursor to (x, y) with animation."""
    cx, cy = get_cursor_pos()
    distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5

    if distance < 1:
        set_cursor_pos(x, y)
        return

    sw, sh = _get_screen_size()
    step = max(1, int(distance / max(sw, sh) * 10))
    interval = min(1.0 / speed, 0.05) / max(step, 1)

    for i in range(1, step + 1):
        nx = int(cx + (x - cx) * i / step)
        ny = int(cy + (y - cy) * i / step)
        set_cursor_pos(nx, ny)
        time.sleep(interval)

    set_cursor_pos(x, y)


def click(x: int, y: int, right: bool = False, double: bool = False,
          wait_time: float = 0.3) -> None:
    """Click at (x, y). Supports right-click and double-click."""
    set_cursor_pos(x, y)
    nx, ny = _normalize(x, y)
    down_flag = MOUSE_RIGHTDOWN if right else MOUSE_LEFTDOWN
    up_flag = MOUSE_RIGHTUP if right else MOUSE_LEFTUP

    clicks = 2 if double else 1
    for _ in range(clicks):
        _send_mouse_event(down_flag | MOUSE_ABSOLUTE, nx, ny, 0)
        time.sleep(0.05)
        _send_mouse_event(up_flag | MOUSE_ABSOLUTE, nx, ny, 0)
        time.sleep(0.05)

    time.sleep(wait_time)


def scroll_at(x: int, y: int, direction: str = "down", amount: int = 3,
              wait_time: float = 0.3) -> None:
    """Scroll at position (x, y)."""
    set_cursor_pos(x, y)
    delta = -WHEEL_DELTA if direction == "down" else WHEEL_DELTA

    for _ in range(amount):
        _send_mouse_event(MOUSE_WHEEL, 0, 0, delta, 0)
        time.sleep(0.05)

    time.sleep(wait_time)


def drag(x1: int, y1: int, x2: int, y2: int, right: bool = False,
         wait_time: float = 0.3) -> None:
    """Drag from (x1,y1) to (x2,y2)."""
    set_cursor_pos(x1, y1)
    nx1, ny1 = _normalize(x1, y1)
    down_flag = MOUSE_RIGHTDOWN if right else MOUSE_LEFTDOWN
    up_flag = MOUSE_RIGHTUP if right else MOUSE_LEFTUP

    # Press
    _send_mouse_event(down_flag | MOUSE_ABSOLUTE, nx1, ny1, 0)
    time.sleep(0.05)

    # Move to target
    move_to(x2, y2, speed=5.0)
    time.sleep(0.05)

    # Release
    nx2, ny2 = _normalize(x2, y2)
    _send_mouse_event(up_flag | MOUSE_ABSOLUTE, nx2, ny2, 0)
    time.sleep(wait_time)


# ── Keyboard operations ───────────────────────────────────────────


def _send_unicode_char(char: str) -> None:
    """Send a single Unicode character via SendInput."""
    code = ord(char)
    _send_input(
        _key_input(0, code, KEY_UNICODE | KEY_DOWN),
        _key_input(0, code, KEY_UNICODE | KEY_UP),
    )


# Extended keys require KEY_EXTENDED flag for correct input.
# Navigation keys, Edit keys, and Nav keys are all extended.
# See: https://learn.microsoft.com/en-us/windows/win32/inputdev/virtual-key-codes
_EXTENDED_VKS = frozenset({
    VK_HOME, VK_END, VK_PRIOR, VK_NEXT,  # Nav: Home, End, PgUp, PgDn
    VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN,    # Arrow keys
    VK_INSERT, VK_DELETE,                  # Edit keys: Insert, Delete
})


def _is_extended_key(vk: int) -> bool:
    """Check if a virtual key code is an extended key."""
    return vk in _EXTENDED_VKS


def _send_vk_key(vk: int, scan: int = 0) -> None:
    """Send a virtual key press (down + up) via keybd_event."""
    flags = KEY_EXTENDED if _is_extended_key(vk) else 0
    if not scan:
        scan = user32.MapVirtualKeyA(vk, 0)
    user32.keybd_event(vk, scan, flags, 0)
    time.sleep(0.01)
    user32.keybd_event(vk, scan, flags | KEY_UP, 0)


def _press_key(vk: int) -> None:
    """Press and hold a key."""
    flags = KEY_EXTENDED if _is_extended_key(vk) else 0
    scan = user32.MapVirtualKeyA(vk, 0)
    user32.keybd_event(vk, scan, flags, 0)


def _release_key(vk: int) -> None:
    """Release a held key."""
    flags = KEY_EXTENDED if _is_extended_key(vk) else 0
    scan = user32.MapVirtualKeyA(vk, 0)
    user32.keybd_event(vk, scan, flags | KEY_UP, 0)


def type_text(text: str, interval: float = 0.02, wait_time: float = 0.3) -> None:
    """Type text using Unicode direct input (bypasses IME).

    Sends each character via SendInput with KEY_UNICODE flag,
    ensuring reliable input for any language including CJK.
    """
    for char in text:
        _send_unicode_char(char)
        time.sleep(interval)
    time.sleep(wait_time)


def type_text_at(text: str, x: int, y: int, clear: bool = False,
                 enter: bool = False) -> None:
    """Click at position, optionally clear, then type text."""
    click(x, y)

    if clear:
        # Select all (Ctrl+A) then delete
        shortcut("ctrl+a")
        time.sleep(0.05)
        _send_vk_key(VK_DELETE)
        time.sleep(0.05)

    type_text(text)

    if enter:
        _send_vk_key(VK_RETURN)


def shortcut(keys: str, wait_time: float = 0.3) -> None:
    """Send a keyboard shortcut (e.g. 'ctrl+c', 'alt+f4').

    Parses the shortcut string, holds modifier keys, presses the main key,
    then releases modifiers in reverse order.
    """
    parts = [p.strip() for p in keys.split("+")]
    if not parts:
        return

    # Resolve key names
    resolved: list[tuple[str, int]] = []
    for part in parts:
        name = KEY_ALIASES.get(part.lower(), part)
        # Capitalize first letter for display
        if len(name) == 1:
            name = name.upper()

        vk = SPECIAL_KEYS.get(name)
        if vk is None:
            # Try single character
            if len(name) == 1:
                vk = ord(name.upper())
            else:
                raise ValueError(f"Unknown key: {part}")

        resolved.append((name, vk))

    # Press modifier keys (hold them)
    held_vks: list[int] = []
    for name, vk in resolved[:-1]:
        _press_key(vk)
        held_vks.append(vk)
        time.sleep(0.01)

    # Press the final key
    final_name, final_vk = resolved[-1]
    _send_vk_key(final_vk)
    time.sleep(0.01)

    # Release modifiers in reverse order
    for vk in reversed(held_vks):
        _release_key(vk)
        time.sleep(0.01)

    time.sleep(wait_time)
