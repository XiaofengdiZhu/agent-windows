"""WaitFor multi-condition polling logic (TODO #7).

Pure functions only — no COM, no daemon, no I/O. The daemon ``wait_for`` /
``wait_for_check`` handlers (``daemon/handlers/tree.py``) call
:func:`matches_condition` with a snapshot dict from :func:`get_tree_snapshot`;
everything here is unit-testable with a hand-built snapshot dict + ElementInfo.

Matching is **case-sensitive substring** (``in``), matching the existing
``wait`` text mode (``text in el.name``) — deliberately NOT the casefold match
the reference uses. Documented in ``skills/.../commands.md``.
"""

from __future__ import annotations

from typing import Any

from agent_windows.core.tree import ElementInfo

# Canonical condition names. Order is stable for error messages.
_VALID_CONDITIONS = (
    "text_exists",
    "active_window",
    "element_exists",
    "element_enabled",
    "focused_element",
)

# Short aliases → canonical. Mirrors the reference impl's alias table so callers
# can write the friendly short form (e.g. ``wait-for enabled --text OK``).
_ALIASES: dict[str, str] = {
    "text": "text_exists",
    "window": "active_window",
    "element": "element_exists",
    "enabled": "element_enabled",
    "focused": "focused_element",
}


def normalize_condition(condition: str) -> str:
    """Normalize a raw condition string to a canonical name.

    Lowercases, collapses ``-``/space to ``_``, then applies the alias table.
    Raises ``ValueError`` (→ INVALID_PARAMS at the router) for an unknown
    condition so the caller gets a clear list of valid choices.
    """
    if not isinstance(condition, str):
        raise ValueError(
            "condition must be a string, got "
            f"{type(condition).__name__}"
        )
    norm = condition.strip().lower().replace("-", "_").replace(" ", "_")
    norm = _ALIASES.get(norm, norm)
    if norm not in _VALID_CONDITIONS:
        raise ValueError(
            "condition must be one of: " + ", ".join(_VALID_CONDITIONS)
        )
    return norm


def validate_wait_for(
    condition: str,
    text: str | None,
    window_name: str | None,
    timeout: float,
    interval: float | None,
    *,
    max_timeout: float,
) -> str:
    """Validate + normalize WaitFor arguments.

    Returns the normalized condition. Raises ``ValueError`` (→ INVALID_PARAMS)
    on any bad value. ``max_timeout`` is threaded in from the handler's
    ``MAX_WAIT_SECONDS`` (SEC3-01) so this module stays free of the daemon
    constant and is testable in isolation.

    ``interval`` may be ``None`` to skip the interval bounds check — a single
    probe (``wait_for_check``) has no poll interval to validate.
    """
    norm = normalize_condition(condition)

    if timeout <= 0 or timeout > max_timeout:
        raise ValueError(
            f"timeout must be greater than 0 and at most {max_timeout:g} seconds"
        )
    if interval is not None and (interval <= 0 or interval > 5):
        raise ValueError("interval must be greater than 0 and at most 5 seconds")

    # text/window_name presence checks use the raw values (None or ""). The
    # canonicalized "" forms are not returned — callers (handlers) pass their
    # own params.get(...) strings into _wait_for_once / matches_condition, which
    # re-coalesce None→"" at entry.
    if norm == "text_exists" and not text:
        raise ValueError("text is required when condition is text_exists")
    # active_window: accept either the title via text or an explicit window_name.
    if norm == "active_window" and not (text or window_name):
        raise ValueError(
            "text or window_name is required when condition is active_window"
        )
    if norm in {"element_exists", "element_enabled", "focused_element"} and not (
        text or window_name
    ):
        raise ValueError(
            "text or window_name is required when condition is "
            f"{norm}"
        )

    return norm


def _substr(value: object | None, expected: str) -> bool:
    """Case-sensitive substring test. None / non-str values are stringified so
    a metadata int/bool can still match, mirroring how the snapshot renders them."""
    if expected == "":
        return True  # empty filter matches anything
    if value is None:
        return False
    return expected in str(value)


def _iter_text_sources(snapshot: dict[str, Any]):
    """Yield every string that ``text_exists`` should search: the active
    window title, all window titles, then per-element name / control_type /
    window_name / metadata values. Matches the reference impl's text-source
    enumeration minus the DOM nodes (no browser DOM in this project — #5)."""
    active = snapshot.get("active_window")
    if active is not None:
        yield getattr(active, "title", "") or ""

    for win in snapshot.get("windows", []):
        yield getattr(win, "title", "") or ""

    for el in snapshot.get("interactive_elements", []):
        yield el.name or ""
        yield el.control_type or ""
        yield el.window_name or ""
        for value in (el.metadata or {}).values():
            yield value


def _element_matches(el: ElementInfo, text: str, window_name: str) -> bool:
    """An element matches when its name/control_type/metadata contains ``text``
    AND its window_name contains ``window_name`` (both case-sensitive substring;
    empty filter = wildcard). Mirrors the reference ``_node_matches``."""
    if text and not (
        _substr(el.name, text)
        or _substr(el.control_type, text)
        or any(_substr(v, text) for v in (el.metadata or {}).values())
    ):
        return False
    if window_name and not _substr(el.window_name, window_name):
        return False
    return True


def matches_condition(
    snapshot: dict[str, Any],
    condition: str,
    text: str | None,
    window_name: str | None,
) -> tuple[bool, str]:
    """Evaluate a normalized condition against a snapshot.

    Args:
        snapshot: dict from :func:`traverse_tree` / :func:`get_tree_snapshot`
            (keys: ``active_window``, ``windows``, ``interactive_elements``).
        condition: a canonical condition name (run through
            :func:`normalize_condition` first by the caller).
        text, window_name: optional case-sensitive substring filters.

    Returns:
        ``(matched, detail)`` — ``detail`` is a human-readable explanation
        suitable for the CLI's success/timeout message.
    """
    text = text or ""
    window_name = window_name or ""

    if condition == "text_exists":
        # Defensive guard: text_exists is meaningless without a text filter, and
        # an empty filter would wildcard-match the first (possibly empty) source
        # string via _substr. validate_wait_for rejects this on the public path;
        # guard here too so a direct caller of matches_condition can't get a
        # spurious match.
        if not text:
            return False, "no text filter supplied for text_exists"
        for source in _iter_text_sources(snapshot):
            if _substr(source, text):
                return True, f"text {text!r} appeared"
        return False, f"text {text!r} was absent"

    if condition == "active_window":
        expected = window_name or text
        active = snapshot.get("active_window")
        active_title = getattr(active, "title", "") if active else ""
        if _substr(active_title, expected):
            return True, f"active window matched {active_title!r}"
        return False, f"active window was {active_title!r}"

    if condition in {"element_exists", "element_enabled", "focused_element"}:
        for el in snapshot.get("interactive_elements", []):
            if not _element_matches(el, text, window_name):
                continue
            if condition == "element_enabled" and not el.is_enabled:
                continue
            if condition == "focused_element" and not el.is_focused:
                continue
            return True, f"element matched {el.name!r}"
        return False, "matching element was absent"

    # Unreachable when the caller normalizes first, but keep a defensive guard
    # so a raw/unknown condition never silently returns "not found".
    raise ValueError(
        f"Unsupported WaitFor condition: {condition!r} "
        "(expected one of: " + ", ".join(_VALID_CONDITIONS) + ")"
    )
