"""System handlers: app management, shell, filesystem, clipboard, process."""

from typing import Any

from agent_windows.daemon.router import handler, require


# ── Application management ─────────────────────────────────────────


@handler("app_launch")
def _app_launch(params: dict) -> dict[str, Any]:
    """Launch an application."""
    from agent_windows.core.app_manager import launch_app
    args = params.get("args")
    if args is not None:
        # args must be a list of strings; anything else crashes Popen or
        # smuggles unintended argument shapes. (SEC3-06)
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("parameter 'args' must be a list of strings")
    return launch_app(require(params, "name_or_path", str), args)


@handler("app_switch")
def _app_switch(params: dict) -> dict[str, Any]:
    """Switch to an application window."""
    from agent_windows.core.app_manager import switch_window
    return switch_window(require(params, "name_or_title", str))


def _positive_int(params: dict, key: str) -> int:
    """Extract a required parameter that must be a positive int (e.g. window
    width/height). Rejects non-ints and non-positive values as INVALID_PARAMS."""
    value = require(params, key, int)
    if isinstance(value, bool) or value <= 0:
        raise ValueError(f"parameter '{key}' must be a positive integer, got {value!r}")
    return value


@handler("app_resize")
def _app_resize(params: dict) -> dict[str, Any]:
    """Resize an application window."""
    from agent_windows.core.app_manager import resize_window
    return resize_window(
        require(params, "name_or_title", str),
        _positive_int(params, "width"), _positive_int(params, "height"),
        params.get("x", -1), params.get("y", -1),
    )


@handler("app_list")
def _app_list(params: dict) -> dict[str, Any]:
    """List all visible windows."""
    from agent_windows.core.app_manager import list_windows
    return list_windows()


# ── Shell ───────────────────────────────────────────────────────────


@handler("shell")
def _shell(params: dict) -> dict[str, Any]:
    """Execute a PowerShell command.

    Optional ``timeout`` (seconds) overrides ``shell.timeout`` config. Omitted
    → run_shell reads the config default. A value of 0 is treated as "use the
    config default" too (0 would mean instant timeout), matching the CLI's
    ``--timeout 0`` semantics. (TODO #13)
    """
    from agent_windows.core.shell_runner import run_shell
    timeout = params.get("timeout")
    # An MCP/JSON client may pass timeout as a float, int, or (erroneously) a
    # string/bool. Coerce real numbers; 0 → None ("use config default", since
    # a literal 0 would mean instant timeout). Reject non-numeric values with
    # a clear INVALID_PARAMS error instead of letting run_shell crash on a bad
    # type (a bool True == 1 would otherwise silently pass `== 0` and become a
    # 1s timeout).
    if isinstance(timeout, bool) or (timeout is not None and not isinstance(timeout, (int, float))):
        raise ValueError("timeout must be a number (seconds)")
    if timeout == 0:
        timeout = None
    return run_shell(require(params, "command", str), timeout=timeout)


# ── Filesystem ──────────────────────────────────────────────────────


@handler("fs_read")
def _fs_read(params: dict) -> dict[str, Any]:
    """Read a file. ``offset``/``limit`` select a 1-based line range;
    ``encoding`` overrides UTF-8 for legacy codepage files (TODO #20b)."""
    from agent_windows.core.filesystem import read_file
    return read_file(
        require(params, "path", str),
        offset=params.get("offset"),
        limit=params.get("limit"),
        encoding=params.get("encoding"),
    )


@handler("fs_write")
def _fs_write(params: dict) -> dict[str, Any]:
    """Write to a file. ``append=True`` appends instead of overwriting."""
    from agent_windows.core.filesystem import write_file
    return write_file(
        require(params, "path", str),
        require(params, "content", str),
        append=params.get("append", False),
    )


@handler("fs_list")
def _fs_list(params: dict) -> dict[str, Any]:
    """List directory contents.

    ``pattern`` filters names, ``recursive=True`` walks subdirs, ``show_hidden``
    keeps dotfiles. All optional — bare ``fs_list`` behaves as before.
    """
    from agent_windows.core.filesystem import list_dir
    return list_dir(
        require(params, "path", str),
        pattern=params.get("pattern"),
        recursive=params.get("recursive", False),
        show_hidden=params.get("show_hidden", False),
    )


@handler("fs_copy")
def _fs_copy(params: dict) -> dict[str, Any]:
    """Copy a file or directory. ``overwrite=True`` replaces an existing target."""
    from agent_windows.core.filesystem import copy_path
    return copy_path(
        require(params, "path", str),
        require(params, "destination", str),
        overwrite=params.get("overwrite", False),
    )


@handler("fs_move")
def _fs_move(params: dict) -> dict[str, Any]:
    """Move/rename a file or directory. ``overwrite=True`` replaces an existing target."""
    from agent_windows.core.filesystem import move_path
    return move_path(
        require(params, "path", str),
        require(params, "destination", str),
        overwrite=params.get("overwrite", False),
    )


@handler("fs_delete")
def _fs_delete(params: dict) -> dict[str, Any]:
    """Delete a file or directory. Default recycles; permanent=True force-deletes.
    dry_run=True previews without deleting. Non-empty dirs need recursive=True."""
    from agent_windows.core.filesystem import delete_path
    return delete_path(
        require(params, "path", str),
        recursive=params.get("recursive", False),
        permanent=params.get("permanent", False),
        dry_run=params.get("dry_run", False),
    )


@handler("fs_search")
def _fs_search(params: dict) -> dict[str, Any]:
    """Find files/dirs matching a glob ``pattern`` (recursive by default)."""
    from agent_windows.core.filesystem import search_files
    return search_files(
        require(params, "path", str),
        require(params, "pattern", str),
        recursive=params.get("recursive", True),
    )


@handler("fs_info")
def _fs_info(params: dict) -> dict[str, Any]:
    """Return metadata about a file or directory."""
    from agent_windows.core.filesystem import get_file_info
    return get_file_info(require(params, "path", str))


# ── Clipboard ───────────────────────────────────────────────────────


def _default_keyboard() -> bool:
    """Default clipboard keyboard flag from config (true when method == 'keyboard')."""
    from agent_windows.utils.config import load_config
    return load_config().get("clipboard", {}).get("method", "system") == "keyboard"


@handler("clipboard_get")
def _clipboard_get(params: dict) -> dict[str, Any]:
    """Get clipboard content."""
    from agent_windows.core.clipboard import get_clipboard
    keyboard = params.get("keyboard", _default_keyboard())
    return get_clipboard(keyboard=keyboard)


@handler("clipboard_set")
def _clipboard_set(params: dict) -> dict[str, Any]:
    """Set clipboard content."""
    from agent_windows.core.clipboard import set_clipboard
    keyboard = params.get("keyboard", _default_keyboard())
    return set_clipboard(require(params, "text", str), keyboard=keyboard)


# ── Process management ──────────────────────────────────────────────


@handler("process_list")
def _process_list(params: dict) -> dict[str, Any]:
    """List running processes.

    Optional ``sort_by`` (memory/cpu/name), ``limit`` (top-N, 0=all), and
    ``name`` (case-insensitive substring filter) refine the output.
    """
    from agent_windows.core.process_manager import list_processes
    return list_processes(
        sort_by=params.get("sort_by", "memory"),
        limit=params.get("limit", 0),
        name=params.get("name"),
    )


@handler("process_kill")
def _process_kill(params: dict) -> dict[str, Any]:
    """Kill a process. ``force=True`` 强杀 (immediate); default is graceful."""
    from agent_windows.core.process_manager import kill_process
    return kill_process(
        require(params, "pid_or_name", (str, int)),
        force=params.get("force", False),
    )


# ── Notifications ────────────────────────────────────────────────────


@handler("notify")
def _notify(params: dict) -> dict[str, Any]:
    """Send a Windows toast notification (title + message). (TODO #10)

    ``app_id`` defaults to PowerShell's AppUserModelID (always present, no
    registration needed); a custom AUMID needs a registered Start Menu shortcut
    to display reliably.
    """
    from agent_windows.core.notifier import send_notification, DEFAULT_APP_ID
    return send_notification(
        require(params, "title", str),
        require(params, "message", str),
        app_id=params.get("app_id") or DEFAULT_APP_ID,
    )


# ── Registry ─────────────────────────────────────────────────────────


@handler("registry")
def _registry(params: dict) -> dict[str, Any]:
    """Windows Registry operations: get/set/delete/list. (TODO #11)

    ``mode`` selects the operation; ``path`` is a PowerShell-drive path
    (e.g. ``HKCU:\\Software\\MyApp``). ``set`` requires ``name``+``value``;
    ``get`` requires ``name``; ``delete`` takes optional ``name`` (omit to
    delete the whole key); ``list`` takes only ``path``.
    """
    from agent_windows.core import registry
    mode = require(params, "mode", str)
    path = require(params, "path", str)
    if mode == "get":
        return registry.get_value(path, require(params, "name", str))
    if mode == "set":
        return registry.set_value(
            path,
            require(params, "name", str),
            require(params, "value"),
            reg_type=params.get("type") or "String",
        )
    if mode == "delete":
        # name absent → delete the whole key; name="" → delete the unnamed
        # (default) value. Distinguish "key not in params" from a falsy name so
        # an explicit empty string targets the default value, not the whole key. (BUG-B)
        name = params["name"] if "name" in params else None
        return registry.delete_entry(path, name=name)
    if mode == "list":
        return registry.list_key(path)
    raise ValueError(
        f"mode must be 'get', 'set', 'delete', or 'list' (got {mode!r})"
    )


# ── Power ────────────────────────────────────────────────────────────


def _delay_and_force(params: dict) -> tuple[int, bool]:
    """Validate + coerce the shared shutdown/restart params. ``delay`` must be
    a non-bool int in [0, MAX_SHUTDOWN_DELAY]; ``force`` coerces to bool."""
    from agent_windows.core.power import MAX_SHUTDOWN_DELAY
    delay = params.get("delay", 0)
    if isinstance(delay, bool) or not isinstance(delay, int) or not (0 <= delay <= MAX_SHUTDOWN_DELAY):
        raise ValueError(f"delay must be an int in [0, {MAX_SHUTDOWN_DELAY}]")
    return delay, bool(params.get("force", False))


@handler("lock")
def _lock(params: dict) -> dict[str, Any]:
    """Lock the workstation (Win+L)."""
    from agent_windows.core.power import lock_workstation
    return lock_workstation()


@handler("sleep")
def _sleep(params: dict) -> dict[str, Any]:
    """Suspend the system to sleep (S3)."""
    from agent_windows.core.power import suspend_sleep
    return suspend_sleep()


@handler("system_shutdown")
def _system_shutdown(params: dict) -> dict[str, Any]:
    """Shut down. ``delay`` (0–600s, default 0); ``force`` (default graceful).

    Registered as ``system_shutdown`` (not ``shutdown``) to avoid clobbering
    the daemon's built-in lifecycle ``shutdown`` handler (router.py) that
    ``daemon stop`` relies on — a power-off must never hijack that name."""
    from agent_windows.core.power import shutdown_system
    delay, force = _delay_and_force(params)
    return shutdown_system(delay, force)


@handler("system_restart")
def _system_restart(params: dict) -> dict[str, Any]:
    """Restart. ``delay`` (0–600s, default 0); ``force`` (default graceful)."""
    from agent_windows.core.power import restart_system
    delay, force = _delay_and_force(params)
    return restart_system(delay, force)
