"""Clipboard operations: get and set text via PowerShell."""

import subprocess
import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_clipboard() -> dict[str, Any]:
    """Get current clipboard text content."""
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


def set_clipboard(text: str) -> dict[str, Any]:
    """Set clipboard text content."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Set-Clipboard -Value @'\n{text}\n'@"],
            capture_output=True, text=True, timeout=5,
            encoding="utf-8", errors="replace",
        )
        if result.returncode != 0:
            raise RuntimeError(f"Set-Clipboard failed: {result.stderr}")
        return {"output": f"## Clipboard Set\nText: {text[:50]}{'...' if len(text) > 50 else ''}"}
    except Exception as e:
        raise RuntimeError(f"Failed to set clipboard: {e}")
