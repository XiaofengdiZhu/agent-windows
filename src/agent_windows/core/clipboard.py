"""Clipboard operations: get/set via PowerShell or keyboard simulation."""

import subprocess
import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_clipboard(keyboard: bool = False) -> dict[str, Any]:
    """Get clipboard text content.

    Args:
        keyboard: If True, simulate Ctrl+C first to copy current selection,
            then read clipboard via system API.
    """
    if keyboard:
        from agent_windows.core.input_controller import shortcut
        shortcut("ctrl+c")
        import time
        time.sleep(0.1)

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        text = result.stdout.strip()
        if not text:
            return {"output": "## Clipboard\n(Empty or non-text content)"}
        return {"output": f"## Clipboard\n---\n{text}", "text": text}
    except Exception as e:
        return {"output": f"## Clipboard\n(Error: {e})"}


def set_clipboard(text: str, keyboard: bool = False) -> dict[str, Any]:
    """Set clipboard text content.

    Args:
        text: Text to put in clipboard.
        keyboard: If True, set clipboard via system API, then simulate
            Ctrl+V to paste into focused application.
    """
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Set-Clipboard -Value @'\n{text}\n'@"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(f"Set-Clipboard failed: {result.stderr}")
    except Exception as e:
        raise RuntimeError(f"Failed to set clipboard: {e}")

    if keyboard:
        from agent_windows.core.input_controller import shortcut
        shortcut("ctrl+v")

    return {"output": f"## Clipboard Set\nText: {text[:50]}{'...' if len(text) > 50 else ''}" + ("\nKeyboard: simulated Ctrl+V" if keyboard else "")}
