"""Default-on, HMAC-keyed audit log for destructive desktop operations.

Records caller identity, method, target params, and outcome for every
destructive op (fs_delete / fs_move / process_kill / registry set|delete /
system_shutdown / system_restart / sleep), with a per-line HMAC so integrity
is verifiable by ``audit verify``. Default-on (inverse of telemetry's opt-in):
destructive ops are low-frequency / high-harm. A ``[audit] enabled = false``
config is the kill-switch.

Caller identity is threaded from the MCP HTTP edge via the injected
``__caller`` envelope field (read from the live ASGI scope in
``mcp/_call.py::_resolve_audit_caller``); the CLI injects ``"local"``; the
daemon router sets this module's ``audit_caller`` contextvar from ``__caller``
(default ``"pipe"``). See the design spec at
docs/superpowers/specs/2026-07-22-destructive-audit-design.md.

Pure stdlib at module top (hmac/datetime/secrets/contextvars/dataclasses) +
``agent_windows.utils.config.get_log_dir`` — import-latency-safe; the CLI path
never imports this module (it only injects the string ``"local"``).
"""
from __future__ import annotations

import datetime
import hmac
import json
import logging
import secrets
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_windows.utils.config import get_log_dir

logger = logging.getLogger(__name__)

# Max active log size before it rotates to action_audit.log.1 — matches the
# hand-rolled single-backup scheme used by telemetry.log / shell_audit.log.
_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB

# Caller identity set at the daemon router from the injected ``__caller`` param
# (the MCP edge reads the live ASGI scope in mcp/_call.py::_resolve_audit_caller
# and injects it; the CLI injects "local"). Default None -> unknown, which
# current_caller() reports as "pipe".
audit_caller: ContextVar[str | None] = ContextVar("audit_caller", default=None)


def current_caller() -> str:
    """The caller identity for the in-flight request (``"pipe"`` when unknown).

    The daemon router sets this from the injected ``__caller`` param around
    dispatch; destructive handlers pass it to :func:`audit_destructive`.
    """
    return audit_caller.get() or "pipe"


def _audit_path() -> Path:
    """The action audit log path under the owner-only-ACL logs dir."""
    return get_log_dir() / "action_audit.log"


def _escape(s: str) -> str:
    """Escape ``|``, newline, tab and backslash so a field can't split a line.

    Backslash first (so it doesn't double-escape the replacements), then the
    chars it protects — identical to telemetry.py / shell_audit.log. Stops a
    value carrying ``|`` or ``\\n`` from injecting fake fields or extra lines.
    The verifier splits on the LAST ``" | "`` (the HMAC separator) so escaped
    pipes inside a value never misalign the columns.
    """
    return (s.replace("\\", "\\\\")
             .replace("|", "\\|")
             .replace("\r", "\\r")
             .replace("\n", "\\n")
             .replace("\t", "\\t"))


def _rotate_if_needed(path: Path) -> None:
    """Best-effort rotate the active log to ``.1`` once it exceeds the cap.

    A rotation failure is swallowed (debug only): it must never block the audit
    write, which is the whole point. Mirrors telemetry.py.
    """
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        logger.debug("Audit log rotation skipped", exc_info=True)


def _audit_enabled() -> bool:
    """Read the ``[audit] enabled`` kill-switch (default True)."""
    from agent_windows.utils.config import load_config
    return bool((load_config().get("audit") or {}).get("enabled", True))


def _get_key() -> bytes:
    """The HMAC key, lazily generated and DPAPI-encrypted at rest.

    Mirrors ``vlm.api_key`` / ``mcp.auth_key``: stored under ``[audit] key``,
    OUTSIDE DEFAULT_CONFIG (so it is neither ``config set``-able nor
    type-validated), DPAPI-encrypted by ``_encrypt_for_write``. On first use a
    32-byte url-safe token is generated and persisted. A corrupted/unreadable
    key is treated as first-run: a fresh key is generated (prior log lines then
    fail ``verify`` — itself a forensic signal).
    """
    from agent_windows.utils.config import load_config
    key = (load_config().get("audit") or {}).get("key")
    if isinstance(key, str) and key:
        return key.encode("utf-8")
    key = secrets.token_urlsafe(32)
    try:
        _persist_key(key)
    except Exception:  # noqa: BLE001 — key persistence is best-effort
        logger.debug("Failed to persist audit key", exc_info=True)
    return key.encode("utf-8")


def _persist_key(key: str) -> None:
    """Write the generated audit key into ``[audit] key``.

    DPAPI-encrypted at rest by ``save_config`` (not by this function).
    """
    from agent_windows.utils.config import load_config, save_config
    cfg = load_config()
    cfg.setdefault("audit", {})["key"] = key
    save_config(cfg)


def audit_destructive(
    method: str,
    params: dict[str, Any],
    status: str,
    *,
    caller: str | None = None,
) -> None:
    """Append one HMAC-keyed audit line for a destructive operation.

    Args:
        method: daemon method name (e.g. ``"fs_delete"``). For the registry
            handler the recorded method is ``"registry_set"`` /
            ``"registry_delete"`` (mode made explicit for forensic clarity).
        params: the recorded param subset (caller-chosen, already
            privacy-scrubbed — e.g. registry ``value`` is excluded).
        status: ``"ok"`` (success) / ``"fail"`` (raised) / ``"blocked"``
            (pre-effect rejection, e.g. filesystem blacklist / INVALID_PARAMS).
        caller: caller identity; defaults to :func:`current_caller`.

    No-op when ``[audit] enabled`` is false. Best-effort: a write failure is
    logged at debug and never raised — audit must not break the destructive-op
    path it observes (same posture as telemetry / shell_audit).
    """
    if not _audit_enabled():
        return
    try:
        ts = datetime.datetime.now().isoformat(timespec="milliseconds")
        # Stable, compact JSON so the HMAC input is deterministic across runs.
        params_json = _escape(
            json.dumps(params, ensure_ascii=False, separators=(",", ":")))
        who = _escape(caller or current_caller())
        prefix = (f"{ts} | {who} | {_escape(method)} | "
                  f"{_escape(status)} | {params_json}")
        key = _get_key()
        mac = hmac.new(key, prefix.encode("utf-8"), "sha256").hexdigest()
        path = _audit_path()
        _rotate_if_needed(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{prefix} | {mac}\n")
    except Exception:  # noqa: BLE001 — audit is best-effort, never fatal
        logger.debug("Failed to write audit log", exc_info=True)


@dataclass
class VerifyReport:
    """Integrity report for :func:`verify_log`."""
    total: int = 0          # parseable lines (verified + mismatched)
    verified: int = 0
    malformed: list[int] = field(default_factory=list)   # 1-indexed line numbers
    mismatched: list[int] = field(default_factory=list)  # 1-indexed line numbers

    @property
    def ok(self) -> bool:
        return not self.malformed and not self.mismatched


def verify_log(path: Path | None = None) -> VerifyReport:
    """Recompute every line's HMAC and report integrity.

    Each line is ``<5 joined fields> | <hmac>``; the verifier splits on the LAST
    ``" | "`` (the HMAC separator, which a hex digest never contains) so escaped
    pipes inside ``params_json`` cannot misalign the split. Lines with no ``" | "``
    are ``malformed`` (not mismatched). A missing/empty file -> an empty report.
    """
    report = VerifyReport()
    path = path or _audit_path()
    if not path.exists():
        return report
    key = None  # fetched lazily only when a parseable line is encountered
    with open(path, encoding="utf-8") as f:
        for lineno, raw in enumerate(f, start=1):
            line = raw.rstrip("\n")
            idx = line.rfind(" | ")
            if idx == -1:
                report.malformed.append(lineno)
                continue
            report.total += 1
            if key is None:
                key = _get_key()
            prefix, mac = line[:idx], line[idx + 3:]
            expected = hmac.new(key, prefix.encode("utf-8"), "sha256").hexdigest()
            if hmac.compare_digest(expected, mac):
                report.verified += 1
            else:
                report.mismatched.append(lineno)
    return report
