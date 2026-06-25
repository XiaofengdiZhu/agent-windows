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


# TODO #13: PowerShell 5.1 emits stdout in the system codepage (GBK on a
# Chinese Windows) by default; the subprocess below decodes as UTF-8, so the
# two mismatch and non-ASCII (CJK) output turns to mojibake. Prepending this
# snippet forces both PowerShell's *output* encoding ([Console]::OutputEncoding,
# what it writes to the pipe) and its *input* encoding ($OutputEncoding, what
# it uses to decode bytes read from native exes) to UTF-8, so the bytes on the
# pipe are already UTF-8 and the Python-side utf-8 decode matches. This mirrors
# the reference (powershell/service.py) approach but stays on `powershell
# -Command` (no -EncodedCommand) to match the rest of the codebase's style.
_UTF8_PREFIX = (
    "$OutputEncoding = [System.Text.Encoding]::UTF8; "
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
)


def _shell_config() -> dict:
    """Return the ``shell`` config section (empty dict if absent)."""
    return load_config().get("shell", {}) or {}


def _truncate_stream(text: str, budget: int) -> tuple[str, int]:
    """Truncate *text* to at most *budget* UTF-8 bytes, ending on a newline.

    Returns (truncated_text, bytes_used). The byte slice is walked back to the
    last valid UTF-8 character boundary (so a multi-byte CJK char is dropped
    whole rather than split into a replacement char), then to the last newline
    to keep the tail readable. bytes_used is the actual byte length returned.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text, len(encoded)
    capped_bytes = encoded[:budget]
    # Walk back to the last full character boundary: errors='ignore' drops the
    # 1-2 trailing bytes of a half-included multi-byte char instead of emitting
    # a U+FFFD replacement char.
    capped = capped_bytes.decode("utf-8", "ignore")
    nl = capped.rfind("\n")
    if nl > 0:
        capped = capped[:nl] + "\n"
    return capped, len(capped.encode("utf-8"))


def _truncate_streams(stdout: str, stderr: str, budget: int) -> tuple[str, str]:
    """Truncate stdout+stderr to a combined *budget* (UTF-8 bytes).

    stdout gets budget priority (the common case is a large stdout with little
    or no stderr). Returns (new_stdout, new_stderr).
    """
    stdout, used = _truncate_stream(stdout, budget)
    remaining = budget - used
    if remaining <= 0:
        return stdout, ""
    stderr, _ = _truncate_stream(stderr, remaining)
    return stdout, stderr


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


def run_shell(command: str, timeout: float | None = None) -> dict[str, Any]:
    """Execute a PowerShell command and return the output.

    A command matching the optional ``shell.blacklist`` config (M-05) is
    rejected with :class:`PermissionError` (→ JSON-RPC ``PERMISSION_DENIED``
    -1004) before it reaches PowerShell. Every attempt — allowed or blocked —
    is recorded with the full command in ``logs/shell_audit.log``.

    Args:
        command: PowerShell command string.
        timeout: Execution timeout in seconds. ``None`` (default) reads
            ``shell.timeout`` from config (fallback 30.0). An explicit value
            overrides the config default. (TODO #13)

    Returns:
        Dict with output, exit_code.

    Raises:
        PermissionError: If the command matches the blacklist.
        TimeoutError: On execution timeout (→ JSON-RPC ``WAIT_TIMEOUT``
            -1003). (TODO #13: was RuntimeError — a timeout is a wait
            condition, not an internal crash, so it must not trip the router's
            catch-all + logger.exception.)
        RuntimeError: If PowerShell is missing (environment failure).
    """
    matched = _check_blacklist(command)
    if matched is not None:
        _audit_shell(command, f"BLOCKED:{matched}")
        raise PermissionError(
            f"Command blocked by shell blacklist (matched pattern: {matched!r})"
        )
    _audit_shell(command, "ALLOWED")
    logger.debug("Executing shell command: %s", command)

    # TODO #13: timeout from config when the caller doesn't pass one.
    cfg = _shell_config()
    if timeout is None:
        timeout = float(cfg.get("timeout", 30.0))
    max_output_bytes = int(cfg.get("max_output_bytes", 0) or 0)
    # Empty/absent → UTF-8. The UTF-8 prefix below makes PS emit UTF-8, so the
    # two match and non-ASCII (CJK) output decodes cleanly. A NON-utf-8 override
    # (e.g. 'gbk') means a native exe ignores our UTF-8 request and emits its
    # legacy codepage — in that case the prefix must NOT run, or PS emits UTF-8
    # while Python decodes as GBK → mojibake (the exact bug this TODO fixes).
    out_encoding = (cfg.get("output_encoding") or "utf-8").strip() or "utf-8"
    use_utf8_prefix = out_encoding.lower() in ("", "utf-8", "utf8")

    # TODO #13: force UTF-8 on the PowerShell side so the pipe bytes match the
    # Python-side decode. The prefix is prepended to the *command* (still one
    # -Command argument), so it never changes how the caller's command is
    # quoted/escaped. Skipped for a non-UTF-8 decode override (see above).
    full_command = (_UTF8_PREFIX + command) if use_utf8_prefix else command

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", full_command],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding=out_encoding,
            errors="replace",
        )

        stdout = result.stdout or ""
        stderr = result.stderr or ""

        # TODO #13: cap combined output so a runaway command can't flood the
        # agent's context. Measured on the decoded text's UTF-8 byte length so
        # the budget is encoding-stable. Applies to the returned stdout/stderr
        # and thus the formatted `output`; the audit log is unaffected (it
        # records commands, never output). stdout gets budget priority (the
        # common case is a huge stdout with little/no stderr).
        truncated = False
        orig_bytes = 0
        if max_output_bytes > 0:
            orig_bytes = len((stdout + stderr).encode("utf-8", "replace"))
            if orig_bytes > max_output_bytes:
                stdout, stderr = _truncate_streams(
                    stdout, stderr, max_output_bytes
                )
                truncated = True

        output_parts = [f"## Shell\nCommand: {command}"]
        if stdout.strip():
            output_parts.append(stdout.strip())
        if stderr.strip():
            output_parts.append(f"[stderr]\n{stderr.strip()}")
        output_parts.append(f"Exit code: {result.returncode}")

        if truncated:
            output_parts.append(
                f"\n[output truncated: {orig_bytes} bytes, limit {max_output_bytes}]"
            )

        # "Access is denied" is the canonical opaque ACL failure; when the
        # server isn't elevated, append a hint so the agent has an actionable
        # fix instead of a raw system error. (TODO #14) Operates on the
        # (possibly truncated) streams by design — if a huge stdout ate the
        # whole budget and emptied stderr, the hint won't fire; that's an
        # acceptable trade-off for bounding context, and ACL failures rarely
        # produce multi-KB stdout.
        hint = None
        if result.returncode != 0:
            combined = f"{stdout}\n{stderr}".lower()
            if "access is denied" in combined:
                hint = elevation_hint()

        if hint:
            output_parts.append(f"\nHINT: {hint}")

        return {
            "output": "\n".join(output_parts),
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
    except subprocess.TimeoutExpired:
        raise TimeoutError(
            f"Shell command timed out after {timeout}s: {command}"
        )
    except FileNotFoundError:
        raise RuntimeError("PowerShell not found")
