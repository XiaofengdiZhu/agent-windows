"""Mouse/keyboard input controller using Win32 API.

Uses mouse_event for clicks, SendInput for Unicode text, keybd_event for shortcuts.
All coordinates are in physical pixels (DPI-aware).
"""

import ctypes
import ctypes.wintypes
import time
import logging

# The ctypes INPUT/MOUSEINPUT/KEYBDINPUT/_INPUTUnion structures are reused from
# the vendored uia.enums module rather than redefined here — the canonical Win32
# definition (which also carries HARDWAREINPUT) lives there, and MOUSEINPUT is
# the largest union member so sizeof(INPUT) is identical. core → uia.enums is the
# same established direction tree.py already uses. (QUAL-07)
from agent_windows.uia.enums import MOUSEINPUT, KEYBDINPUT, _INPUTUnion, INPUT

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

# DoS bounds for animation/scroll loops. The handler layer validates inputs,
# but these primitives must also self-bound: a runaway ``amount`` (scroll_at) or
# ``step`` (move_to) ties up the single-threaded STA worker with millions of
# set_cursor_pos/mouse_event + sleep iterations. (F11)
MAX_SCROLL_STEPS = 1000  # wheel notches per scroll_at call; 1000×120 delta ≈ a few hundred pages
MAX_MOVE_STEPS = 1000    # interpolated cursor positions per move_to; beyond this, motion is imperceptible

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


# ── Low-level API wrappers ────────────────────────────────────────

user32 = ctypes.windll.user32


def _get_virtual_screen() -> tuple[int, int, int, int]:
    """Get virtual screen rect (left, top, width, height) covering all monitors."""
    left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
    top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
    width = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
    height = user32.GetSystemMetrics(79) # SM_CYVIRTUALSCREEN
    return left, top, width, height


def validate_coordinates(x, y) -> tuple[int, int]:
    """Validate and coerce input coordinates, checking the virtual-screen bounds.

    Accepts int/float (float is truncated to int, matching mouse_event's integer
    pixel grid). Rejects non-numeric types and out-of-bounds values with a
    ``CoordinateRangeError`` (→ COORDINATE_OUT_OF_RANGE). (SEC3-02)
    """
    from agent_windows.daemon.router import CoordinateRangeError

    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise CoordinateRangeError(
            f"coordinate 'x' must be a number, got {type(x).__name__}")
    if isinstance(y, bool) or not isinstance(y, (int, float)):
        raise CoordinateRangeError(
            f"coordinate 'y' must be a number, got {type(y).__name__}")

    xi, yi = int(x), int(y)
    vl, vt, vw, vh = _get_virtual_screen()
    # Allow a 1px grace on each edge so clicking the very last pixel of a
    # monitor still works despite rounding; reject wildly out-of-range values.
    if xi < vl - 1 or xi > vl + vw or yi < vt - 1 or yi > vt + vh:
        raise CoordinateRangeError(
            f"coordinate ({xi}, {yi}) is outside the virtual screen "
            f"({vl},{vt})-({vl + vw},{vt + vh})")
    return xi, yi


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

    _, _, sw, sh = _get_virtual_screen()
    step = max(1, int(distance / max(sw, sh) * 10))
    # Cap the interpolation count so a directly-invoked move_to with an
    # out-of-screen coordinate (bypassing the handler's validate_coordinates)
    # can't produce a runaway step → millions of set_cursor_pos + sleep
    # iterations. When coordinates ARE validated (the handler path), distance
    # is already screen-bounded so step is tiny — this only bites the bypass.
    # (F11)
    step = min(step, MAX_MOVE_STEPS)
    interval = min(1.0 / speed, 0.05) / max(step, 1)

    for i in range(1, step + 1):
        nx = int(cx + (x - cx) * i / step)
        ny = int(cy + (y - cy) * i / step)
        set_cursor_pos(nx, ny)
        time.sleep(interval)

    set_cursor_pos(x, y)


def hover(x: int, y: int, wait_time: float = 0.3) -> None:
    """Move mouse to (x, y) and trigger hover event.

    Unlike move_to(), this also sends a MOUSE_MOVE event via mouse_event,
    ensuring WM_MOUSEMOVE reaches the target window to trigger tooltips,
    hover highlights, etc.
    """
    set_cursor_pos(x, y)
    nx, ny = _normalize(x, y)
    _send_mouse_event(MOUSE_MOVE | MOUSE_ABSOLUTE, nx, ny, 0)
    time.sleep(wait_time)


def mouse_down(x: int, y: int, button: str = "left") -> None:
    """Press and hold mouse button at (x, y) without releasing."""
    if button not in ("left", "right", "middle"):
        raise ValueError(f"Invalid button: {button!r}. Must be 'left', 'right', or 'middle'.")
    set_cursor_pos(x, y)
    nx, ny = _normalize(x, y)
    flag = {"right": MOUSE_RIGHTDOWN, "middle": MOUSE_MIDDLEDOWN}.get(button, MOUSE_LEFTDOWN)
    _send_mouse_event(flag | MOUSE_ABSOLUTE, nx, ny, 0)


def mouse_up(x: int, y: int, button: str = "left") -> None:
    """Release mouse button at (x, y)."""
    if button not in ("left", "right", "middle"):
        raise ValueError(f"Invalid button: {button!r}. Must be 'left', 'right', or 'middle'.")
    set_cursor_pos(x, y)
    nx, ny = _normalize(x, y)
    flag = {"right": MOUSE_RIGHTUP, "middle": MOUSE_MIDDLEUP}.get(button, MOUSE_LEFTUP)
    _send_mouse_event(flag | MOUSE_ABSOLUTE, nx, ny, 0)


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

    # Bound the loop: amount is a count of wheel notches, so clamping is safe
    # (shrinking it can't misdirect input the way an unbounded position offset
    # could). Coerce to int and clamp to [0, MAX_SCROLL_STEPS] so a runaway
    # amount (1e9) can't spin the single-threaded daemon for days. The handler
    # layer doesn't cap amount today, so this primitive is the floor. (F11)
    amount = max(0, min(int(amount), MAX_SCROLL_STEPS))

    for _ in range(amount):
        _send_mouse_event(MOUSE_WHEEL, 0, 0, delta)
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
    """Send a single Unicode character via SendInput.

    Win32 KEY_UNICODE takes a UTF-16 *code unit* in wScan, so a character
    above U+FFFF (emoji, CJK Ext-B, …) must be sent as a high+low surrogate
    pair — two KEY_UNICODE events per such character. Splitting the character
    into UTF-16 code units handles both the BMP (1 unit) and supplementary
    (2 units) planes uniformly.
    """
    encoded = char.encode("utf-16-le")
    inputs = []
    for i in range(0, len(encoded), 2):
        code = int.from_bytes(encoded[i:i + 2], "little")
        inputs.append(_key_input(0, code, KEY_UNICODE | KEY_DOWN))
        inputs.append(_key_input(0, code, KEY_UNICODE | KEY_UP))
    _send_input(*inputs)


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


def clear_field() -> None:
    """Clear the focused text field via Ctrl+A then Delete."""
    shortcut("ctrl+a")
    time.sleep(0.05)
    _send_vk_key(VK_DELETE)
    time.sleep(0.05)


def move_caret(caret_position: str) -> None:
    """Move the text caret in the focused field. (TODO #16)

    ``caret_position`` is one of ``"start"`` / ``"end"`` / ``"idle"``. ``"idle"``
    (the default elsewhere) leaves the caret where the platform put it after a
    click; the other two press Home / End respectively. Home/End have no
    destructive effect on a non-text field (they may scroll or move focus in
    some controls, but won't alter data).
    """
    if caret_position == "start":
        _send_vk_key(VK_HOME)
    elif caret_position == "end":
        _send_vk_key(VK_END)
    # "idle" (or anything else) → no-op; validation lives at the handler layer.


def type_text_at(text: str, x: int, y: int, clear: bool = False,
                 enter: bool = False, caret_position: str = "idle") -> None:
    """Click at position, optionally move caret, optionally clear, then type.

    Order matches the reference implementation (TODO #16): click → caret
    (Home/End) → clear → type → Enter. ``caret_position`` defaults to
    ``"idle"`` (no caret move) so existing callers are unchanged.
    """
    click(x, y)
    move_caret(caret_position)

    if clear:
        clear_field()

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

    # Press modifier keys (hold them), press the final key, then release
    # modifiers in reverse order. The whole press→final→release sequence is
    # wrapped in try/finally so an exception (ctypes OSError, a Ctrl+C, a
    # handler timeout) can't leave a modifier (Ctrl/Shift/Alt/Win) stuck down
    # — which would corrupt every subsequent input on the desktop. multi_select
    # (below) has the same guard for the same reason.
    held_vks: list[int] = []
    try:
        for name, vk in resolved[:-1]:
            _press_key(vk)
            held_vks.append(vk)
            time.sleep(0.01)

        # Press the final key
        final_name, final_vk = resolved[-1]
        _send_vk_key(final_vk)
        time.sleep(0.01)
    finally:
        # Release modifiers in reverse order (best-effort: each release is
        # independent, so one failing release still attempts the rest).
        for vk in reversed(held_vks):
            try:
                _release_key(vk)
            except Exception:
                logger.warning("Failed to release modifier key %s", vk, exc_info=True)
            time.sleep(0.01)

    time.sleep(wait_time)


# ── Batch operations (TODO #12) ────────────────────────────────────


def multi_select(press_ctrl: bool, locs: list[tuple[int, int]]) -> None:
    """Click multiple points in sequence. (TODO #12)

    With ``press_ctrl=True``, Ctrl is held down across all clicks so each
    click toggles selection (file multi-select, checkbox groups, …) rather
    than activating. With ``press_ctrl=False``, each click is independent
    (equivalent to a sequence of plain ``click`` calls, but in one call).

    Args:
        press_ctrl: Hold Ctrl across the whole sequence.
        locs: Screen coordinates to click, as ``(x, y)`` tuples.
    """
    if press_ctrl:
        _press_key(VK_CONTROL)
        time.sleep(0.05)
    try:
        for x, y in locs:
            click(x, y)
            time.sleep(0.2)
    finally:
        # Always release Ctrl, even if a click raised — otherwise the desktop
        # is stuck with Ctrl held (every subsequent click toggles selection).
        if press_ctrl:
            _release_key(VK_CONTROL)
            time.sleep(0.05)


def multi_edit(locs: list[tuple[int, int, str]]) -> None:
    """Type text into multiple fields in sequence. (TODO #12)

    Each entry is ``(x, y, text)``: click the field, clear it, type the text.
    Equivalent to a sequence of ``type_text_at(text, x, y, clear=True)`` but in
    one call — for filling out a form in one shot.
    """
    for x, y, text in locs:
        type_text_at(text, x, y, clear=True)
