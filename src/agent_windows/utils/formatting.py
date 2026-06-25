"""Display-formatting helpers.

Kept dependency-free and pure so callers in core/ and the MCP layer can format
human-readable values (file sizes, etc.) consistently without duplicating the
threshold math. Currently only ``format_size``; add siblings here as needed.
"""
from __future__ import annotations

_UNITS = [("B", 1), ("KB", 1024), ("MB", 1024 ** 2),
          ("GB", 1024 ** 3), ("TB", 1024 ** 4)]


def format_size(size_bytes: int) -> str:
    """Format a byte count into a compact human-readable string.

    Binary units (1024-based), one decimal place from KB upward. Mirrors the
    reference Windows-MCP formatter but also handles negatives and values at
    or above 1 TiB (the reference would mislabel those as GB).

    >>> format_size(0)
    '0 B'
    >>> format_size(1023)
    '1023 B'
    >>> format_size(1024)
    '1.0 KB'
    >>> format_size(1572864)
    '1.5 MB'
    """
    if size_bytes < 0:
        return f"{size_bytes} B"
    # Walk the unit ladder; stop at the largest unit that keeps the value >= 1
    # (or TB, the last entry). Below 1024 B the value stays in "B".
    unit, divisor = "B", 1
    for name, div in _UNITS[1:]:
        if size_bytes < div:
            break
        unit, divisor = name, div
    if divisor == 1:
        return f"{size_bytes} B"
    return f"{size_bytes / divisor:.1f} {unit}"
