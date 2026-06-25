"""PowerShell command execution."""

import logging
import re
import subprocess
from typing import Any

from agent_windows.utils.config import load_config, get_log_dir
from agent_windows.utils.win32 import elevation_hint

logger = logging.getLogger(__name__)


# M-05 #3: a quantifier applied to a group that itself holds an unbounded
# quantifier — the classic ReDoS shape like (a+)+, (a*)*, (a|b+)* — can
# backtrack catastrophically on adversarial input and hang the single-threaded
# daemon (the regex runs in _check_blacklist, before the subprocess timeout).
# Reject such patterns up front. This is a conservative heuristic, not a
# complete ReDoS guarantee; it targets the textbook shapes while letting
# benign quantified groups like (\w)+ through. Known blind spot: overlapping
# alternation like (a|a)+ has no inner quantifier and slips through — a regex
# to catch it would over-match benign (a|b)+, and a true fix needs a
# time-bounded matcher (Python's re lacks one), so this stays a documented
# limitation rather than a weakening of the heuristic.
_REDOS_SHAPE = re.compile(r"\([^()]*[+*?][^()]*\)[+*?]")


def _check_blacklist(command: str) -> str | None:
    """Return the blacklist pattern a command matches, or ``None`` if allowed.

    Patterns are case-insensitive regular expressions matched against the full
    command. ``blacklist`` may be a single string (one pattern — settable via
    ``config set shell.blacklist "..."``, since the config setter only handles
    scalars) or a list of strings (multiple patterns — set by editing
    config.toml). The default is empty (no filtering). An invalid pattern, or
    one that looks like a ReDoS vector, is skipped with a warning rather than
    aborting command handling. (M-05)
    """
    blacklist = load_config().get("shell", {}).get("blacklist", [])
    if isinstance(blacklist, str):
        patterns = [blacklist]
    elif isinstance(blacklist, list):
        patterns = blacklist
    else:
        return None
    for pattern in patterns:
        if not isinstance(pattern, str):
            continue
        if _REDOS_SHAPE.search(pattern):
            logger.warning(
                "Skipping shell blacklist pattern that looks like a ReDoS vector "
                "(nested quantifier): %r",
                pattern,
            )
            continue
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return pattern
        except re.error:
            logger.warning("Skipping invalid shell blacklist pattern: %r", pattern)
    return None


# M-05 #2: cap the audit log so a long-running agent can't fill the disk.
# When the active file exceeds this, it rotates to .1 (one backup kept), so
# roughly the last 2x cap of command history is retained.
_SHELL_AUDIT_MAX_BYTES = 10 * 1024 * 1024  # 10 MB


def _audit_shell(command: str, status: str) -> None:
    """Append the full command and a status to the shell audit log. (M-05)

    Unlike daemon.log (which truncates), this records the complete, untruncated
    command for every execution attempt — allowed or blocked — as a forensic
    trail. Each entry is one line (newlines in the command are escaped so the
    log stays line-addressable). The file is size-capped via rotation. A write
    failure is logged but never disrupts command handling.
    """
    try:
        import datetime

        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        one_line = (
            command.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
        )
        entry = f"{ts} | {status} | {one_line}\n"
        # The full command (which may carry secrets) is written here. There is no
        # DPAPI layer for logs — the owner-only ACL applied by get_log_dir() is
        # the SOLE access control, and it is best-effort (see config.py
        # _apply_owner_only_acl, SEC3-07). In multi-user environments, verify
        # logs/ ACLs after first daemon start.
        path = get_log_dir() / "shell_audit.log"
        # Bound growth: rotate the active file to .1 when it exceeds the cap.
        # Best-effort — a rotation failure must not block the audit write below.
        try:
            if path.exists() and path.stat().st_size > _SHELL_AUDIT_MAX_BYTES:
                path.replace(path.with_name(path.name + ".1"))
        except OSError:
            logger.debug("Shell audit log rotation skipped", exc_info=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception:
        logger.warning("Failed to write shell audit log", exc_info=True)


def run_shell(command: str, timeout: float = 30.0) -> dict[str, Any]:
    """Execute a PowerShell command and return the output.

    A command matching the optional ``shell.blacklist`` config (M-05) is
    rejected with :class:`PermissionError` (→ JSON-RPC ``PERMISSION_DENIED``
    -1004) before it reaches PowerShell. Every attempt — allowed or blocked —
    is recorded with the full command in ``logs/shell_audit.log``.

    Args:
        command: PowerShell command string.
        timeout: Execution timeout in seconds.

    Returns:
        Dict with output, exit_code.

    Raises:
        PermissionError: If the command matches the blacklist.
        RuntimeError: On timeout or if PowerShell is missing.
    """
    matched = _check_blacklist(command)
    if matched is not None:
        _audit_shell(command, f"BLOCKED:{matched}")
        raise PermissionError(
            f"Command blocked by shell blacklist (matched pattern: {matched!r})"
        )
    _audit_shell(command, "ALLOWED")
    logger.debug("Executing shell command: %s", command)

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )

        output_parts = [f"## Shell\nCommand: {command}"]
        if result.stdout.strip():
            output_parts.append(result.stdout.strip())
        if result.stderr.strip():
            output_parts.append(f"[stderr]\n{result.stderr.strip()}")
        output_parts.append(f"Exit code: {result.returncode}")

        # "Access is denied" is the canonical opaque ACL failure; when the
        # server isn't elevated, append a hint so the agent has an actionable
        # fix instead of a raw system error. (TODO #14)
        hint = None
        if result.returncode != 0:
            combined = f"{result.stdout}\n{result.stderr}".lower()
            if "access is denied" in combined:
                hint = elevation_hint()

        if hint:
            output_parts.append(f"\nHINT: {hint}")

        return {
            "output": "\n".join(output_parts),
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Shell command timed out after {timeout}s: {command}")
    except FileNotFoundError:
        raise RuntimeError("PowerShell not found")
