"""Tests for local-only tool-call telemetry (TODO #9).

Covers:
- ``record_tool_call`` line format, escaping, rotation, best-effort failure,
  and the disabled short-circuit (default off).
- ``_parse_telemetry_enabled`` flag > env > config precedence.
- ``apply_tool_telemetry`` wrapping: signature preserved, success/failure
  recorded, idempotent, no-op when disabled.
- CLI dispatcher integration: both daemon and --no-daemon paths record, and
  failures are recorded then re-raised.

Telemetry writes to ``~/.agent-windows/logs/telemetry.log``; every test
redirects ``get_log_dir`` to a tmp path so nothing touches the real data dir.
"""
import asyncio
from unittest.mock import MagicMock

import pytest

import agent_windows.telemetry as telemetry_mod
from agent_windows.telemetry import (
    record_tool_call,
    _parse_telemetry_enabled,
    apply_tool_telemetry,
    configure,
)


@pytest.fixture
def tmp_log_dir(tmp_path, monkeypatch):
    """Redirect telemetry to a tmp logs dir and enable it for the test."""
    monkeypatch.setattr(
        "agent_windows.telemetry.get_log_dir", lambda: tmp_path)
    prev = telemetry_mod._enabled
    configure(True)
    yield tmp_path
    configure(prev)


@pytest.fixture(autouse=True)
def _reset_enabled():
    """Telemetry is off by default; restore that after every test."""
    prev = telemetry_mod._enabled
    yield
    telemetry_mod._enabled = prev


# ── record_tool_call ────────────────────────────────────────────────────


def test_disabled_writes_nothing(tmp_path, monkeypatch):
    """Default off: record_tool_call creates no file."""
    monkeypatch.setattr("agent_windows.telemetry.get_log_dir", lambda: tmp_path)
    configure(False)
    record_tool_call("cli", "screenshot", 42, True)
    assert not (tmp_path / "telemetry.log").exists()


def test_record_success_line_format(tmp_log_dir):
    record_tool_call("mcp", "screenshot", 142, True)
    line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
    # timestamp | source | tool | ok | duration_ms   (no trailing error field)
    parts = line.rstrip("\n").split(" | ")
    assert len(parts) == 5
    assert parts[1] == "mcp"
    assert parts[2] == "screenshot"
    assert parts[3] == "ok"
    assert parts[4] == "142"
    # timestamp is ISO with milliseconds
    assert "T" in parts[0] and "." in parts[0]


def test_record_failure_includes_error_type(tmp_log_dir):
    record_tool_call("cli", "click", 8, False, "RuntimeError")
    line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
    parts = line.rstrip("\n").split(" | ")
    assert len(parts) == 6
    assert parts[3] == "fail"
    assert parts[5] == "RuntimeError"


def test_record_success_omits_error_field_even_if_passed(tmp_log_dir):
    """success=True must never append an error field (data minimization)."""
    record_tool_call("cli", "snapshot", 5, True, "ShouldNotAppear")
    line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
    parts = line.rstrip("\n").split(" | ")
    assert len(parts) == 5
    assert "ShouldNotAppear" not in line


def test_record_escapes_pipe_and_newline_in_tool_name(tmp_log_dir):
    record_tool_call("cli", "a|b\nc", 1, True)
    line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
    # Exactly one trailing newline (the embedded \n must be escaped, not literal).
    assert line.count("\n") == 1
    parts = line.rstrip("\n").split(" | ")
    assert parts[2] == "a\\|b\\nc"  # pipe and newline escaped


def test_record_escapes_backslash(tmp_log_dir):
    record_tool_call("cli", "a\\b", 1, True)
    line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
    parts = line.rstrip("\n").split(" | ")
    assert parts[2] == "a\\\\b"


def test_record_escapes_tab(tmp_log_dir):
    """Tab is escaped so it can't misalign the | -delimited columns."""
    record_tool_call("cli", "a\tb", 1, True)
    line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
    assert "\t" not in line.rstrip("\n")
    parts = line.rstrip("\n").split(" | ")
    assert parts[2] == "a\\tb"


def test_record_best_effort_swallows_write_error(tmp_log_dir, monkeypatch):
    """A write failure must never raise out of record_tool_call."""
    real_open = open

    def _boom(path, *a, **k):
        if str(path).endswith("telemetry.log"):
            raise OSError("disk full")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _boom)
    # Must not raise:
    record_tool_call("cli", "screenshot", 1, True)


def test_record_appends_multiple_lines(tmp_log_dir):
    for i in range(3):
        record_tool_call("cli", "screenshot", i, True)
    lines = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3


def test_record_rotates_when_over_max_bytes(tmp_log_dir, monkeypatch):
    """A log exceeding _MAX_BYTES rotates to .1 before the new write."""
    monkeypatch.setattr(telemetry_mod, "_MAX_BYTES", 100)
    log = tmp_log_dir / "telemetry.log"
    # Pre-write a file over the cap.
    log.write_text("x" * 200, encoding="utf-8")
    record_tool_call("cli", "screenshot", 1, True)
    assert (tmp_log_dir / "telemetry.log.1").exists()
    # Old oversized content is now in .1; new record is the active file.
    rotated = (tmp_log_dir / "telemetry.log.1").read_text(encoding="utf-8")
    assert rotated == "x" * 200
    assert "screenshot" in log.read_text(encoding="utf-8")


def test_record_rotation_failure_does_not_block_write(tmp_log_dir, monkeypatch):
    """If rotation raises, the record write still proceeds."""
    monkeypatch.setattr(telemetry_mod, "_MAX_BYTES", 10)
    log = tmp_log_dir / "telemetry.log"
    log.write_text("x" * 50, encoding="utf-8")

    def _bad_replace(self, *a, **k):
        raise OSError("cannot replace")

    monkeypatch.setattr(
        "pathlib.Path.replace", _bad_replace)
    record_tool_call("cli", "screenshot", 1, True)  # must not raise
    # The new record landed (rotation just failed silently).
    assert "screenshot" in log.read_text(encoding="utf-8")


# ── _parse_telemetry_enabled ────────────────────────────────────────────


def test_parse_defaults_false():
    assert _parse_telemetry_enabled(False, None, False) is False
    assert _parse_telemetry_enabled(False, None, None) is False


def test_parse_flag_beats_env_and_config():
    assert _parse_telemetry_enabled(True, "false", False) is True


def test_parse_env_beats_config():
    for truthy in ("1", "true", "TRUE", "yes", "Yes"):
        assert _parse_telemetry_enabled(False, truthy, False) is True


def test_parse_env_falsy_falls_through_to_config():
    assert _parse_telemetry_enabled(False, "false", True) is True
    assert _parse_telemetry_enabled(False, "no", False) is False


def test_parse_env_invalid_string_is_falsy():
    assert _parse_telemetry_enabled(False, "maybe", False) is False


def test_parse_config_bool():
    assert _parse_telemetry_enabled(False, None, True) is True
    assert _parse_telemetry_enabled(False, None, False) is False


# ── apply_tool_telemetry (real FastMCP registry) ────────────────────────


def _build_fresh_server():
    from agent_windows.mcp.server import build_server, reset_server
    reset_server()
    return build_server(), reset_server


def _get_tools(mcp):
    """Return {name: FunctionTool} across both FastMCP registry shapes."""
    tool_mgr = getattr(mcp, "_tool_manager", None)
    tools_dict = getattr(tool_mgr, "_tools", None)
    if tools_dict is not None:
        return dict(tools_dict)
    provider = getattr(mcp, "_local_provider", None)
    components = getattr(provider, "_components", {})
    return {
        (getattr(v, "name", None) or k.split(":", 1)[1].split("@", 1)[0]): v
        for k, v in components.items()
        if isinstance(k, str) and k.startswith("tool:")
    }


def test_disabled_wraps_nothing():
    mcp, reset = _build_fresh_server()
    try:
        tools = _get_tools(mcp)
        params_before = {n: dict(t.parameters) for n, t in tools.items()}
        apply_tool_telemetry(mcp, enabled=False)
        for name, tool in tools.items():
            assert not getattr(tool.fn, "__telemetry_wrapped__", False), name
        # params unchanged
        for name, tool in tools.items():
            assert dict(tool.parameters) == params_before[name], name
    finally:
        reset()


def test_enabled_wraps_all_tools_and_preserves_signature():
    mcp, reset = _build_fresh_server()
    try:
        tools = _get_tools(mcp)
        params_before = {n: dict(t.parameters) for n, t in tools.items()}
        apply_tool_telemetry(mcp, enabled=True)
        for name, tool in tools.items():
            assert getattr(tool.fn, "__telemetry_wrapped__", False), name
            # signature/schema preserved (functools.wraps keeps __wrapped__)
            assert dict(tool.parameters) == params_before[name], name
    finally:
        reset()


def test_wrapper_records_success(tmp_log_dir):
    mcp, reset = _build_fresh_server()
    try:
        apply_tool_telemetry(mcp, enabled=True)
        screenshot = _get_tools(mcp)["screenshot"]
        result = asyncio.run(screenshot.fn(output=None, monitor=-1,
                                           bbox=None, selector=None))
        # The wrapped fn returns the same shape ([text, ...]).
        assert isinstance(result, list)
        line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
        parts = line.rstrip("\n").split(" | ")
        assert parts[1] == "mcp"
        assert parts[2] == "screenshot"
        assert parts[3] == "ok"
    finally:
        reset()


def test_wrapper_records_failure_and_reraises(tmp_log_dir):
    mcp, reset = _build_fresh_server()
    try:
        apply_tool_telemetry(mcp, enabled=True)
        # get_text requires a selector; calling with "" raises ValueError.
        get_text = _get_tools(mcp)["get_text"]
        with pytest.raises(ValueError):
            asyncio.run(get_text.fn(selector=""))
        line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
        parts = line.rstrip("\n").split(" | ")
        assert parts[2] == "get_text"
        assert parts[3] == "fail"
        assert parts[5] == "ValueError"
    finally:
        reset()


def test_wrapper_idempotent():
    mcp, reset = _build_fresh_server()
    try:
        apply_tool_telemetry(mcp, enabled=True)
        tool = _get_tools(mcp)["screenshot"]
        fn_after_first = tool.fn
        apply_tool_telemetry(mcp, enabled=True)  # second pass
        assert tool.fn is fn_after_first  # not re-wrapped
    finally:
        reset()


def test_wrapper_records_nothing_when_disabled_after_wrap(tmp_path, monkeypatch):
    """If telemetry is disabled at record time, the wrapper's record is a no-op
    (the wrapper is still installed, but record_tool_call short-circuits)."""
    monkeypatch.setattr("agent_windows.telemetry.get_log_dir", lambda: tmp_path)
    mcp, reset = _build_fresh_server()
    try:
        apply_tool_telemetry(mcp, enabled=True)
        configure(False)  # disable AFTER wrapping
        screenshot = _get_tools(mcp)["screenshot"]
        asyncio.run(screenshot.fn(output=None, monitor=-1, bbox=None, selector=None))
        assert not (tmp_path / "telemetry.log").exists()
    finally:
        configure(False)
        reset()


# ── CLI dispatcher integration ──────────────────────────────────────────


def _reset_dispatcher_telemetry():
    """Force _ensure_telemetry_configured to re-resolve on next dispatch."""
    import agent_windows.cli.dispatcher as d
    d._telemetry_configured = False


def _enable_cli_telemetry(monkeypatch):
    """Make the dispatcher's own env>config resolution turn telemetry on.

    The dispatcher resolves the switch lazily (env > [telemetry] config); setting
    the env var means ``_ensure_telemetry_configured`` calls ``configure(True)``
    itself, rather than the test fixture's ``configure(True)`` being overridden.
    """
    monkeypatch.setenv("AGENT_WINDOWS_TELEMETRY", "true")


def test_dispatch_records_cli_call(tmp_log_dir, monkeypatch):
    """Daemon-path success records a cli | <method> | ok line."""
    from agent_windows.cli.dispatcher import dispatch
    _reset_dispatcher_telemetry()
    _enable_cli_telemetry(monkeypatch)
    monkeypatch.setattr(
        "agent_windows.cli.dispatcher.call_daemon",
        lambda method, params, debug=False: {"output": "ok"})
    dispatch("screenshot", {}, ctx=_ctx(no_daemon=False))
    line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
    parts = line.rstrip("\n").split(" | ")
    assert parts[1] == "cli"
    assert parts[2] == "screenshot"
    assert parts[3] == "ok"


def test_dispatch_records_no_daemon_path(tmp_log_dir, monkeypatch):
    """The --no-daemon path is also recorded."""
    from agent_windows.cli.dispatcher import dispatch
    _reset_dispatcher_telemetry()
    _enable_cli_telemetry(monkeypatch)
    monkeypatch.setattr(
        "agent_windows.daemon.router.HANDLERS",
        {"ping": lambda p: {"status": "ok"}})
    monkeypatch.setattr("pythoncom.CoInitialize", lambda: None)
    monkeypatch.setattr("pythoncom.CoUninitialize", lambda: None)
    dispatch("ping", {}, ctx=_ctx(no_daemon=True))
    line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
    parts = line.rstrip("\n").split(" | ")
    assert parts[2] == "ping"
    assert parts[3] == "ok"


def test_dispatch_records_failure_and_reraises(tmp_log_dir, monkeypatch):
    """A failing daemon call is recorded as fail + re-raised."""
    from agent_windows.cli.dispatcher import dispatch
    _reset_dispatcher_telemetry()
    _enable_cli_telemetry(monkeypatch)

    def _boom(method, params, debug=False):
        raise RuntimeError("Daemon error [-1]: boom")

    monkeypatch.setattr("agent_windows.cli.dispatcher.call_daemon", _boom)
    with pytest.raises(RuntimeError):
        dispatch("click", {"x": 1, "y": 2}, ctx=_ctx(no_daemon=False))
    line = (tmp_log_dir / "telemetry.log").read_text(encoding="utf-8")
    parts = line.rstrip("\n").split(" | ")
    assert parts[2] == "click"
    assert parts[3] == "fail"
    assert parts[5] == "RuntimeError"


def test_dispatch_disabled_records_nothing(tmp_path, monkeypatch):
    """Default off: no telemetry file is created."""
    monkeypatch.setattr("agent_windows.telemetry.get_log_dir", lambda: tmp_path)
    configure(False)
    from agent_windows.cli.dispatcher import dispatch
    _reset_dispatcher_telemetry()
    monkeypatch.setattr(
        "agent_windows.cli.dispatcher.call_daemon",
        lambda method, params, debug=False: {"output": "ok"})
    dispatch("screenshot", {}, ctx=_ctx(no_daemon=False))
    assert not (tmp_path / "telemetry.log").exists()


# ── helpers ─────────────────────────────────────────────────────────────


def _ctx(no_daemon: bool):
    """Build a typer-like ctx whose .obj carries the dispatch flags."""
    ctx = MagicMock()
    ctx.obj = {"no_daemon": no_daemon, "debug": False, "format": "text"}
    return ctx
