"""Configuration management: TOML file read/write.

Config file: ~/.agent-windows/config.toml
Data directory: ~/.agent-windows/
"""

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import toml

logger = logging.getLogger(__name__)

# ── Config cache ───────────────────────────────────────────────────

_config_cache: dict[str, Any] | None = None
_config_cache_mtime: float = 0.0
_config_lock = threading.Lock()

# ── Paths ─────────────────────────────────────────────────────────


def get_data_dir() -> Path:
    """Return the agent-windows data directory (~/.agent-windows/), creating it if needed."""
    d = Path.home() / ".agent-windows"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_config_path() -> Path:
    """Return path to config.toml."""
    return get_data_dir() / "config.toml"


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
    "wait": {
        "poll_interval": 0.5,  # seconds between polls when waiting for element/text
    },
    "shell": {
        # M-05: optional command blacklist — list of case-insensitive regex
        # patterns. A `shell` command matching any pattern is rejected with
        # PERMISSION_DENIED (-1004) before execution. Empty list = no filtering
        # (default). Best-effort only (PowerShell aliases/encodings can bypass);
        # every attempt is also recorded in logs/shell_audit.log regardless.
        "blacklist": [],
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
    },
    "security": {
        # HTTP-transport access controls (sse / streamable-http). Both default
        # empty = no restriction beyond the auth_key / loopback guard. Set via
        # `config set security.ip_allowlist ...` (a TOML array of strings) or the
        # `--ip-allowlist` / `--cors-origins` flags. (TODO #2/#3)
        "ip_allowlist": [],
        "cors_origins": [],
    },
}

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

        # Environment variable overrides API key
        env_key = os.environ.get("AGENT_WINDOWS_VLM_API_KEY", "")
        if env_key:
            merged.setdefault("vlm", {})["api_key"] = env_key

        # Update cache
        _config_cache = merged
        if config_path.exists():
            _config_cache_mtime = config_path.stat().st_mtime

    return merged


def save_config(config: dict[str, Any]) -> None:
    """Save config dict to TOML file.

    The ``vlm.api_key`` is DPAPI-encrypted at rest; all other values are
    non-secret. The file is also restricted to its owner via an ACL. The
    caller's dict is not mutated — encryption is applied to a shallow copy used
    only for serialization, so the in-memory config keeps the usable key.
    """
    global _config_cache
    config_path = get_config_path()

    # Encrypt the api_key on a copy so the caller's dict keeps the plaintext.
    to_write = dict(config)
    vlm = to_write.get("vlm")
    if isinstance(vlm, dict):
        vlm = dict(vlm)
        key = vlm.get("api_key", "")
        # Mirror the decrypt-side guard: only a real non-empty plaintext string
        # gets encrypted. A pre-prefixed dpapi: blob is written through unchanged
        # (avoids double-encryption), and a non-string key (e.g. a hand-edited
        # ``api_key = 123``) is left as-is rather than crashing in .encode().
        if isinstance(key, str) and key and not key.startswith("dpapi:"):
            vlm["api_key"] = _encrypt_secret(key)
        to_write["vlm"] = vlm

    # Same for mcp.auth_key.
    mcp = to_write.get("mcp")
    if isinstance(mcp, dict):
        mcp = dict(mcp)
        mkey = mcp.get("auth_key", "")
        if isinstance(mkey, str) and mkey and not mkey.startswith("dpapi:"):
            mcp["auth_key"] = _encrypt_secret(mkey)
        to_write["mcp"] = mcp

    config_path.write_text(toml.dumps(to_write), encoding="utf-8")
    _secure_config_file(config_path)
    _config_cache = None  # Clear cache


def init_config() -> Path:
    """Initialize config file with defaults if it doesn't exist."""
    config_path = get_config_path()
    if not config_path.exists():
        save_config(DEFAULT_CONFIG)
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
    # Set the leaf value
    d[parts[-1]] = _parse_value(value)
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
        logger.warning(
            "Failed to decrypt DPAPI-protected api_key; treating as unset",
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
    """Merge override into base (shallow copy, recursive for nested dicts)."""
    result = dict(base)
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
