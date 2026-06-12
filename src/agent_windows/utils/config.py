"""Configuration management: TOML file read/write.

Config file: ~/.agent-windows/config.toml
Data directory: ~/.agent-windows/
"""

import os
from pathlib import Path
from typing import Any

import toml

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


def get_log_dir() -> Path:
    d = get_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
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
    "grounding": {
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
    },
    "screenshot": {
        "backend": "auto",
        "scale": 1.0,
        "max_width": 1920,
        "max_height": 1080,
        "format": "png",
        "jpeg_quality": 85,
        "max_screenshot_age_days": 7,
        "max_screenshot_dir_mb": 500,
        "cleanup_interval_minutes": 10,
    },
    "tree": {
        "include_invisible": False,
        "include_offscreen": False,
        "max_depth": 30,
    },
    "daemon": {
        "idle_timeout_minutes": 30,
    },
    "clipboard": {
        "method": "system",  # "system" (PowerShell) or "keyboard" (simulate Ctrl+C/V)
    },
    "wait": {
        "poll_interval": 0.5,  # seconds between polls when waiting for element/text
    },
}

# ── Load / Save ───────────────────────────────────────────────────


def load_config() -> dict[str, Any]:
    """Load config from TOML file, merging with defaults."""
    config_path = get_config_path()
    if config_path.exists():
        user_config = toml.loads(config_path.read_text(encoding="utf-8"))
    else:
        user_config = {}

    # Deep merge: user values override defaults
    merged = _deep_merge(DEFAULT_CONFIG, user_config)
    return merged


def save_config(config: dict[str, Any]) -> None:
    """Save config dict to TOML file."""
    config_path = get_config_path()
    config_path.write_text(toml.dumps(config), encoding="utf-8")


def init_config() -> Path:
    """Initialize config file with defaults if it doesn't exist."""
    config_path = get_config_path()
    if not config_path.exists():
        save_config(DEFAULT_CONFIG)
    return config_path


def set_config_value(key: str, value: str) -> None:
    """Set a dotted key value in config (e.g. 'grounding.model' → config['grounding']['model']).

    Value is parsed as int/float/bool if possible, otherwise kept as string.
    """
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
    """Get grounding API key from env var or config."""
    env_key = os.environ.get("AGENT_WINDOWS_GROUNDING_API_KEY", "")
    if env_key:
        return env_key
    config = load_config()
    return config.get("grounding", {}).get("api_key", "")


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
