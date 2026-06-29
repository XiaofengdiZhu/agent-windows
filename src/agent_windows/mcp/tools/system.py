"""System MCP tools — apps, shell, filesystem, clipboard, process, notify,
registry. These map 1:1 to the daemon's system handlers."""
from typing import Annotated, Any, Literal

from mcp.types import ToolAnnotations
from pydantic import Field

from agent_windows.mcp._call import _call, _as_bool


# ── apps ───────────────────────────────────────────────────────────────

def app_launch_fn(name_or_path: str, args: list[str] | None = None) -> str:
    if args is not None and (not isinstance(args, list) or not all(isinstance(a, str) for a in args)):
        raise ValueError("args must be a list of strings")
    return str(_call("app_launch", name_or_path=name_or_path, args=args).get("output", ""))


def app_switch_fn(name_or_title: str) -> str:
    return str(_call("app_switch", name_or_title=name_or_title).get("output", ""))


def app_resize_fn(name_or_title: str, width: int, height: int,
                  x: int = -1, y: int = -1) -> str:
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive integers")
    return str(_call("app_resize", name_or_title=name_or_title, width=width,
                     height=height, x=x, y=y).get("output", ""))


def app_list_fn() -> str:
    return str(_call("app_list").get("output", ""))


# ── shell ──────────────────────────────────────────────────────────────

def shell_fn(command: str, timeout: int | None = None) -> str:
    return str(_call("shell", command=command, timeout=timeout).get("output", ""))


# ── filesystem ─────────────────────────────────────────────────────────

def fs_read_fn(path: str, offset: int | None = None, limit: int | None = None,
               encoding: str | None = None) -> str:
    return str(_call("fs_read", path=path, offset=offset, limit=limit,
                     encoding=encoding).get("output", ""))


def fs_write_fn(path: str, content: str, append: bool | str = False) -> str:
    if content is None:
        raise ValueError("content is required for write")
    return str(_call("fs_write", path=path, content=content,
                     append=_as_bool(append)).get("output", ""))


def fs_list_fn(path: str, pattern: str | None = None,
               recursive: bool | str = False, show_hidden: bool | str = False) -> str:
    return str(_call("fs_list", path=path, pattern=pattern,
                     recursive=_as_bool(recursive),
                     show_hidden=_as_bool(show_hidden)).get("output", ""))


def fs_copy_fn(path: str, destination: str, overwrite: bool | str = False) -> str:
    return str(_call("fs_copy", path=path, destination=destination,
                     overwrite=_as_bool(overwrite)).get("output", ""))


def fs_move_fn(path: str, destination: str, overwrite: bool | str = False) -> str:
    return str(_call("fs_move", path=path, destination=destination,
                     overwrite=_as_bool(overwrite)).get("output", ""))


def fs_delete_fn(path: str, recursive: bool | str = False) -> str:
    return str(_call("fs_delete", path=path, recursive=_as_bool(recursive)).get("output", ""))


def fs_search_fn(path: str, pattern: str, recursive: bool | str = True) -> str:
    return str(_call("fs_search", path=path, pattern=pattern,
                     recursive=_as_bool(recursive)).get("output", ""))


def fs_info_fn(path: str) -> str:
    return str(_call("fs_info", path=path).get("output", ""))


# ── clipboard ──────────────────────────────────────────────────────────

def clipboard_get_fn(keyboard: bool | str = False) -> str:
    return str(_call("clipboard_get", keyboard=_as_bool(keyboard) or None).get("output", ""))


def clipboard_set_fn(text: str, keyboard: bool | str = False) -> str:
    return str(_call("clipboard_set", text=text, keyboard=_as_bool(keyboard) or None).get("output", ""))


# ── process ────────────────────────────────────────────────────────────

def process_list_fn(
    sort_by: str = "memory",
    limit: int = 0,
    name: str | None = None,
) -> str:
    return str(_call("process_list", sort_by=sort_by, limit=limit,
                     name=name).get("output", ""))


def process_kill_fn(pid_or_name: int | str, force: bool | str = False) -> str:
    return str(_call("process_kill", pid_or_name=pid_or_name,
                     force=_as_bool(force) or None).get("output", ""))


# ── notify ─────────────────────────────────────────────────────────────

def notify_fn(title: str, message: str, app_id: str | None = None) -> str:
    return str(_call("notify", title=title, message=message, app_id=app_id).get("output", ""))


# ── registry ───────────────────────────────────────────────────────────

def registry_fn(
    mode: Literal["get", "set", "delete", "list"],
    path: str,
    name: str | None = None,
    value: Any = None,
    type: str = "String",
) -> str:
    if mode not in ("get", "set", "delete", "list"):
        raise ValueError("mode must be get/set/delete/list")
    return str(_call("registry", mode=mode, path=path, name=name,
                     value=value, type=type).get("output", ""))


# ── registration ───────────────────────────────────────────────────────

def register(mcp) -> None:
    _REG = ToolAnnotations

    @mcp.tool(name="app_launch", description=(
        "Launch an application by name (e.g. 'notepad') or path. "
        "Keywords: launch app, open program, start application, run exe. "
        "args is an optional list of string arguments."
    ), annotations=_REG(title="App Launch", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def _app_launch(name_or_path: str, args: list[str] | None = None) -> str:
        return app_launch_fn(name_or_path=name_or_path, args=args)

    @mcp.tool(name="app_switch", description=(
        "Switch focus to a window by name/title. "
        "Keywords: switch window, focus app, bring to front, activate window, alt-tab."
    ), annotations=_REG(title="App Switch", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def _app_switch(name_or_title: str) -> str:
        return app_switch_fn(name_or_title=name_or_title)

    @mcp.tool(name="app_resize", description=(
        "Resize a window by name/title. width/height required (positive ints). "
        "Keywords: resize window, move window, window size, reposition. "
        "x/y position optional (-1 = leave)."
    ), annotations=_REG(title="App Resize", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def _app_resize(name_or_title: str, width: int, height: int,
                    x: int = -1, y: int = -1) -> str:
        return app_resize_fn(name_or_title=name_or_title, width=width, height=height, x=x, y=y)

    @mcp.tool(name="app_list", description=(
        "List all visible windows. Keywords: list windows, open apps, "
        "running applications, window titles."
    ), annotations=_REG(title="App List", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _app_list() -> str:
        return app_list_fn()

    @mcp.tool(name="shell", description=(
        "Run a PowerShell command and return stdout/stderr. "
        "Keywords: shell, command line, powershell, cmd, run command, terminal, exec. "
        "timeout (seconds) overrides the config default (shell.timeout, 30); large output "
        "is truncated past shell.max_output_bytes (0 = no cap)."
    ), annotations=_REG(title="Shell", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def _shell(command: str, timeout: int | None = None) -> str:
        return shell_fn(command=command, timeout=timeout)

    @mcp.tool(name="fs_read", description=(
        "Read a text file. Keywords: read file, cat, view file, file content, "
        "open file. offset/limit select a 1-based line range (0 or omitted = "
        "whole file; otherwise positive ints; limit is capped at 100000 lines "
        "and large files are streamed so a multi-GB file is not read whole). "
        "encoding overrides UTF-8 for legacy codepage files (e.g. 'gbk', "
        "'latin-1')."
    ), annotations=_REG(title="FS Read", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _fs_read(path: str, offset: int | None = None, limit: int | None = None,
                 encoding: Annotated[str | None, Field(
                     description="Override the default UTF-8 decode for legacy codepage files (e.g. 'gbk', 'latin-1', 'cp1252'). Omit for UTF-8."
                 )] = None) -> str:
        return fs_read_fn(path=path, offset=offset, limit=limit, encoding=encoding)

    @mcp.tool(name="fs_write", description=(
        "Write to a file. Keywords: write file, save, create file, edit file, "
        "append. append=True appends instead of overwriting."
    ), annotations=_REG(title="FS Write", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _fs_write(path: str, content: str, append: bool | str = False) -> str:
        return fs_write_fn(path=path, content=content, append=append)

    @mcp.tool(name="fs_list", description=(
        "List a directory. Keywords: list directory, ls, dir, folder contents, "
        "list files. pattern filters names; recursive walks subdirs; "
        "show_hidden keeps dotfiles."
    ), annotations=_REG(title="FS List", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _fs_list(path: str, pattern: str | None = None,
                 recursive: bool | str = False, show_hidden: bool | str = False) -> str:
        return fs_list_fn(path=path, pattern=pattern, recursive=recursive, show_hidden=show_hidden)

    @mcp.tool(name="fs_copy", description=(
        "Copy a file/dir. Keywords: copy, duplicate, cp, clone file. "
        "overwrite=True replaces target."
    ), annotations=_REG(title="FS Copy", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _fs_copy(path: str, destination: str, overwrite: bool | str = False) -> str:
        return fs_copy_fn(path=path, destination=destination, overwrite=overwrite)

    @mcp.tool(name="fs_move", description=(
        "Move/rename a file/dir. Keywords: move, rename, mv, relocate file. "
        "overwrite=True replaces target."
    ), annotations=_REG(title="FS Move", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _fs_move(path: str, destination: str, overwrite: bool | str = False) -> str:
        return fs_move_fn(path=path, destination=destination, overwrite=overwrite)

    @mcp.tool(name="fs_delete", description=(
        "Delete a file/dir. Keywords: delete, remove, rm, trash, erase. "
        "recursive=True for non-empty dirs."
    ), annotations=_REG(title="FS Delete", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _fs_delete(path: str, recursive: bool | str = False) -> str:
        return fs_delete_fn(path=path, recursive=recursive)

    @mcp.tool(name="fs_search", description=(
        "Find files/dirs matching a glob pattern (recursive by default). "
        "Keywords: search files, find, glob, locate file, file lookup."
    ), annotations=_REG(title="FS Search", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _fs_search(path: str, pattern: str, recursive: bool | str = True) -> str:
        return fs_search_fn(path=path, pattern=pattern, recursive=recursive)

    @mcp.tool(name="fs_info", description=(
        "Return metadata (size/dates/type) for a file or directory. "
        "Keywords: file info, stat, metadata, file size, properties."
    ), annotations=_REG(title="FS Info", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _fs_info(path: str) -> str:
        return fs_info_fn(path=path)

    @mcp.tool(name="clipboard_get", description=(
        "Get clipboard content. Keywords: clipboard, paste, read clipboard, "
        "get copied text. keyboard=True simulates Ctrl+C."
    ), annotations=_REG(title="Clipboard Get", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _clipboard_get(keyboard: bool | str = False) -> str:
        return clipboard_get_fn(keyboard=keyboard)

    @mcp.tool(name="clipboard_set", description=(
        "Set clipboard content. Keywords: clipboard, copy, set clipboard, "
        "write clipboard. keyboard=True simulates Ctrl+V."
    ), annotations=_REG(title="Clipboard Set", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _clipboard_set(text: str, keyboard: bool | str = False) -> str:
        return clipboard_set_fn(text=text, keyboard=keyboard)

    @mcp.tool(name="process_list", description=(
        "List running processes with PID, name, memory, and CPU time. "
        "Keywords: task manager, running tasks, processes, memory usage, CPU usage. "
        "sort_by=memory|cpu|name; limit caps the row count (0=all); name filters "
        "by case-insensitive substring."
    ), annotations=_REG(title="Process List", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _process_list(sort_by: Annotated[str, Field(
                          description="Sort key: 'memory' (default), 'cpu', or 'name'."
                      )] = "memory",
                      limit: Annotated[int, Field(
                          description="Cap on rows returned; 0 (default) = no limit."
                      )] = 0,
                      name: str | None = None) -> str:
        return process_list_fn(sort_by=sort_by, limit=limit, name=name)

    @mcp.tool(name="process_kill", description=(
        "Kill a process by PID (int) or name (str). "
        "Keywords: kill, terminate, end process, stop process, force kill, close app. "
        "Default is graceful (lets the app save); force=True 强杀 immediately."
    ), annotations=_REG(title="Process Kill", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _process_kill(pid_or_name: int | str, force: bool | str = False) -> str:
        return process_kill_fn(pid_or_name=pid_or_name, force=force)

    @mcp.tool(name="notify", description=(
        "Show a Windows toast notification. Keywords: notification, toast, "
        "alert, message popup, notify user. app_id defaults to PowerShell's AUMID."
    ), annotations=_REG(title="Notify", readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def _notify(title: str, message: str, app_id: str | None = None) -> str:
        return notify_fn(title=title, message=message, app_id=app_id)

    @mcp.tool(name="registry", description=(
        "Windows Registry operation. Keywords: registry, regedit, registry key, "
        "registry value, HKCU, HKLM, system settings. mode: get/set/delete/list. "
        "path is a PowerShell-drive path (e.g. 'HKCU:\\\\Software\\\\MyApp'). "
        "set needs name+value (type: String/DWord/QWord/MultiString/Binary); "
        "get needs name; delete takes optional name (omit to delete the whole "
        "key); list takes only path."
    ), annotations=_REG(title="Registry", readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _registry(mode: Annotated[Literal["get", "set", "delete", "list"], Field(
                      description="Operation: 'get' a value, 'set' a value, 'delete' a value/key, or 'list' a key's values."
                  )],
                  path: str,
                  name: str | None = None, value: Any = None,
                  type: Annotated[str, Field(
                      description="Value type for 'set': 'String' (default), 'DWord', 'QWord', 'MultiString', or 'Binary'."
                  )] = "String") -> str:
        return registry_fn(mode=mode, path=path, name=name, value=value, type=type)
