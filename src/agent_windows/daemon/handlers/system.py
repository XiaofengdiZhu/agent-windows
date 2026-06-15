"""System handlers: app management, shell, filesystem, clipboard, process."""

from typing import Any

from agent_windows.daemon.router import handler, require


# ── Application management ─────────────────────────────────────────


@handler("app_launch")
def _app_launch(params: dict) -> dict[str, Any]:
    """Launch an application."""
    from agent_windows.core.app_manager import launch_app
    return launch_app(require(params, "name_or_path", str), params.get("args"))


@handler("app_switch")
def _app_switch(params: dict) -> dict[str, Any]:
    """Switch to an application window."""
    from agent_windows.core.app_manager import switch_window
    return switch_window(require(params, "name_or_title", str))


@handler("app_resize")
def _app_resize(params: dict) -> dict[str, Any]:
    """Resize an application window."""
    from agent_windows.core.app_manager import resize_window
    return resize_window(
        require(params, "name_or_title", str),
        require(params, "width"), require(params, "height"),
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
    """Execute a PowerShell command."""
    from agent_windows.core.shell_runner import run_shell
    return run_shell(require(params, "command", str))


# ── Filesystem ──────────────────────────────────────────────────────


@handler("fs_read")
def _fs_read(params: dict) -> dict[str, Any]:
    """Read a file."""
    from agent_windows.core.filesystem import read_file
    return read_file(require(params, "path", str))


@handler("fs_write")
def _fs_write(params: dict) -> dict[str, Any]:
    """Write to a file."""
    from agent_windows.core.filesystem import write_file
    return write_file(require(params, "path", str), require(params, "content", str))


@handler("fs_list")
def _fs_list(params: dict) -> dict[str, Any]:
    """List directory contents."""
    from agent_windows.core.filesystem import list_dir
    return list_dir(require(params, "path", str))


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
    """List running processes."""
    from agent_windows.core.process_manager import list_processes
    return list_processes()


@handler("process_kill")
def _process_kill(params: dict) -> dict[str, Any]:
    """Kill a process."""
    from agent_windows.core.process_manager import kill_process
    return kill_process(require(params, "pid_or_name", (str, int)))
