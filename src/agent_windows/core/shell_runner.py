"""PowerShell command execution."""

import logging
import re
import subprocess
from typing import Any

from agent_windows.utils.config import load_config, get_log_dir

logger = logging.getLogger(__name__)


def _check_blacklist(command: str) -> str | None:
    """Return the blacklist pattern a command matches, or ``None`` if allowed.

    Patterns are case-insensitive regular expressions matched against the full
    command. ``blacklist`` may be a single string (one pattern — settable via
    ``config set shell.blacklist "..."``, since the config setter only handles
    scalars) or a list of strings (multiple patterns — set by editing
    config.toml). The default is empty (no filtering). An invalid pattern is
    skipped with a warning rather than aborting command handling. (M-05)
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
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return pattern
        except re.error:
            logger.warning("Skipping invalid shell blacklist pattern: %r", pattern)
    return None


def _audit_shell(command: str, status: str) -> None:
    """Append the full command and a status to the shell audit log. (M-05)

    Unlike daemon.log (which truncates), this records the complete, untruncated
    command for every execution attempt — allowed or blocked — as a forensic
    trail. Each entry is one line (newlines in the command are escaped so the
    log stays line-addressable). A write failure is logged but never disrupts
    command handling.
    """
    try:
        import datetime

        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        one_line = (
            command.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
        )
        entry = f"{ts} | {status} | {one_line}\n"
        path = get_log_dir() / "shell_audit.log"
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
