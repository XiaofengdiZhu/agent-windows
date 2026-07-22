"""Configuration management: TOML file read/write.

Config file: ~/.agent-windows/config.toml
Data directory: ~/.agent-windows/
"""

import copy
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any
from dataclasses import dataclass, field

import toml

logger = logging.getLogger(__name__)

# ── Config cache ───────────────────────────────────────────────────

_config_cache: dict[str, Any] | None = None
_config_cache_mtime: float = 0.0
_config_lock = threading.Lock()

# ── Config override (--config) ─────────────────────────────────────
# When set (by the CLI --config flag or the daemon's argv scan),
# get_config_path() returns this instead of the default data-dir path.
# set_config_override() also invalidates the cache so the next load_config()
# re-reads from the new path. State files (pid/lock/version/logs) are NOT
# affected — they stay in get_data_dir() (the override is config-file-only).
_config_override: Path | None = None

# ── Paths ─────────────────────────────────────────────────────────


def get_data_dir() -> Path:
    """Return the agent-windows data directory (~/.agent-windows/), creating it if needed."""
    d = Path.home() / ".agent-windows"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_config_path() -> Path:
    """Return path to config.toml (override > default).

    The override is set by ``--config`` (CLI Typer callback, or the daemon's
    argv scan) and applies to reads *and* writes (``config set``/``mcp auth``
    write to the override file when one is set). State files
    (pid/lock/version/logs/screenshots) are unaffected — they stay in
    :func:`get_data_dir`.
    """
    if _config_override is not None:
        return _config_override
    return get_data_dir() / "config.toml"


def set_config_override(path: str | Path | None) -> None:
    """Set (or clear, if *path* is None) the config-file path override.

    Expands ``~``/``~user`` and resolves to absolute. Invalidates the in-memory
    cache so the next :func:`load_config` re-reads from the new path
    (mtime-based caching would otherwise serve stale contents if the default
    and override files happened to share an mtime). Thread-safe (reuses
    ``_config_lock``).

    Called once at process start: from the CLI Typer ``--config`` callback
    (CLI/MCP process) and from ``_init_daemon_state`` (daemon process, reading
    ``--config`` off argv).
    """
    global _config_override, _config_cache, _config_cache_mtime
    with _config_lock:
        if path is None:
            _config_override = None
        else:
            _config_override = Path(os.path.expanduser(str(path))).resolve()
        # Force reload regardless of mtime.
        _config_cache = None
        _config_cache_mtime = 0.0


def get_config_override() -> Path | None:
    """Return the active config override path, or None (default location)."""
    return _config_override


def get_pid_path() -> Path:
    return get_data_dir() / "daemon.pid"


def get_version_path() -> Path:
    return get_data_dir() / "daemon.version"


def get_lock_path() -> Path:
    """Path to the cross-process lock file used to serialize daemon spawns.

    Both the Python client and the Rust launcher LockFileEx this file across the
    check-then-spawn critical section, so concurrent invocations can't each
    decide the daemon is down and spawn a rival. The lock is kernel-level and
    language-agnostic (LockFileEx on the same path).
    """
    return get_data_dir() / "daemon.lock"


_logs_dir_lock = threading.Lock()
_logs_dir_secured = False


def get_log_dir() -> Path:
    global _logs_dir_secured
    d = get_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    # M-01: lock logs/ to the owner with an inheritable DACL so every log file
    # (daemon.log, shell_audit.log, rotated backups) is owner-only — full shell
    # commands recorded there may carry secrets, and unlike config.toml there are
    # many files, so one directory-level inheritable ACL covers them all. NTFS
    # inheritance is applied at file creation, so the first call per process also
    # re-secures any files already present: logs left from before this hardening
    # were created under a broader inherited ACL and don't retroactively inherit
    # the directory's new DACL, so they're locked down file-by-file. A daemon
    # restart repeats this (covers upgrades). Guarded so concurrent first-callers
    # run the securing once, not each.
    with _logs_dir_lock:
        if not _logs_dir_secured:
            _secure_directory(d)
            for entry in d.iterdir():
                if entry.is_file():
                    _apply_owner_only_acl(entry, inheritable=False)
            _logs_dir_secured = True
    return d


def get_screenshot_dir() -> Path:
    d = get_data_dir() / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_prompts_dir() -> Path:
    d = get_data_dir() / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Defaults ──────────────────────────────────────────────────────

DEFAULT_CONFIG: dict[str, Any] = {
    "vlm": {
        "enabled": True,
        "engine": "openai",
        "model": "gpt-4o-mini",
        "api_base": "https://api.openai.com/v1",
        "api_key": "",
        "timeout": 30,
        "width": 1000,
        "height": 1000,
        "crop_size": 400,
        "crop_height": 300,
        "max_drift": 40,
        "image_format": "JPEG",  # Vision API image encoding: "JPEG" or "PNG"
        "jpeg_quality": 85,  # JPEG quality (1-95) for vision API, ignored when PNG
        "single_pass": False,  # Skip the fine-pass API call (1 call, faster, lower precision)
    },
    "screenshot": {
        "backend": "auto",
        "scale": 1.0,
        "max_width": 1920,
        "max_height": 1080,
        "format": "png",
        "jpeg_quality": 85,
        "resampling": "BILINEAR",  # Resampling filter for saved screenshots: "BILINEAR", "LANCZOS", "NEAREST"
        "max_screenshot_age_days": 7,
        "max_screenshot_dir_mb": 500,
        "cleanup_interval_minutes": 10,
    },
    "tree": {
        "include_invisible": False,
        "include_offscreen": False,
        "max_depth": 15,  # H-04: lowered from 30 to bound traversal depth
        "max_elements": 200,  # H-04: cap on interactive elements collected per snapshot
        "timeout": 10,  # H-04: global traversal timeout in seconds (across all windows)
    },
    "daemon": {
        "idle_timeout_minutes": 30,
    },
    "ipc": {
        "read_timeout_seconds": 150,  # Client seconds to wait for a daemon response (H-02)
    },
    "clipboard": {
        "method": "system",  # "system" (PowerShell) or "keyboard" (simulate Ctrl+C/V)
    },
    "input": {
        # DoS bounds for keyboard input on the single serial STA worker. type
        # sleeps per char (~0.02s), so an oversized string blocks for hours;
        # key_down --hold sleeps for the hold duration. Both caps reject the
        # over-limit param at the handler edge (INVALID_PARAMS). The module
        # constants (MAX_TYPE_TEXT_LEN / MAX_KEY_HOLD_SECONDS) stay as fallback.
        "type_text_len": 10000,  # type / multi_edit text length (SEC3-05)
        "key_hold_seconds": 60,  # key_down --hold duration (F11)
    },
    "wait": {
        "poll_interval": 0.5,  # seconds between polls when waiting for element/text
        # DoS cap: max seconds for wait / wait_for / wait_for_check. An unbounded
        # value would sleep on the single STA worker (the submit timeout only
        # reports WAIT_TIMEOUT, it can't interrupt the sleep). (SEC3-01)
        "max_seconds": 300,
    },
    "audit": {
        # Default-on audit log for destructive ops (fs_delete / fs_move /
        # process_kill / registry set|delete / system_shutdown / system_restart
        # / sleep): low-frequency / high-harm → record the target. Inverse of
        # telemetry's opt-in. The kill-switch for operators who want it off.
        # The HMAC key lives under `key` but is NOT a schema leaf here (it is
        # lazily generated + DPAPI-encrypted, like oauth_client_secret) so it is
        # not `config set`-able and not type-validated. (audit)
        "enabled": True,
    },
    "shell": {
        # M-05: optional command blacklist — list of case-insensitive regex
        # patterns. A `shell` command matching any pattern is rejected with
        # PERMISSION_DENIED (-1004) before execution. Empty list = no filtering
        # (default). Best-effort only (PowerShell aliases/encodings can bypass);
        # every attempt is also recorded in logs/shell_audit.log regardless.
        "blacklist": [],
        # TODO #13: execution timeout in seconds. Used when the caller omits an
        # explicit timeout (run_shell(timeout=None), the daemon shell handler,
        # the CLI/MCP tool). A timeout now raises TimeoutError → WAIT_TIMEOUT
        # (-1003), not a generic internal error.
        "timeout": 30,
        # G5: default cap on combined stdout+stderr in BYTES. run_shell now
        # drains the subprocess incrementally and TERMINATES it once the running
        # byte total crosses this cap — bounding daemon memory (the old
        # capture_output=True materialized the whole output before any
        # truncation) AND wall-clock (the runaway is killed at the cap). 1 MiB
        # covers normal command output while stopping a `1..1e6 | %{...}`
        # runaway from OOMing the single-threaded daemon within the timeout.
        # Mirrors telemetry._MAX_BYTES. 0 = explicit legacy opt-out (no bound,
        # no terminate — operator-acknowledged unbounded).
        "max_output_bytes": 1024 * 1024,
        # TODO #13: override the output decode encoding. Empty (default) uses
        # UTF-8 — the run_shell command prefix forces PowerShell 5.1 to emit
        # UTF-8, so the two match and non-ASCII (CJK) output decodes cleanly.
        # Set to e.g. 'gbk'/'cp1252' only if a command's native exe ignores the
        # UTF-8 request and emits its legacy codepage instead.
        "output_encoding": "",
    },
    "mcp": {
        # MCP server (agent-windows mcp serve/auth/install). auth_key is the
        # bearer token HTTP clients must present; DPAPI-encrypted at rest like
        # vlm.api_key. transport/host/port are the `serve` defaults (overridable
        # by flags). ssl_certfile/ssl_keyfile enable HTTPS for remote access.
        "transport": "stdio",
        "host": "127.0.0.1",
        "port": 8000,
        "auth_key": "",
        "ssl_certfile": "",
        "ssl_keyfile": "",
        # Opt-in to allow an unauthenticated HTTP bind to a non-loopback host
        # (the footgun guard refuses this unless set). Default off — a bare
        # non-loopback bind with no auth_key is insecure. (TODO #1)
        "allow_insecure_remote": False,
        # Run the streamable-http transport statelessly (no Mcp-Session-Id,
        # no per-connection session state) so reconnecting clients survive a
        # server restart and the server scales behind a load balancer.
        # streamable-http only; default off. (TODO #5)
        "stateless_http": False,
        # File-transfer HTTP endpoints (/files GET/HEAD/PUT) mounted on the
        # HTTP transport only. Default on (HTTP + auth already gates access);
        # set false to suppress the endpoints entirely (404). Endpoints are
        # NOT MCP tools, so [tools] include/exclude does not apply.
        "file_transfer": True,
        # Upload size cap in MiB (PUT /files). 0 = no cap (trust auth'd
        # client). Negative rejected at config build. Downloads are uncapped
        # (streamed).
        "max_upload_mb": 512,
    },
    "security": {
        # HTTP-transport access controls (sse / streamable-http). Both default
        # empty = no restriction beyond the auth_key / loopback guard. ip_allowlist
        # is set via the --ip-allowlist flag or a TOML array in config; cors_origins
        # likewise (TODO #2/#3). NOTE: `config set` only writes scalars, so to set
        # these as arrays edit config.toml directly or use the flags/env vars.
        "ip_allowlist": [],
        "cors_origins": [],
    },
    "tools": {
        # Startup tool filtering. `include` is the explicit whitelist enabled
        # via --tools (overrides exclude); `exclude` removes tools via
        # --exclude-tools. Both default empty = all tools. As with [security],
        # set as TOML arrays (edit config.toml or use flags/env) since
        # `config set` only writes scalars. (TODO #4)
        #
        # Two layers honor this: the MCP server (discovery trim + the batch
        # sub-command gate, from --tools/--exclude-tools flags/env) AND the
        # daemon itself — at startup it reads this [tools] section and rejects
        # any filtered method at route_request (PERMISSION_DENIED) before
        # dispatch, so a process connecting directly to the named pipe (bypassing
        # the MCP server) is still bound by the policy. ping/shutdown are always
        # exempt (an over-restrictive typo must not brick the daemon). Note the
        # daemon reads ONLY this config section, not the MCP --tools flag (the
        # flag never lands in config); for the daemon to enforce, write [tools]
        # here. (review #7 follow-up)
        #
        # Namespace caveat: the daemon matches on *daemon method names*, which
        # differ from MCP tool names for 3 tools — ask_vision/ask-vision,
        # wait/wait_check, wait_for/wait_for_check. Excluding the MCP name
        # "ask_vision" won't bind the daemon (use "ask-vision"); excluding "wait"
        # excludes only the sleep tool, not "wait_check". The high-risk names
        # (shell, registry, fs_delete, process_kill, …) are identical in both
        # namespaces, so this only matters for those 3. Like [security], set as
        # TOML arrays (edit config.toml) — `config set` writes scalars, so a
        # comma-joined value lands as one element and matches nothing.
        "include": [],
        "exclude": [],
    },
    "telemetry": {
        # Local-only tool-call telemetry (TODO #9). When enabled, each tool
        # call (source mcp|cli, tool name, success/failure, duration_ms) is
        # appended as one line to logs/telemetry.log. Zero network, zero new
        # deps. Default off — opt in via --telemetry / AGENT_WINDOWS_TELEMETRY
        # env / this config. write_config writes only non-defaults, so false
        # never lands in config.toml.
        "enabled": False,
    },
}

# ── MCP typed view (TODO #10) ─────────────────────────────────────
# Typed dataclasses mirroring the [mcp]/[security]/[tools] sections of
# DEFAULT_CONFIG. Field defaults MUST stay byte-identical to DEFAULT_CONFIG
# (test_mcp_config.TestDataclassDefaults enforces this). build_mcp_config
# constructs them from a validated config dict for type-safe access in
# mcp_cmd.serve(); load_config itself still returns a plain dict so the
# ~20 other consumers are untouched.


@dataclass
class ServerConfig:
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8000
    auth_key: str = ""
    ssl_certfile: str = ""
    ssl_keyfile: str = ""
    allow_insecure_remote: bool = False
    stateless_http: bool = False
    file_transfer: bool = True
    max_upload_mb: int = 512


@dataclass
class SecurityConfig:
    ip_allowlist: list[str] = field(default_factory=list)
    cors_origins: list[str] = field(default_factory=list)
    # Not in DEFAULT_CONFIG (optional reads); default empty.
    oauth_client_id: str = ""
    oauth_client_secret: str = ""


@dataclass
class ToolsConfig:
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class MCPConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    tools: ToolsConfig = field(default_factory=ToolsConfig)


def _coerce_list(value: Any) -> list:
    """Normalize a list config value to a list, wrapping a scalar string as a
    one-element list (a scalar would otherwise be character-iterated by
    ``list(...)``). Mirrors mcp_cmd._as_list; duplicated here so config.py stays
    free of the CLI layer. Defense-in-depth for programmatically-built configs;
    load_config validation already rejects scalars on list fields.
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _resolve_config_relative(raw: str, base: Path) -> str:
    """Resolve a possibly-relative config path string against *base*.

    Absolute paths and ``~``-prefixed paths are resolved in place. A relative
    path is interpreted relative to *base* (the config file's directory), NOT
    the process CWD. Empty string (the default) passes through unchanged so the
    caller's truthiness checks (``ssl_certfile or None``) still work.

    Pure path math — does NOT check file existence; that's a runtime concern
    handled by the serve-time pre-flight (``_check_tls_files`` in mcp_cmd).
    """
    if not raw:
        return ""
    expanded = Path(os.path.expanduser(raw))
    if expanded.is_absolute():
        return str(expanded.resolve())
    return str((base / expanded).resolve())


def build_mcp_config(config: dict) -> MCPConfig:
    """Build a typed :class:`MCPConfig` view from a validated config dict.

    Reads the [mcp]/[security]/[tools] sections with the same defaults as the
    dataclasses (== DEFAULT_CONFIG). Call after load_config() (already
    type-validated).
    """
    mcp = config.get("mcp", {}) or {}
    sec = config.get("security", {}) or {}
    tools = config.get("tools", {}) or {}
    # Resolve TLS cert/key relative paths against the active config file's
    # directory, NOT the process CWD (TODO #21-2). Absolute/~/empty values
    # pass through. Must be read here (process start, after any --config
    # override is applied) so the resolution base follows the override.
    # load_config()'s returned dict keeps the raw value — round-trip safe.
    config_dir = get_config_path().parent
    max_upload_mb = mcp.get("max_upload_mb", 512)
    if max_upload_mb < 0:
        raise ValueError(
            f"mcp.max_upload_mb must be >= 0 (0 = no cap), got {max_upload_mb}"
        )
    return MCPConfig(
        server=ServerConfig(
            transport=mcp.get("transport", "stdio"),
            host=mcp.get("host", "127.0.0.1"),
            port=mcp.get("port", 8000),
            auth_key=mcp.get("auth_key", ""),
            ssl_certfile=_resolve_config_relative(
                mcp.get("ssl_certfile", ""), config_dir),
            ssl_keyfile=_resolve_config_relative(
                mcp.get("ssl_keyfile", ""), config_dir),
            allow_insecure_remote=mcp.get("allow_insecure_remote", False),
            stateless_http=mcp.get("stateless_http", False),
            file_transfer=mcp.get("file_transfer", True),
            max_upload_mb=max_upload_mb,
        ),
        security=SecurityConfig(
            ip_allowlist=_coerce_list(sec.get("ip_allowlist", []) or []),
            cors_origins=_coerce_list(sec.get("cors_origins", []) or []),
            oauth_client_id=sec.get("oauth_client_id", "") or "",
            oauth_client_secret=sec.get("oauth_client_secret", "") or "",
        ),
        tools=ToolsConfig(
            include=_coerce_list(tools.get("include", []) or []),
            exclude=_coerce_list(tools.get("exclude", []) or []),
        ),
    )


# ── Load / Save ───────────────────────────────────────────────────


def load_config() -> dict[str, Any]:
    """Load config from TOML file, merging with defaults.

    Uses an in-memory cache invalidated by file modification time (mtime).
    """
    global _config_cache, _config_cache_mtime

    config_path = get_config_path()

    # Hold the lock for the check-and-update sequence to avoid races
    with _config_lock:
        # Check cache: if file unmodified, return cached config
        if _config_cache is not None and config_path.exists():
            current_mtime = config_path.stat().st_mtime
            if current_mtime == _config_cache_mtime:
                return _config_cache

        if config_path.exists():
            user_config = toml.loads(config_path.read_text(encoding="utf-8"))
        else:
            user_config = {}

        # Deep merge: user values override defaults
        merged = _deep_merge(DEFAULT_CONFIG, user_config)

        # Decrypt the api_key if it was stored as a DPAPI blob. Legacy
        # plaintext values pass through unchanged for backward compatibility.
        g = merged.get("vlm")
        if isinstance(g, dict):
            raw_key = g.get("api_key", "")
            if isinstance(raw_key, str) and raw_key.startswith("dpapi:"):
                g["api_key"] = _decrypt_secret(raw_key)

        # Same DPAPI treatment for mcp.auth_key (the HTTP bearer token).
        m = merged.get("mcp")
        if isinstance(m, dict):
            raw_mkey = m.get("auth_key", "")
            if isinstance(raw_mkey, str) and raw_mkey.startswith("dpapi:"):
                m["auth_key"] = _decrypt_secret(raw_mkey)

        # Same DPAPI treatment for audit.key (the destructive-op HMAC key),
        # lazily generated + persisted by core/audit._get_key.
        a = merged.get("audit")
        if isinstance(a, dict):
            raw_akey = a.get("key", "")
            if isinstance(raw_akey, str) and raw_akey.startswith("dpapi:"):
                a["key"] = _decrypt_secret(raw_akey)

        # Environment variable overrides API key
        env_key = os.environ.get("AGENT_WINDOWS_VLM_API_KEY", "")
        if env_key:
            merged.setdefault("vlm", {})["api_key"] = env_key

        # Strict type validation (TODO #10): reject type-mismatched values
        # (e.g. port = true, telemetry.enabled = "yes") before they reach the
        # cache/consumers. Runs only when the file mtime changes (cache guard
        # above), so there is no per-request cost.
        _validate_config(merged)

        # Update cache
        _config_cache = merged
        if config_path.exists():
            _config_cache_mtime = config_path.stat().st_mtime

    return merged


def _trim_to_non_defaults(config: dict, reference: dict = None) -> dict:
    """Return a copy of *config* containing only non-default leaves plus all
    unknown keys/sections (passed through verbatim for round-trip safety).

    Used by :func:`write_config` so the on-disk file stays minimal (TODO #10).
    """
    if reference is None:
        reference = DEFAULT_CONFIG
    out: dict[str, Any] = {}
    for key, actual in config.items():
        default_val = reference.get(key, _MISSING)
        if default_val is _MISSING:
            out[key] = actual  # unknown key/section — preserve
        elif isinstance(default_val, dict):
            if isinstance(actual, dict):
                sub = _trim_to_non_defaults(actual, default_val)
                if sub:
                    out[key] = sub
            else:
                out[key] = actual  # defensive; validation should prevent this
        else:
            if actual != default_val:
                out[key] = actual
    return out


def _encrypt_for_write(config: dict) -> dict:
    """Shallow-copy *config* and DPAPI-encrypt ``vlm.api_key`` /
    ``mcp.auth_key`` / ``audit.key`` for serialization. A key that is empty or
    already ``dpapi:``-prefixed, or a non-string value, is passed through
    unchanged. The caller's dict is not mutated (it keeps usable plaintext).
    Shared by write_config and full-write.
    """
    to_write = dict(config)
    for section, field in (("vlm", "api_key"), ("mcp", "auth_key"),
                           ("audit", "key")):
        sub = to_write.get(section)
        if isinstance(sub, dict):
            sub = dict(sub)
            key = sub.get(field, "")
            if isinstance(key, str) and key and not key.startswith("dpapi:"):
                sub[field] = _encrypt_secret(key)
            to_write[section] = sub
    return to_write


def _serialize(config: dict, path: Path) -> None:
    """Write *config* to *path* as TOML, encrypting secrets first.

    Atomic: writes to a sibling temp file then ``os.replace``s it into
    place, so a crash mid-write can't truncate the existing config (which
    also holds the DPAPI-encrypted api_key).

    The temp file is owner-only ACL'd *before* the replace (D2). On NTFS,
    ``os.replace`` (``MoveFileEx``) carries the source file's own security
    descriptor over the target, so ``path`` atomically appears with an
    owner-only DACL — there is no window where the new file sits at the
    default (possibly group-readable) DACL between replace and a later
    ``_secure_config_file``. This matters most for ``init_config``'s
    first write of default config (which carries no DPAPI layer). ``save_config``
    and ``init_config`` still call ``_secure_config_file(path)`` after as
    belt-and-braces (the call is idempotent).
    """
    data = toml.dumps(_encrypt_for_write(config))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    _secure_config_file(tmp)  # close the replace→secure TOCTOU window (D2)
    os.replace(tmp, path)


def write_config(config: dict, path: Path) -> None:
    """Serialize *config* to TOML at *path*, writing only non-default values
    (TODO #10). Unknown keys are preserved; secrets are DPAPI-encrypted.
    Does NOT set the owner-only ACL or clear the cache — callers do that.
    """
    _serialize(_trim_to_non_defaults(config), path)


def save_config(config: dict[str, Any]) -> None:
    """Save config dict to TOML file, writing only non-default values (TODO #10).

    The ``vlm.api_key``, ``mcp.auth_key`` and ``audit.key`` are DPAPI-encrypted
    at rest (handled inside :func:`write_config`); all other values are
    non-secret. The file is restricted to its owner via an ACL. The caller's
    dict is not mutated.
    """
    global _config_cache
    config_path = get_config_path()
    write_config(config, config_path)
    _secure_config_file(config_path)
    _config_cache = None  # Clear cache


def _write_full_config(config: dict[str, Any], path: Path) -> None:
    """Write the FULL config (all keys, incl. defaults) to *path*.

    Used by :func:`init_config` so a freshly-initialized config.toml serves as
    discoverable documentation of every available knob — NOT a trimmed file.
    """
    _serialize(config, path)


def init_config() -> Path:
    """Initialize config file with full defaults if it doesn't exist."""
    config_path = get_config_path()
    if not config_path.exists():
        _write_full_config(DEFAULT_CONFIG, config_path)
        _secure_config_file(config_path)
    return config_path


# Keys that are legitimately settable via ``config set`` but intentionally NOT
# in DEFAULT_CONFIG — read with a fallback elsewhere (screenshot.dir by
# get_screenshot_save_dir). Listed explicitly so the allowlist stays in sync
# with such optional reads instead of silently rejecting a valid knob. (L-07)
_OPTIONAL_SETTABLE_KEYS = frozenset({"screenshot.dir"})


def _settable_config_keys() -> frozenset[str]:
    """Dotted config keys :func:`set_config_value` may write (L-07).

    Every leaf path present in :data:`DEFAULT_CONFIG` plus the optional keys in
    :data:`_OPTIONAL_SETTABLE_KEYS`. Generated from the schema so it stays in
    sync with DEFAULT_CONFIG automatically — no hand-maintained list to drift.
    """
    leaves: list[str] = []

    def _walk(prefix: str, node: Any) -> None:
        if not isinstance(node, dict):
            leaves.append(prefix)
            return
        for k, v in node.items():
            _walk(f"{prefix}.{k}" if prefix else k, v)

    _walk("", DEFAULT_CONFIG)
    return frozenset(leaves) | _OPTIONAL_SETTABLE_KEYS


def set_config_value(key: str, value: str) -> None:
    """Set a dotted key value in config (e.g. 'vlm.model' → config['vlm']['model']).

    Value is parsed as int/float/bool if possible, otherwise kept as string.

    Only keys present in the default schema (plus a few documented optional
    keys) are accepted; any other key raises ``ValueError`` (→
    ``INVALID_PARAMS``). This is defense-in-depth for the CLI ``config set``
    path — there is no config-mutation RPC today, but an allowlist prevents an
    arbitrary key from creating a bogus section (``evil.payload``) or clobbering
    a whole section (``vlm``) by setting its name, and guards a future
    config RPC if one is added.
    """
    if key not in _settable_config_keys():
        raise ValueError(
            f"Refusing to set unknown config key {key!r}: only keys present in "
            "the default schema are settable. Run `config show` for the "
            "valid keys."
        )
    config = load_config()
    parts = key.split(".")
    # Navigate to parent dict
    d = config
    for part in parts[:-1]:
        if part not in d:
            d[part] = {}
        d = d[part]
    # Parse the value by the key's schema type so the written value passes
    # _validate_config on the next load (TODO #10). Optional/unknown keys fall
    # back to the legacy heuristic parser.
    d[parts[-1]] = _parse_value_typed(value, _default_leaf_type(key))
    save_config(config)


def get_config_value(key: str) -> Any:
    """Get a dotted key value from config."""
    config = load_config()
    parts = key.split(".")
    d = config
    for part in parts:
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def validate_config_file() -> list[str]:
    """Validate the on-disk config file, returning a list of error messages
    (empty if valid). Does not raise — the CLI ``config doctor`` uses this to
    report problems to the user. A missing file is treated as valid (defaults).

    Unlike :func:`load_config` (which raises on a bad config), this reads the
    file directly so it can report errors even when the cache holds a prior
    valid state.
    """
    config_path = get_config_path()
    if not config_path.exists():
        return []
    user_config = toml.loads(config_path.read_text(encoding="utf-8"))
    merged = _deep_merge(DEFAULT_CONFIG, user_config)
    errors: list[str] = []
    _collect_type_errors("", DEFAULT_CONFIG, merged, errors)
    return errors


def get_api_key() -> str:
    """Get vision model API key from env var or config."""
    env_key = os.environ.get("AGENT_WINDOWS_VLM_API_KEY", "")
    if env_key:
        return env_key
    config = load_config()
    return config.get("vlm", {}).get("api_key", "")


def get_screenshot_save_dir() -> Path:
    """Resolve screenshot save directory with priority chain.

    1. AGENT_WINDOWS_SCREENSHOT_DIR env var
    2. config [screenshot] dir
    3. Default ~/.agent-windows/screenshots/
    """
    env_dir = os.environ.get("AGENT_WINDOWS_SCREENSHOT_DIR", "")
    if env_dir:
        p = Path(env_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    config = load_config()
    config_dir = config.get("screenshot", {}).get("dir", "")
    if config_dir:
        p = Path(config_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    return get_screenshot_dir()


# ── Helpers ───────────────────────────────────────────────────────

# ── Strict type validation ────────────────────────────────────────

_MISSING = object()


def _check_leaf(path: str, default_val: Any, actual_val: Any, errors: list[str]) -> None:
    """Append a descriptive error to *errors* if *actual_val* is the wrong type
    for a leaf whose default is *default_val*.

    Rules (TODO #10):
      - bool default: accept only ``bool`` (reject int/str)
      - int default: accept ``int`` but NOT ``bool`` (TOML bool is an int subclass)
      - float default: accept ``int`` or ``float`` (but NOT bool)
      - str default: accept only ``str``
      - list default: accept only ``list`` (container-level; element types not checked)
      - dict default: accept only ``dict``
      - any other default type: accept (no strict rule)
    """
    expected = type(default_val)
    if expected is bool:
        ok = isinstance(actual_val, bool)
    elif expected is int:
        ok = isinstance(actual_val, int) and not isinstance(actual_val, bool)
    elif expected is float:
        ok = isinstance(actual_val, (int, float)) and not isinstance(actual_val, bool)
    elif expected is str:
        ok = isinstance(actual_val, str)
    elif expected is list:
        ok = isinstance(actual_val, list)
    elif expected is dict:
        ok = isinstance(actual_val, dict)
    else:
        ok = True
    if not ok:
        errors.append(
            f"{path}: expected {expected.__name__}, "
            f"got {type(actual_val).__name__} ({actual_val!r})"
        )


def _collect_type_errors(
    prefix: str, default_node: Any, actual_node: Any, errors: list[str]
) -> None:
    """Recursively compare *actual_node* against the *default_node* schema,
    appending one error per mistyped known leaf. Unknown keys are ignored."""
    if not isinstance(default_node, dict):
        return  # leaves are checked at the parent level
    if not isinstance(actual_node, dict):
        errors.append(
            f"{prefix or '<root>'}: expected a table, "
            f"got {type(actual_node).__name__}"
        )
        return
    for key, default_val in default_node.items():
        path = f"{prefix}.{key}" if prefix else key
        actual_val = actual_node.get(key, _MISSING)
        if actual_val is _MISSING:
            continue
        if isinstance(default_val, dict):
            _collect_type_errors(path, default_val, actual_val, errors)
        else:
            _check_leaf(path, default_val, actual_val, errors)


def _validate_config(merged: dict) -> None:
    """Validate *merged* against :data:`DEFAULT_CONFIG` types (TODO #10).

    Collects ALL type errors and raises a single ``ValueError`` listing them,
    so a user fixing a hand-edited config sees every problem at once instead
    of one-at-a-time. Unknown keys/sections pass through (backward compat).
    """
    errors: list[str] = []
    _collect_type_errors("", DEFAULT_CONFIG, merged, errors)
    if errors:
        raise ValueError(
            "Invalid config.toml — type errors:\n  " + "\n  ".join(errors)
        )


# ── Secret protection (DPAPI) ──────────────────────────────────────


def _encrypt_secret(plaintext: str) -> str:
    """Protect a secret with Windows DPAPI (current-user scope).

    Returns ``dpapi:<base64 blob>`` for storage. Only the same Windows account
    can unprotect it, so a file copied to another account/machine or grabbed by
    a backup yields an unusable blob (M-01). ``win32crypt`` is imported lazily
    so configs without a stored key never pay the import cost.
    """
    import base64
    import win32crypt

    blob = win32crypt.CryptProtectData(
        plaintext.encode("utf-8"), None, None, None, None, 0
    )
    return "dpapi:" + base64.b64encode(blob).decode("ascii")


def _decrypt_secret(value: str) -> str:
    """Unprotect a ``dpapi:``-prefixed secret; pass plaintext through unchanged.

    Non-prefixed values (legacy plaintext keys, or hand-edited entries) are
    returned as-is so existing configs keep working. A blob that cannot be
    unprotected (corrupt, or not from this account) yields ``""`` — the key is
    treated as unset rather than surfacing a raw blob upstream (M-01).
    """
    if not isinstance(value, str) or not value.startswith("dpapi:"):
        return value
    import base64
    import win32crypt

    try:
        blob = base64.b64decode(value[len("dpapi:"):])
        _, plaintext = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        return plaintext.decode("utf-8")
    except Exception:
        # Fail closed (treat as unset), but call out the most common root cause
        # so the failure is debuggable: DPAPI keys are per-Windows-account, so a
        # blob copied across accounts/machines (backup restore, roaming profile,
        # running the daemon under a different service account) can't be
        # unprotected here. The fix is to re-set the key under this account
        # (`config set vlm.api_key ...`). (D3)
        logger.warning(
            "Failed to decrypt DPAPI-protected secret; treating as unset. The "
            "blob is likely encrypted under a different Windows account (e.g. "
            "config copied across machines/accounts, a backup restore, a roaming "
            "profile, or the daemon running under another service account). "
            "Re-set it under this account (e.g. `config set vlm.api_key ...` or "
            "`config set mcp.auth_key ...`).",
            exc_info=True,
        )
        return ""


def _apply_owner_only_acl(path: Path, inheritable: bool = False) -> None:
    """Set a protected owner-only DACL on a file or directory.

    The single ACE grants the current user full access; the DACL is protected
    from parent-dir inheritance (SE_DACL_PROTECTED) so no inherited group ACE can
    broaden access. When ``inheritable``, the ACE also carries OBJECT_INHERIT |
    CONTAINER_INHERIT so files and subdirs created inside a directory inherit
    owner-only access — used for ``logs/`` so daemon.log, shell_audit.log, and
    rotated backups are all owner-only from one directory-level ACL (no per-file
    calls).

    **Best-effort — and intentionally so** (SEC3-07). A failure is warned, not
    raised. The threat model differs by caller:
      - ``config.toml``: the api_key is ALSO DPAPI-encrypted at rest (M-01), so
        even if this ACL fails the key remains unreadable to other users. ACL
        here is defense-in-depth.
      - ``logs/`` (``shell_audit.log`` records full PowerShell commands and may
        carry secrets; ``daemon.log`` may carry runtime data): there is NO DPAPI
        layer, so this ACL is the SOLE access control. If it fails, those logs
        fall back to the inherited directory default, which on some Windows
        configurations is world- or group-readable.
    Unlike the named pipe (M-02), which fails CLOSED (the daemon aborts if its
    security descriptor can't be built), files do not fail closed — a process
    that can't lock its logs still needs to run. Operators in multi-user or
    high-security environments should verify ``logs/`` ACLs are owner-only after
    first daemon start (the warning log line is the signal), or relocate the
    data dir to an already-restricted location. (M-01)
    """
    import ntsecuritycon
    import win32api
    import win32security

    try:
        user_sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
        inheritance_flags = (
            ntsecuritycon.OBJECT_INHERIT_ACE | ntsecuritycon.CONTAINER_INHERIT_ACE
            if inheritable
            else 0
        )
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            inheritance_flags,
            ntsecuritycon.FILE_ALL_ACCESS,
            user_sid,
        )
        sd = win32security.SECURITY_DESCRIPTOR()
        sd.SetSecurityDescriptorDacl(1, dacl, 0)
        # Protect the DACL from parent-dir inheritance so the target is exactly
        # owner-only — no inherited group ACEs could broaden access.
        sd.SetSecurityDescriptorControl(
            win32security.SE_DACL_PROTECTED, win32security.SE_DACL_PROTECTED
        )
        win32security.SetFileSecurity(
            str(path), win32security.DACL_SECURITY_INFORMATION, sd
        )
    except Exception:
        logger.warning("Failed to set owner-only ACL on %s", path, exc_info=True)


def _secure_config_file(path: Path) -> None:
    """Restrict a config file to its owner (non-inheritable owner-only DACL).

    Replaces the POSIX ``chmod(0o600)`` no-op. Defense-in-depth: the api_key is
    already DPAPI-encrypted at rest, so best-effort is acceptable. (M-01)
    """
    _apply_owner_only_acl(path, inheritable=False)


def _secure_directory(path: Path) -> None:
    """Restrict a directory to its owner with an inheritable owner-only DACL.

    New files and subdirs created inside inherit owner-only access, so the whole
    directory stays owner-only from a single ACL — used for ``logs/`` (M-01):
    full shell commands recorded there may carry secrets, and unlike
    ``config.toml`` there are many files (daemon.log, shell_audit.log, rotated
    backups). ``get_log_dir`` calls this once per process.
    """
    _apply_owner_only_acl(path, inheritable=True)


def _deep_merge(base: dict, override: dict) -> dict:
    """Merge override into base (deep copy, recursive for nested dicts).

    The base (DEFAULT_CONFIG) must never be mutated: it is the validation
    schema and the merge source for every later load. A shallow top-level copy
    would alias base's nested dicts, letting callers that mutate the merged
    result (e.g. set_config_value writes back into the cached merge) corrupt
    DEFAULT_CONFIG in place. So we deep-copy base on every merge. The override
    is trusted user data, read-only here.
    """
    result = copy.deepcopy(base)
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _parse_value(value: str) -> Any:
    """Try to parse a string as bool, int, float, or keep as string."""
    if value.lower() in ("true", "yes", "1"):
        return True
    if value.lower() in ("false", "no", "0"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _default_leaf_type(key: str) -> type | None:
    """Return the type of a dotted key's default value in DEFAULT_CONFIG.

    Returns ``None`` for unknown keys and for section names (non-leaf paths).
    Used by :func:`set_config_value` to parse the CLI string into the right
    type so a written value is always accepted by :func:`_validate_config`.
    """
    node: Any = DEFAULT_CONFIG
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return None if isinstance(node, dict) else type(node)


def _parse_value_typed(value: str, expected_type: type | None) -> Any:
    """Parse a CLI string according to *expected_type* (TODO #10 / S3).

    Unlike the legacy :func:`_parse_value`, this never bool-coerces a value
    destined for an int/str key (so ``"0"`` -> ``0`` not ``False``, and
    ``"true"`` -> ``"true"`` for a str key), and wraps a scalar for a list
    key. Unknown/optional keys (``expected_type is None``) fall back to
    :func:`_parse_value` for backward compatibility.
    """
    if expected_type is bool:
        if value.lower() in ("true", "yes", "1"):
            return True
        if value.lower() in ("false", "no", "0"):
            return False
        raise ValueError(f"expected bool, got {value!r}")
    if expected_type is int:
        # int("1.5") raises an opaque "invalid literal" ValueError — translate
        # to a clear message so `config set <int-key> 1.5` explains the problem
        # rather than dumping a raw int() traceback.
        try:
            return int(value)
        except ValueError:
            raise ValueError(f"expected an integer, got {value!r}")
    if expected_type is float:
        try:
            return float(value)
        except ValueError:
            raise ValueError(f"expected a float, got {value!r}")
    if expected_type is list:
        return [value]
    if expected_type is str:
        return value  # never coerce a string key's value
    return _parse_value(value)
