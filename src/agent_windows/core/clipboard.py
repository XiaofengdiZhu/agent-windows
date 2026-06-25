"""Clipboard operations: get/set via the native Win32 clipboard API, with an
optional keyboard (Ctrl+C/V) mode.

The system path uses ``win32clipboard`` directly (``OpenClipboard`` /
``GetClipboardData(CF_UNICODETEXT)`` / ``EmptyClipboard`` +
``SetClipboardData``) instead of spawning a ``powershell`` process per call
(~100-300ms latency saved). It also distinguishes "empty" from "non-text"
content: a clipboard with no ``CF_UNICODETEXT`` available reports "non-text",
while one with an empty string reports "empty". (TODO #17)

The ``keyboard=True`` mode (Ctrl+C to copy a selection before reading, Ctrl+V
to paste after setting) is retained — it's a capability the reference lacks.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# Win32 clipboard access can fail when another app holds the clipboard open
# (transient) or the API is unavailable (non-Windows). Retry a few times with
# a short backoff before giving up — the "clipboard locked" race is common and
# almost always resolves within milliseconds.
_OPEN_RETRIES = 5
_OPEN_RETRY_DELAY = 0.01


def _open_clipboard():
    """Open the clipboard with retries, returning the win32clipboard handle.

    ``OpenClipboard`` fails if another window has it open; clipboard-aware apps
    grab-and-release it constantly, so a couple of quick retries usually win.
    Raises ``RuntimeError`` (chained) if it can't be acquired.

    The ``except Exception`` is deliberate: pywin32 raises ``pywintypes.error``
    (a subclass of Exception) on the lock race, and importing pywin32 on
    non-Windows raises ImportError — both must trigger a retry/give-up, and we
    don't want to import pywin32 at module load (latency). The body is a single
    ``OpenClipboard()`` call, so the masked-error surface is tiny.
    """
    import time

    import win32clipboard

    last_err = None
    for _ in range(_OPEN_RETRIES):
        try:
            win32clipboard.OpenClipboard()
            return win32clipboard
        except Exception as e:  # pywintypes.error (lock race) or ImportError
            last_err = e
            time.sleep(_OPEN_RETRY_DELAY)
    raise RuntimeError(f"Could not open clipboard after {_OPEN_RETRIES} tries") from last_err


def get_clipboard(keyboard: bool = False) -> dict[str, Any]:
    """Get clipboard text content.

    Args:
        keyboard: If True, simulate Ctrl+C first to copy the current selection,
            then read the clipboard via the native API.

    Returns:
        Dict with ``output`` (always) and ``text`` (only when there's text).
        An empty clipboard and a non-text clipboard (e.g. an image) are reported
        distinctly. (TODO #17)
    """
    if keyboard:
        from agent_windows.core.input_controller import shortcut
        shortcut("ctrl+c")
        import time
        time.sleep(0.1)

    try:
        w32c = _open_clipboard()
        try:
            if not w32c.IsClipboardFormatAvailable(w32c.CF_UNICODETEXT):
                return {"output": "## Clipboard\n(Empty or non-text content)"}
            text = w32c.GetClipboardData(w32c.CF_UNICODETEXT) or ""
            text = text.strip()
            if not text:
                return {"output": "## Clipboard\n(Empty)"}
            return {"output": f"## Clipboard\n---\n{text}", "text": text}
        finally:
            w32c.CloseClipboard()
    except RuntimeError:
        # _open_clipboard already chained the cause; re-raise as-is.
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to read clipboard: {e}") from e


def set_clipboard(text: str, keyboard: bool = False) -> dict[str, Any]:
    """Set clipboard text content.

    Args:
        text: Text to put on the clipboard.
        keyboard: If True, after setting the clipboard simulate Ctrl+V to paste
            into the focused application. The paste runs only after a successful
            set (it's gated behind it).

    Returns:
        Dict with ``output`` describing the result.
    """
    try:
        w32c = _open_clipboard()
        try:
            w32c.EmptyClipboard()
            w32c.SetClipboardData(w32c.CF_UNICODETEXT, text)
        finally:
            w32c.CloseClipboard()
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to set clipboard: {e}") from e

    if keyboard:
        from agent_windows.core.input_controller import shortcut
        shortcut("ctrl+v")

    preview = text[:50] + ("..." if len(text) > 50 else "")
    suffix = "\nKeyboard: simulated Ctrl+V" if keyboard else ""
    return {"output": f"## Clipboard Set\nText: {preview}{suffix}"}
